"""E2E CliRunner tests for ``nest-cli auth status`` (FR-CRED-10) — wifi branch.

Coverage:

- No credentials at all → both records configured=false.
- Wifi v2 only → wifi configured=true, schema_version=2, refresh_token_present=false.
- Wifi v3 only → schema_version=3, refresh_token_present=true.
- Master token / refresh token NEVER appear in output (§6.7 redaction).
- --output jsonl emits one record per family (cam + wifi).
- --quiet suppresses stdout.
- Empty stub (post-revoke) reads as configured=false with note.
- Loose chmod on wifi creds → exit 2 family=wifi.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.auth_cmd import auth_group

SeedV2 = Any
SeedV3 = Any


def test_status_no_creds_reports_both_unconfigured(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 2
    cam = next(r for r in payload if r["family"] == "cam")
    wifi = next(r for r in payload if r["family"] == "wifi")
    assert cam["configured"] is False
    assert wifi["configured"] is False


def test_status_v2_wifi_reports_no_refresh_token(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    records = json.loads(result.output)
    wifi = next(r for r in records if r["family"] == "wifi")
    assert wifi["configured"] is True
    assert wifi["schema_version"] == 2
    assert wifi["refresh_token_present"] is False


def test_status_v3_wifi_reports_refresh_token_present(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    records = json.loads(result.output)
    wifi = next(r for r in records if r["family"] == "wifi")
    assert wifi["schema_version"] == 3
    assert wifi["refresh_token_present"] is True


def test_status_redacts_secrets(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    """Master token and refresh token NEVER appear in the rendered output."""
    seeded = seed_v3_creds()
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    assert seeded.master_token not in result.output
    assert (seeded.refresh_token or "MISSING_TOKEN") not in result.output


def test_status_wifi_email_appears_in_record(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds(google_account_email="dan@example.com")
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    records = json.loads(result.output)
    wifi = next(r for r in records if r["family"] == "wifi")
    assert wifi["google_account_email"] == "dan@example.com"


def test_status_jsonl_emits_one_per_line(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(auth_group, ["status", "--jsonl"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().splitlines() if line]
    assert len(lines) == 2
    families = {json.loads(line)["family"] for line in lines}
    assert families == {"cam", "wifi"}


def test_status_quiet_suppresses_stdout(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(auth_group, ["status", "--quiet"])
    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_status_empty_stub_reports_revoked_note(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    """Post-revoke empty {} stub → configured=false with note."""
    path = default_wifi_credentials_path()
    seed_v2_creds()
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o600)
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    records = json.loads(result.output)
    wifi = next(r for r in records if r["family"] == "wifi")
    assert wifi["configured"] is False
    assert "revoked" in wifi.get("note", "")


def test_status_loose_chmod_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
    """0644 wifi creds → exit 2 family=wifi (FR-CRED-12 chmod enforcement)."""
    creds = WifiCredentials(
        version=2,
        type="foyer",
        google_account_email="me@example.com",
        master_token="aas_et/m",  # noqa: S106 - test fixture
        android_id="0123456789abcdef",
        issued_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    path = default_wifi_credentials_path()
    save_wifi_credentials(path, creds)
    os.chmod(path, 0o644)
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_status_wifi_only_cam_unconfigured(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(auth_group, ["status", "--output", "json"])
    assert result.exit_code == 0, result.output
    records = json.loads(result.output)
    cam = next(r for r in records if r["family"] == "cam")
    assert cam["configured"] is False


def test_status_v3_chmod_preserved_after_seed(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    path = default_wifi_credentials_path()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600

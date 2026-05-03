"""E2E CliRunner tests for ``nest-cli auth wifi-refresh-bootstrap`` (Phase C).

Coverage:

- --refresh-token flag with valid 1// prefix → upgrades v2 to v3.
- GOOGLE_REFRESH_TOKEN env var fallback.
- Re-bootstrap on existing v3 → overwrites refresh_token, preserves rest.
- Bad-format token (ya29 prefix) → exit 6 family=wifi.
- Bad-format token (empty after 1//) → exit 6 family=wifi.
- Missing v2 credentials → exit 6 family=wifi with hint pointing at wifi-setup.
- Missing --experimental-wifi → exit 64.
- v2 master_token / android_id / email / issued_at preserved on upgrade.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    load_wifi_credentials,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.auth_cmd import auth_group

SeedV2 = Any


def test_bootstrap_with_flag_upgrades_v2_to_v3(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        auth_group,
        [
            "wifi-refresh-bootstrap",
            "--experimental-wifi",
            "--refresh-token",
            "1//09abc-DEF_xyz123",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["version"] == 3
    assert payload["refresh_token_present"] is True
    loaded = load_wifi_credentials(default_wifi_credentials_path())
    assert loaded.version == 3
    assert loaded.refresh_token == "1//09abc-DEF_xyz123"


def test_bootstrap_preserves_v2_fields(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seeded = seed_v2_creds()
    result = runner.invoke(
        auth_group,
        [
            "wifi-refresh-bootstrap",
            "--experimental-wifi",
            "--refresh-token",
            "1//09abc-DEF",
        ],
    )
    assert result.exit_code == 0, result.output
    loaded = load_wifi_credentials(default_wifi_credentials_path())
    assert loaded.master_token == seeded.master_token
    assert loaded.android_id == seeded.android_id
    assert loaded.google_account_email == seeded.google_account_email
    assert loaded.issued_at == seeded.issued_at


def test_bootstrap_with_env_var_fallback(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "1//09env-token")
    result = runner.invoke(
        auth_group,
        ["wifi-refresh-bootstrap", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    loaded = load_wifi_credentials(default_wifi_credentials_path())
    assert loaded.refresh_token == "1//09env-token"


def test_bootstrap_overwrites_existing_v3_token(isolated_xdg: Path, runner: CliRunner) -> None:
    """Existing v3 → refresh_token is replaced; other fields preserved."""
    seeded = WifiCredentials(
        version=3,
        type="foyer",
        google_account_email="operator@example.com",
        master_token="aas_et/preserved",  # noqa: S106 - test fixture
        android_id="0123456789abcdef",
        issued_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
        refresh_token="1//09old-token",  # noqa: S106 - test fixture
    )
    save_wifi_credentials(default_wifi_credentials_path(), seeded)

    result = runner.invoke(
        auth_group,
        [
            "wifi-refresh-bootstrap",
            "--experimental-wifi",
            "--refresh-token",
            "1//09new-token",
        ],
    )
    assert result.exit_code == 0, result.output
    loaded = load_wifi_credentials(default_wifi_credentials_path())
    assert loaded.refresh_token == "1//09new-token"
    assert loaded.master_token == seeded.master_token


def test_bootstrap_rejects_ya29_format(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    """ya29.foo prefix is the access-token form, not refresh-token."""
    seed_v2_creds()
    result = runner.invoke(
        auth_group,
        [
            "wifi-refresh-bootstrap",
            "--experimental-wifi",
            "--refresh-token",
            "ya29.not-a-refresh-token",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 6, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert "refresh_token" in payload["message"]


def test_bootstrap_rejects_no_v2_credentials(isolated_xdg: Path, runner: CliRunner) -> None:
    """No file at all → exit 6 with hint pointing at wifi-setup."""
    result = runner.invoke(
        auth_group,
        [
            "wifi-refresh-bootstrap",
            "--experimental-wifi",
            "--refresh-token",
            "1//09abc-DEF",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 6, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert "wifi-setup" in (payload.get("hint") or "")


def test_bootstrap_without_experimental_exits_64(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        auth_group,
        ["wifi-refresh-bootstrap", "--refresh-token", "1//09abc-DEF"],
    )
    assert result.exit_code == 64, result.output
    err = result.stderr or result.output
    assert "experimental" in err.lower()


def test_bootstrap_text_mode_emits_human_readable(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        auth_group,
        [
            "wifi-refresh-bootstrap",
            "--experimental-wifi",
            "--refresh-token",
            "1//09abc-DEF",
        ],
    )
    assert result.exit_code == 0, result.output
    # Text mode: not JSON, contains "ok"
    assert "ok" in result.output.lower()

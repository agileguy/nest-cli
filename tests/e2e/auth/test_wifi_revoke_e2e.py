"""E2E CliRunner tests for ``nest-cli auth wifi-revoke`` (FR-CRED-9).

Coverage:

- --yes scrubs file to empty {} stub atomically.
- --yes preserves chmod 0600 on the scrubbed file.
- Stderr emits the FR-CRED-9 reminder pointing at myaccount.google.com.
- No existing creds → exit 0 noop with reminder still emitted.
- Missing --experimental-wifi → exit 64.
- TTY interactive 'y' confirmation → scrubs.
- TTY interactive 'n' confirmation → leaves file intact.
- --output json mode emits {status, credentials_path}.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    WIFI_REVOCATION_REMINDER,
    default_wifi_credentials_path,
)
from nest_cli.cli.auth_cmd import auth_group

SeedV2 = Any


def test_wifi_revoke_yes_scrubs_to_empty_stub(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    path = default_wifi_credentials_path()
    result = runner.invoke(
        auth_group,
        ["wifi-revoke", "--experimental-wifi", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert path.read_text(encoding="utf-8").strip() == "{}"


def test_wifi_revoke_preserves_chmod_0600(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    path = default_wifi_credentials_path()
    result = runner.invoke(
        auth_group,
        ["wifi-revoke", "--experimental-wifi", "--yes"],
    )
    assert result.exit_code == 0, result.output
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_wifi_revoke_emits_stderr_reminder(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        auth_group,
        ["wifi-revoke", "--experimental-wifi", "--yes"],
    )
    assert result.exit_code == 0, result.output
    err = result.stderr or result.output
    assert "myaccount.google.com/permissions" in err
    assert WIFI_REVOCATION_REMINDER in err


def test_wifi_revoke_no_creds_is_idempotent_noop(isolated_xdg: Path, runner: CliRunner) -> None:
    """No file to scrub → exit 0 (idempotency over erroring out)."""
    result = runner.invoke(
        auth_group,
        ["wifi-revoke", "--experimental-wifi", "--yes"],
    )
    assert result.exit_code == 0, result.output


def test_wifi_revoke_no_creds_still_emits_reminder(isolated_xdg: Path, runner: CliRunner) -> None:
    """Even when noop, the reminder lands on stderr (operator guidance)."""
    result = runner.invoke(
        auth_group,
        ["wifi-revoke", "--experimental-wifi", "--yes"],
    )
    err = result.stderr or result.output
    assert "myaccount.google.com" in err


def test_wifi_revoke_without_experimental_exits_64(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(auth_group, ["wifi-revoke"])
    assert result.exit_code == 64, result.output


def test_wifi_revoke_tty_yes_confirmation_scrubs(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    """Interactive 'y' on confirmation → scrubs."""
    seed_v2_creds()
    path = default_wifi_credentials_path()
    result = runner.invoke(
        auth_group,
        ["wifi-revoke", "--experimental-wifi"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert path.read_text(encoding="utf-8").strip() == "{}"


def test_wifi_revoke_tty_no_confirmation_leaves_intact(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    """Interactive 'n' on confirmation → file unchanged."""
    seed_v2_creds()
    path = default_wifi_credentials_path()
    before = path.read_text(encoding="utf-8")
    result = runner.invoke(
        auth_group,
        ["wifi-revoke", "--experimental-wifi"],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    after = path.read_text(encoding="utf-8")
    assert before == after


def test_wifi_revoke_json_output_emits_status_record(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    """``--output json`` emits the {status, credentials_path} envelope on stdout.

    The reminder line lands on stderr (not stdout). When result.stderr is
    available we parse stdout directly; when CliRunner has mixed them we
    look for the trailing JSON object in result.output.
    """
    seed_v2_creds()
    result = runner.invoke(
        auth_group,
        ["wifi-revoke", "--experimental-wifi", "--yes", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    # Extract the JSON object from output (which may include the stderr
    # reminder if Click mixes streams). The JSON object starts at the
    # first { that decodes cleanly.
    raw = result.output
    json_start = raw.find("{")
    payload = json.loads(raw[json_start:])
    assert payload["status"] == "revoked"
    assert "credentials_path" in payload

"""CliRunner tests for ``nest-cli wifi reboot group`` (FR-WIFI-11/12).

Coverage:

- TTY + interactive yes → reboots, names point list on stderr.
- Single confirmation prompt for the whole group (not per-point).
- Non-tty without --yes → exit 64.
- Non-tty with --yes → reboots, returns rebooted point list.
- --quiet implies --yes.
- Unknown group id → exit 4.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.wifi_cmd import wifi_group


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_wifi_creds() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=1,
            type="foyer",
            google_account_email="me@example.com",
            master_token="t",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
        ),
    )


def _force_tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Patch the verb-side TTY accessor (CliRunner makes raw sys.stdin
    non-overridable for tests)."""
    monkeypatch.setattr("nest_cli.cli.wifi_cmd._stdin_is_tty", lambda: value)


def test_reboot_group_tty_interactive_yes(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY + 'y' → reboot proceeds; upstream restart_system called once."""
    _force_tty(monkeypatch, True)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--experimental-wifi",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    restart_calls = [c for c in last.calls if c[0] == "restart_system"]
    assert len(restart_calls) == 1
    assert restart_calls[0][1] == ("group-home-001",)


def test_reboot_group_non_tty_with_yes(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-tty + --yes → reboot proceeds, only ONE upstream restart_system call."""
    _force_tty(monkeypatch, False)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    restart_calls = [c for c in last.calls if c[0] == "restart_system"]
    # FR-WIFI-11: prompts once for the group; one upstream call covers all points.
    assert len(restart_calls) == 1
    assert restart_calls[0][1] == ("group-home-001",)


def test_reboot_group_non_tty_without_yes_exits_64(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-tty without --yes (and no stdin input) → exit 64 (family=wifi)."""
    _force_tty(monkeypatch, False)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 64, result.output
    err = result.stderr
    json_part = err[err.find("{") :]
    payload = json.loads(json_part)
    assert payload["family"] == "wifi"


def test_reboot_group_quiet_implies_yes(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-WIFI-12: --quiet alone (non-tty) implies --yes."""
    _force_tty(monkeypatch, False)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--quiet",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output == ""
    last = fake_googlewifi.last_instance
    assert last is not None
    restart_calls = [c for c in last.calls if c[0] == "restart_system"]
    assert len(restart_calls) == 1


def test_reboot_group_unknown_group_exits_4(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown group id → exit 4 (family=wifi)."""
    _force_tty(monkeypatch, False)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-no-such",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_reboot_group_tty_lists_points_on_stderr(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-WIFI-11: stderr names the resolved point list before the prompt."""
    _force_tty(monkeypatch, True)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--experimental-wifi",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    err = result.stderr
    # stderr should mention at least one of the points by id.
    assert "ap-master-living-room" in err or "ap-sat-office" in err

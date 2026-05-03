"""CliRunner tests for ``nest-cli wifi reboot point`` (FR-WIFI-10/12).

Coverage:

- TTY + interactive yes → reboots, exit 0.
- TTY + interactive no/empty → aborts, exit 0 with message on stderr.
- Non-tty without --yes → exit 64 (family=wifi).
- Non-tty with --yes → reboots.
- --quiet implies --yes (FR-WIFI-12) → reboots silently.
- --experimental-wifi gate enforced.
- Unknown point id → exit 4.
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
    """Pretend the verb's TTY check returns ``value``.

    Patches ``nest_cli.cli.wifi_cmd._stdin_is_tty`` directly because
    CliRunner replaces ``sys.stdin`` with an in-memory stream whose
    ``isatty()`` is non-overridable from a fixture.
    """
    monkeypatch.setattr("nest_cli.cli.wifi_cmd._stdin_is_tty", lambda: value)


def test_reboot_point_tty_interactive_yes(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY + 'y' on prompt → reboot proceeds. Upstream restart_ap is called."""
    _force_tty(monkeypatch, True)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--experimental-wifi",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    restart_calls = [c for c in last.calls if c[0] == "restart_ap"]
    assert len(restart_calls) == 1
    assert restart_calls[0][1] == ("ap-master-living-room",)


def test_reboot_point_tty_interactive_no_aborts(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY + 'n' on prompt → abort, exit 0, no upstream call."""
    _force_tty(monkeypatch, True)
    # Reset the shared last_instance so cross-test pollution from a
    # prior happy-path test doesn't make this test see a phantom call.
    fake_googlewifi.last_instance = None
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--experimental-wifi",
        ],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    # Confirmation refused — confirm 'Aborted.' message + no restart_ap call.
    assert "Aborted" in result.stderr
    # No FoyerClient was constructed because confirm short-circuited;
    # last_instance stayed None.
    assert fake_googlewifi.last_instance is None


def test_reboot_point_non_tty_without_yes_exits_64(
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
            "point",
            "ap-master-living-room",
            "--experimental-wifi",
            "--output",
            "json",
        ],
        # No input → click.confirm raises Abort → verb maps to exit 64.
    )
    assert result.exit_code == 64, result.output
    # The JSON envelope is written to stderr; the Click prompt echo may
    # also be in stderr. Locate the JSON line within stderr.
    err = result.stderr
    json_part = err[err.find("{") :]
    payload = json.loads(json_part)
    assert payload["family"] == "wifi"


def test_reboot_point_non_tty_with_yes_reboots(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-tty + --yes → reboot proceeds."""
    _force_tty(monkeypatch, False)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    restart_calls = [c for c in last.calls if c[0] == "restart_ap"]
    assert len(restart_calls) == 1


def test_reboot_point_quiet_implies_yes(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-WIFI-12: --quiet alone (non-tty) implies --yes; reboot proceeds."""
    _force_tty(monkeypatch, False)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--quiet",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code == 0, result.output
    # quiet → no stdout, exit code is the only signal.
    assert result.output == ""
    last = fake_googlewifi.last_instance
    assert last is not None
    restart_calls = [c for c in last.calls if c[0] == "restart_ap"]
    assert len(restart_calls) == 1


def test_reboot_point_unknown_point_exits_4(
    isolated_xdg: Path,
    fake_googlewifi: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown point id → exit 4 (family=wifi)."""
    _force_tty(monkeypatch, False)
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-no-such-point",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_reboot_point_requires_experimental_flag(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """Missing --experimental-wifi → exit 64."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
        ],
    )
    assert result.exit_code == 64

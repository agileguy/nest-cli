"""CliRunner tests for ``nest-cli wifi reboot point`` (FR-WIFI-10/12).

Phase B status (2026-05-03): the reboot-point action verb has not yet
been mapped onto the Foyer gRPC surface; once Click's confirmation gate
passes, every invocation that reaches the FoyerClient layer exits 5
(``unsupported_feature``, family=wifi).

The TTY / --yes / --quiet / --experimental-wifi gating still happens in
the CLI layer *before* the FoyerClient call, so those tests remain
meaningful — they verify the right confirmation behaviour, just with
exit-5 as the post-confirmation outcome (instead of the old exit-0
happy path). Phase C will reinstate the upstream-call assertions.
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
from nest_cli.errors import EXIT_UNSUPPORTED_FEATURE


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_wifi_creds() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=2,
            type="foyer",
            google_account_email="me@example.com",
            master_token="t",
            android_id="0123456789abcdef",
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
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY + 'y' on prompt → confirm passes; verb body exits 5 (Phase B stub)."""
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
            "--output",
            "json",
        ],
        input="y\n",
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    # The Click prompt writes to stderr too; extract the JSON envelope
    # by locating the first '{' after the prompt echo.
    err = result.stderr
    json_part = err[err.find("{") :]
    payload = json.loads(json_part)
    assert payload["family"] == "wifi"


def test_reboot_point_tty_interactive_no_aborts(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY + 'n' on prompt → abort, exit 0, FoyerClient never constructed.

    Confirmation refusal short-circuits before the verb body, so the
    user never sees the Phase-B exit-5; they get the regular Click abort
    path (exit 0 with ``Aborted.`` on stderr).
    """
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
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Aborted" in result.stderr


def test_reboot_point_non_tty_without_yes_exits_64(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-tty without --yes (and no stdin input) → exit 64 (family=wifi).

    The TTY/yes gate fires *before* the Phase-B exit-5, so this stays
    exit 64 (configuration error: missing required confirmation).
    """
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
    )
    assert result.exit_code == 64, result.output
    err = result.stderr
    json_part = err[err.find("{") :]
    payload = json.loads(json_part)
    assert payload["family"] == "wifi"


def test_reboot_point_non_tty_with_yes_exits_5(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-tty + --yes → confirm bypassed; verb body exits 5 (Phase B stub)."""
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


def test_reboot_point_quiet_implies_yes(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-WIFI-12: --quiet alone (non-tty) implies --yes; verb body still exits 5."""
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
    # Confirm gate cleared via --quiet → --yes; verb body exits 5.
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


def test_reboot_point_unknown_point_exits_5(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown point id exits 5 (was 4 pre-Phase-B; verb no longer validates)."""
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_reboot_point_requires_experimental_flag(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """Missing --experimental-wifi → exit 64 (gate fires before verb body)."""
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

"""CliRunner tests for ``nest-cli wifi reboot group`` (FR-WIFI-11/12).

Phase B status (2026-05-03): the reboot-group action verb has not yet
been mapped onto the Foyer gRPC surface; once Click's confirmation gate
passes, the verb body exits 5 (``unsupported_feature``, family=wifi).

The TTY / --yes / --quiet / --experimental-wifi gating still happens in
the CLI layer *before* the FoyerClient call, so those tests remain
meaningful — they verify the right confirmation behaviour with exit-5
as the post-confirmation outcome (was exit-0 happy-path pre-Phase-B).
The ``test_reboot_group_tty_lists_points_on_stderr`` test still exercises
the ``list_points`` pre-resolution step, which works in Phase B.
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
    """Patch the verb-side TTY accessor (CliRunner makes raw sys.stdin
    non-overridable for tests)."""
    monkeypatch.setattr("nest_cli.cli.wifi_cmd._stdin_is_tty", lambda: value)


def test_reboot_group_tty_interactive_yes(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY + 'y' → confirm passes; verb body exits 5 (Phase B stub)."""
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
            "--output",
            "json",
        ],
        input="y\n",
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    # Click prompt + the resolved-points line precede the JSON envelope
    # in stderr; locate the first '{' after them.
    err = result.stderr
    json_part = err[err.find("{") :]
    payload = json.loads(json_part)
    assert payload["family"] == "wifi"


def test_reboot_group_non_tty_with_yes_exits_5(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-tty + --yes → confirm bypassed; verb body exits 5."""
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


def test_reboot_group_non_tty_without_yes_exits_64(
    isolated_xdg: Path,
    fake_googlewifi: None,
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
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-WIFI-12: --quiet alone (non-tty) implies --yes; verb body exits 5."""
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


def test_reboot_group_unknown_group_exits_5(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown group id exits 5 (was 4 pre-Phase-B; verb no longer validates)."""
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_reboot_group_tty_lists_points_on_stderr(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-WIFI-11: stderr names the resolved point list before the prompt.

    The pre-prompt list-resolve uses ``list_points`` (a Phase-B-supported
    read verb), so this output still lands on stderr. After the operator
    confirms, the verb body hits ``reboot_group`` and exits 5 — the
    pre-prompt stderr is unaffected.
    """
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
    # Verb body exits 5 after confirm — but stderr still carries the
    # resolved point list because list_points runs successfully first.
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    err = result.stderr
    assert "ap-master-living-room" in err or "ap-sat-office" in err

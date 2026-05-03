"""CliRunner tests for ``nest-cli wifi reboot point`` (FR-WIFI-10/12, Phase C)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.wifi_cmd import wifi_group
from nest_cli.wifi.client import FoyerClient


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_v3() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=3,
            type="foyer",
            google_account_email="me@example.com",
            master_token="aas_et/m",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
            refresh_token="1//09abc-DEF",
        ),
    )


def _force_tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr("nest_cli.cli.wifi_cmd._stdin_is_tty", lambda: value)


@pytest.fixture
def stub_rest(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_rest(
        self: FoyerClient,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append({"method": method, "path": path, "json": json, "params": params})
        return None

    monkeypatch.setattr(FoyerClient, "_rest", _fake_rest)
    return calls


def test_reboot_point_tty_interactive_yes_succeeds(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_rest: list[dict[str, Any]],
) -> None:
    """TTY + 'y' on prompt → confirm passes, REST POST issued, exit 0."""
    _force_tty(monkeypatch, True)
    _seed_v3()
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
    assert result.exit_code == 0, result.output
    # Verify the verb hit the right endpoint
    assert stub_rest[0]["method"] == "POST"
    assert stub_rest[0]["path"] == "/v2/accesspoints/ap-master-living-room/reboot"


def test_reboot_point_tty_interactive_no_aborts(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY + 'n' on prompt → abort, exit 0, FoyerClient never invoked."""
    _force_tty(monkeypatch, True)
    _seed_v3()
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
    """Non-tty without --yes → exit 64 (confirmation gate)."""
    _force_tty(monkeypatch, False)
    _seed_v3()
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


def test_reboot_point_non_tty_with_yes_succeeds(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_rest: list[dict[str, Any]],
) -> None:
    """Non-tty + --yes → confirm bypassed; verb posts to /accesspoints/.../reboot."""
    _force_tty(monkeypatch, False)
    _seed_v3()
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
    payload = json.loads(result.output)
    assert payload["point_id"] == "ap-master-living-room"
    assert payload["action"] == "reboot"
    assert len(stub_rest) == 1


def test_reboot_point_quiet_implies_yes(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_rest: list[dict[str, Any]],
) -> None:
    """FR-WIFI-12: --quiet alone (non-tty) implies --yes; verb succeeds silently."""
    _force_tty(monkeypatch, False)
    _seed_v3()
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
    assert len(stub_rest) == 1


def test_reboot_point_unknown_point_call_still_issues(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_rest: list[dict[str, Any]],
) -> None:
    """Unknown id still fires POST — Foyer maps 404 → exit 4 server-side."""
    _force_tty(monkeypatch, False)
    _seed_v3()
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
    assert result.exit_code == 0, result.output
    assert stub_rest[0]["path"] == "/v2/accesspoints/ap-no-such-point/reboot"


def test_reboot_point_requires_experimental_flag(isolated_xdg: Path, fake_googlewifi: None) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["reboot", "point", "ap-master-living-room"],
    )
    assert result.exit_code == 64

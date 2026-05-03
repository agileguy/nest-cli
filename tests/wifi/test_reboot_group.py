"""CliRunner tests for ``nest-cli wifi reboot group`` (FR-WIFI-11/12, Phase C)."""

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


def test_reboot_group_tty_interactive_yes_succeeds(
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
            "group",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    # POST to /v2/groups/.../reboot fired
    post_calls = [c for c in stub_rest if c["method"] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0]["path"] == "/v2/groups/group-home-001/reboot"


def test_reboot_group_non_tty_with_yes_succeeds(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_rest: list[dict[str, Any]],
) -> None:
    """Non-tty + --yes → confirm bypassed; verb posts the reboot."""
    _force_tty(monkeypatch, False)
    _seed_v3()
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
    payload = json.loads(result.output)
    assert payload["group_id"] == "group-home-001"
    assert sorted(payload["rebooted_points"]) == [
        "ap-master-living-room",
        "ap-sat-office",
    ]


def test_reboot_group_non_tty_without_yes_exits_64(
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
            "group",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 64, result.output


def test_reboot_group_quiet_implies_yes(
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
            "group",
            "group-home-001",
            "--quiet",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code == 0, result.output
    post_calls = [c for c in stub_rest if c["method"] == "POST"]
    assert len(post_calls) == 1


def test_reboot_group_unknown_group_lists_points_failure(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_rest: list[dict[str, Any]],
) -> None:
    """Unknown group id fails inside list_points (gRPC seam) → exit 4."""
    _force_tty(monkeypatch, False)
    _seed_v3()
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
    # list_points hits the gRPC seam (fake_googlewifi), which doesn't
    # know about group-no-such → exit 4 (not_found, family=wifi).
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_reboot_group_tty_lists_points_on_stderr(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_rest: list[dict[str, Any]],
) -> None:
    """FR-WIFI-11: stderr names the resolved point list before the prompt."""
    _force_tty(monkeypatch, True)
    _seed_v3()
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
    assert "ap-master-living-room" in err or "ap-sat-office" in err

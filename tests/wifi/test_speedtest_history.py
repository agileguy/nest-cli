"""CliRunner tests for ``nest-cli wifi speedtest history`` (FR-WIFI-9, Phase C)."""

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


@pytest.fixture
def stub_history(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch _rest to return a canned history response."""
    state: dict[str, Any] = {
        "calls": [],
        "response": {
            "results": [
                {
                    "downloadSpeedBps": 800_000_000,
                    "uploadSpeedBps": 100_000_000,
                    "pingMs": 15.0,
                    "timestamp": "2026-05-03T11:00:00Z",
                    "apId": "ap-master-living-room",
                },
                {
                    "downloadSpeedBps": 750_000_000,
                    "uploadSpeedBps": 90_000_000,
                    "pingMs": 18.0,
                    "timestamp": "2026-05-03T10:00:00Z",
                    "apId": "ap-master-living-room",
                },
            ]
        },
    }

    def _fake_rest(
        self: FoyerClient,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        state["calls"].append({"method": method, "path": path, "json": json, "params": params})
        return state["response"]

    monkeypatch.setattr(FoyerClient, "_rest", _fake_rest)
    return state


def test_speedtest_history_default_limit(
    isolated_xdg: Path, fake_googlewifi: None, stub_history: dict[str, Any]
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 2
    # Default limit is 30
    assert stub_history["calls"][0]["params"] == {"maxResultCount": 30}


def test_speedtest_history_explicit_limit_passed_through(
    isolated_xdg: Path, fake_googlewifi: None, stub_history: dict[str, Any]
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "5",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stub_history["calls"][0]["params"] == {"maxResultCount": 5}


def test_speedtest_history_limit_below_range_rejected(
    isolated_xdg: Path,
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "0",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "0" in err or "range" in err.lower() or "invalid" in err.lower()


def test_speedtest_history_limit_above_range_rejected(
    isolated_xdg: Path,
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "366",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0


def test_speedtest_history_requires_experimental_flag(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["speedtest", "history", "group-home-001"],
    )
    assert result.exit_code == 64

"""CliRunner tests for ``nest-cli wifi speedtest run`` (FR-WIFI-8, Phase C)."""

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
def stub_speedtest_chain(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Mock the kickoff + result-fetch _rest calls + the operation poller."""
    calls: list[dict[str, Any]] = []
    responses = {
        "POST /v2/groups/group-home-001/wanSpeedTest": {"operationId": "op-123"},
        "GET /v2/groups/group-home-001/speedTestResults": {
            "results": [
                {
                    "downloadSpeedBps": 900_000_000,
                    "uploadSpeedBps": 120_000_000,
                    "pingMs": 12.5,
                    "timestamp": "2026-05-03T12:00:00Z",
                    "apId": "ap-master-living-room",
                }
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
        calls.append({"method": method, "path": path, "json": json, "params": params})
        return responses.get(f"{method} {path}")

    def _fake_wait(self: FoyerClient, op_id: str, *, timeout_s: float = 180.0) -> Any:
        return {"operationState": "DONE", "operationId": op_id}

    monkeypatch.setattr(FoyerClient, "_rest", _fake_rest)
    monkeypatch.setattr(FoyerClient, "_wait_for_operation", _fake_wait)
    return calls


def test_speedtest_run_emits_speedtest_record(
    isolated_xdg: Path,
    fake_googlewifi: None,
    stub_speedtest_chain: list[dict[str, Any]],
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["download_mbps"] == 900.0
    assert payload["upload_mbps"] == 120.0
    assert payload["group_id"] == "group-home-001"
    assert payload["point_id"] == "ap-master-living-room"


def test_speedtest_run_requires_experimental_flag(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["speedtest", "run", "group-home-001", "--output", "json"],
    )
    assert result.exit_code == 64, result.output


def test_speedtest_run_timeout_flag_passed_through(
    isolated_xdg: Path,
    fake_googlewifi: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--timeout 0.5`` is parsed and passed into run_speedtest."""
    _seed_v3()
    captured: dict[str, float] = {}

    def _fake_run(self: FoyerClient, group_id: str, *, timeout_s: float = 180.0) -> Any:
        captured["timeout_s"] = timeout_s
        from nest_cli.wifi.types import SpeedTest

        return SpeedTest(
            ts=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
            group_id=group_id,
            point_id="ap-master-living-room",
            download_mbps=10.0,
            upload_mbps=5.0,
            ping_ms=10.0,
            source="router",
        )

    monkeypatch.setattr(FoyerClient, "run_speedtest", _fake_run)
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--experimental-wifi",
            "--timeout",
            "0.5",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["timeout_s"] == 0.5

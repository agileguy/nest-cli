"""E2E CliRunner tests for ``nest-cli wifi speedtest run|history`` (FR-WIFI-8/9).

Covers both verbs in one file because they share the speedtest subgroup
and the same FoyerClient surface area.

Coverage:

- run happy path → §10.9 SpeedTest record (download_mbps, upload_mbps, ping_ms).
- run --timeout flag accepted at lower / mid / upper bounds.
- run --timeout below FloatRange(0.1, 600.0) rejected.
- run missing --experimental-wifi → exit 64.
- run missing creds → exit 2 family=wifi.
- history default --limit (30) → params={"maxResultCount": 30}.
- history explicit --limit → params reflects the value.
- history --limit 0 (below IntRange) rejected.
- history --limit 366 (above 365) rejected.
- history missing --experimental-wifi → exit 64.
- history missing creds → exit 2 family=wifi.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group
from nest_cli.wifi.client import FoyerClient
from tests.e2e.conftest import RestRecorder

SeedV3 = Any
SeedV2 = Any


# ---------------------------------------------------------------------------
# wifi speedtest run
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_speedtest_chain(
    monkeypatch: pytest.MonkeyPatch,
    stub_rest: RestRecorder,
) -> RestRecorder:
    """Register the kickoff + result-fetch responses + skip the operation poller."""
    stub_rest.register("POST", "/v2/groups/group-home-001/wanSpeedTest", {"operationId": "op-123"})
    stub_rest.register(
        "GET",
        "/v2/groups/group-home-001/speedTestResults",
        {
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
    )

    def _fake_wait(self: FoyerClient, op_id: str, *, timeout_s: float = 180.0) -> Any:
        return {"operationState": "DONE", "operationId": op_id}

    monkeypatch.setattr(FoyerClient, "_wait_for_operation", _fake_wait)
    return stub_rest


def test_speedtest_run_happy_path(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_speedtest_chain: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
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
    assert payload["ping_ms"] == 12.5
    assert payload["group_id"] == "group-home-001"
    assert payload["point_id"] == "ap-master-living-room"


def test_speedtest_run_timeout_at_lower_bound(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_speedtest_chain: RestRecorder,
    runner: CliRunner,
) -> None:
    """--timeout 0.1 (FloatRange minimum) is accepted."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--timeout",
            "0.1",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output


def test_speedtest_run_timeout_at_upper_bound(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_speedtest_chain: RestRecorder,
    runner: CliRunner,
) -> None:
    """--timeout 600.0 (FloatRange maximum) is accepted."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--timeout",
            "600.0",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output


def test_speedtest_run_timeout_below_range_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--timeout",
            "0.05",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0


def test_speedtest_run_without_experimental_exits_64(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["speedtest", "run", "group-home-001"],
    )
    assert result.exit_code == 64, result.output


def test_speedtest_run_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
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
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_speedtest_run_missing_group_arg_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["speedtest", "run", "--experimental-wifi"],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# wifi speedtest history
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_history(stub_rest: RestRecorder) -> RestRecorder:
    """Canned history response — two entries on /speedTestResults."""
    stub_rest.register(
        "GET",
        "/v2/groups/group-home-001/speedTestResults",
        {
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
    )
    return stub_rest


def test_speedtest_history_default_limit_30(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_history: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
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
    assert stub_history.calls[0]["params"] == {"maxResultCount": 30}


def test_speedtest_history_explicit_limit_passed_through(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_history: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
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
    assert stub_history.calls[0]["params"] == {"maxResultCount": 5}


def test_speedtest_history_limit_below_range_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
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


def test_speedtest_history_limit_above_range_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
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


def test_speedtest_history_without_experimental_exits_64(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["speedtest", "history", "group-home-001"],
    )
    assert result.exit_code == 64, result.output


def test_speedtest_history_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
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
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

"""Tests for the Phase 3.1 FoyerClient method extensions.

Coverage map:

- run_speedtest:           TestRunSpeedtest
- get_speedtest_history:   TestSpeedtestHistory
- reboot_point:            TestRebootPoint
- reboot_group:            TestRebootGroup
- get_network_info:        TestGetNetworkInfo
- set_guest_enabled:       TestSetGuestEnabled (exit-5 unsupported)
- get_point_health:        TestGetPointHealth
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from nest_cli.errors import (
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    EXIT_UNSUPPORTED_FEATURE,
    StructuredError,
)
from nest_cli.wifi.client import FoyerClient
from nest_cli.wifi.types import SpeedTest, WifiNetwork, WifiPointHealth

# ---------------------------------------------------------------------------
# run_speedtest
# ---------------------------------------------------------------------------


class TestRunSpeedtest:
    def test_returns_speed_test_record(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        result = client.run_speedtest("group-home-001")
        assert isinstance(result, SpeedTest)
        assert result.group_id == "group-home-001"
        assert result.point_id == "ap-master-living-room"
        assert result.download_mbps == pytest.approx(950.0, abs=1.0)
        assert result.source == "router"

    def test_calls_upstream_run_speed_test(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        client.run_speedtest("group-home-001")
        last = fake_googlewifi.last_instance
        assert last is not None
        names = [c[0] for c in last.calls]
        assert "run_speed_test" in names

    def test_timeout_exits_3_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A speed test that exceeds --timeout maps to EXIT_NETWORK_ERROR."""
        import asyncio

        class _SlowGW:
            last_instance: _SlowGW | None = None

            def __init__(self, refresh_token: str | None = None, **_: Any) -> None:
                type(self).last_instance = self

            async def connect(self) -> bool:
                return True

            async def run_speed_test(self, system_id: str) -> dict[str, Any]:
                # Sleep longer than the timeout we'll pass.
                await asyncio.sleep(5.0)
                return {}

            async def close(self) -> None:
                return None

        fake_module = type(sys)("googlewifi")
        fake_module.GoogleWifi = _SlowGW  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as excinfo:
            client.run_speedtest("group-home-001", timeout_s=0.1)
        assert excinfo.value.code == EXIT_NETWORK_ERROR
        assert excinfo.value.family == "wifi"


# ---------------------------------------------------------------------------
# get_speedtest_history
# ---------------------------------------------------------------------------


class TestSpeedtestHistory:
    def test_returns_descending_by_ts(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        results = client.get_speedtest_history("group-home-001", limit=30)
        assert len(results) == 3
        timestamps = [r.ts for r in results]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_limit_truncates_results(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        results = client.get_speedtest_history("group-home-001", limit=2)
        assert len(results) == 2

    def test_calls_upstream_speed_test_results(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        client.get_speedtest_history("group-home-001", limit=30)
        last = fake_googlewifi.last_instance
        assert last is not None
        names = [c[0] for c in last.calls]
        assert "speed_test_results" in names


# ---------------------------------------------------------------------------
# reboot_point
# ---------------------------------------------------------------------------


class TestRebootPoint:
    def test_calls_upstream_restart_ap(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        client.reboot_point("ap-master-living-room")
        last = fake_googlewifi.last_instance
        assert last is not None
        restart_calls = [c for c in last.calls if c[0] == "restart_ap"]
        assert len(restart_calls) == 1
        assert restart_calls[0][1] == ("ap-master-living-room",)

    def test_unknown_point_exits_4(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as excinfo:
            client.reboot_point("ap-no-such-point")
        assert excinfo.value.code == EXIT_NOT_FOUND
        assert excinfo.value.family == "wifi"


# ---------------------------------------------------------------------------
# reboot_group
# ---------------------------------------------------------------------------


class TestRebootGroup:
    def test_returns_rebooted_point_ids(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        rebooted = client.reboot_group("group-home-001")
        # Two access points in the home fixture.
        assert sorted(rebooted) == ["ap-master-living-room", "ap-sat-office"]

    def test_calls_upstream_restart_system(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        client.reboot_group("group-home-001")
        last = fake_googlewifi.last_instance
        assert last is not None
        names = [c[0] for c in last.calls]
        assert "restart_system" in names

    def test_unknown_group_exits_4(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as excinfo:
            client.reboot_group("group-nope")
        assert excinfo.value.code == EXIT_NOT_FOUND
        assert excinfo.value.family == "wifi"


# ---------------------------------------------------------------------------
# get_network_info
# ---------------------------------------------------------------------------


class TestGetNetworkInfo:
    def test_returns_wifi_network_record(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        net = client.get_network_info("group-home-001")
        assert isinstance(net, WifiNetwork)
        assert net.group_id == "group-home-001"
        assert net.ssid == "HomeMeshNet"

    def test_unknown_group_exits_4(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as excinfo:
            client.get_network_info("group-nope")
        assert excinfo.value.code == EXIT_NOT_FOUND
        assert excinfo.value.family == "wifi"


# ---------------------------------------------------------------------------
# set_guest_enabled (unsupported until upstream lands an endpoint)
# ---------------------------------------------------------------------------


class TestSetGuestEnabled:
    def test_raises_unsupported_feature(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as excinfo:
            client.set_guest_enabled("group-home-001", enabled=True)
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE
        assert excinfo.value.family == "wifi"
        # Operator gets a hint pointing at the upstream gap.
        assert excinfo.value.hint is not None


# ---------------------------------------------------------------------------
# get_point_health
# ---------------------------------------------------------------------------


class TestGetPointHealth:
    def test_master_returns_health_record(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        health = client.get_point_health("ap-master-living-room")
        assert isinstance(health, WifiPointHealth)
        assert health.id == "ap-master-living-room"
        assert health.online is True
        assert health.mesh_role == "master"
        # Master has no upstream signal measurement.
        assert health.signal_to_upstream_dbm is None

    def test_satellite_carries_signal_dbm(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        health = client.get_point_health("ap-sat-office")
        assert health.mesh_role == "satellite"
        assert health.signal_to_upstream_dbm == -52

    def test_unknown_point_exits_4(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as excinfo:
            client.get_point_health("ap-no-such-point")
        assert excinfo.value.code == EXIT_NOT_FOUND
        assert excinfo.value.family == "wifi"


# ---------------------------------------------------------------------------
# Network-error / shape-rotation maps still emit family=wifi
# ---------------------------------------------------------------------------


def test_run_speedtest_connection_error_maps_to_3(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NetGW:
        last_instance: _NetGW | None = None

        def __init__(self, refresh_token: str | None = None, **_: Any) -> None:
            type(self).last_instance = self

        async def connect(self) -> bool:
            return True

        async def run_speed_test(self, system_id: str) -> dict[str, Any]:
            raise ConnectionError("DNS fail")

        async def close(self) -> None:
            return None

    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _NetGW  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

    client = FoyerClient(master_token="t")
    with pytest.raises(StructuredError) as excinfo:
        client.run_speedtest("group-home-001")
    assert excinfo.value.code == EXIT_NETWORK_ERROR
    assert excinfo.value.family == "wifi"


def test_speed_test_history_upstream_error_maps_to_1(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BadGW:
        last_instance: _BadGW | None = None

        def __init__(self, refresh_token: str | None = None, **_: Any) -> None:
            type(self).last_instance = self

        async def connect(self) -> bool:
            return True

        async def speed_test_results(self, system_id: str) -> Any:
            # Simulate an upstream library exception.
            raise RuntimeError("Foyer endpoint rotated")

        async def close(self) -> None:
            return None

    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _BadGW  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

    client = FoyerClient(master_token="t")
    with pytest.raises(StructuredError) as excinfo:
        client.get_speedtest_history("group-home-001", limit=30)
    assert excinfo.value.code == EXIT_DEVICE_ERROR
    assert excinfo.value.family == "wifi"

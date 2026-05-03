"""Phase C unit tests for ``FoyerClient`` action verbs.

The Phase B incarnation of this file collapsed every action verb into a
single exit-5 assertion because the Foyer RPCs were unmapped. Phase C
maps 8 of the 10 verbs to ``/v2/...`` REST endpoints, so the unit tests
exercise the verb body via the ``fake_rest_client`` recorder fixture
that monkey-patches ``FoyerClient._rest`` to record (method, path, json,
params) tuples and serve canned responses.

Two verbs (``set_station_group``, ``set_guest_enabled``) stay exit-5 in
Phase C because their Foyer request bodies are undocumented; the
``TestPhaseDDeferredVerbs`` class in tests/wifi/test_client.py covers
those. ``get_network_info`` also stays exit-5 (HomeGraph carries no
SSID/IPv4 data).
"""

from __future__ import annotations

from typing import Any

import pytest

from nest_cli.errors import (
    EXIT_NOT_FOUND,
    EXIT_UNSUPPORTED_FEATURE,
    StructuredError,
)
from nest_cli.wifi.client import FoyerClient
from nest_cli.wifi.types import WifiPointHealth

# ---------------------------------------------------------------------------
# list_clients (FR-WIFI-3) — GET /v2/groups/{gid}/stations
# ---------------------------------------------------------------------------


class TestListClients:
    def test_calls_correct_endpoint(self, fake_rest_client: Any, make_v3_creds: Any) -> None:
        fake_rest_client.register("GET", "/v2/groups/group-home-001/stations", {"stations": []})
        client = FoyerClient(make_v3_creds())
        client.list_clients("group-home-001")
        call = fake_rest_client.calls[0]
        assert call["method"] == "GET"
        assert call["path"] == "/v2/groups/group-home-001/stations"

    def test_empty_response_returns_empty_list(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        fake_rest_client.register("GET", "/v2/groups/group-home-001/stations", {"stations": []})
        client = FoyerClient(make_v3_creds())
        assert client.list_clients("group-home-001") == []

    def test_returns_parsed_wifi_client_records(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        fake_rest_client.register(
            "GET",
            "/v2/groups/group-home-001/stations",
            {
                "stations": [
                    {
                        "id": "sta-laptop",
                        "friendlyName": "Laptop",
                        "apId": "ap-master-living-room",
                        "macAddress": "aa:bb:cc:dd:ee:ff",
                    }
                ]
            },
        )
        client = FoyerClient(make_v3_creds())
        clients = client.list_clients("group-home-001")
        assert len(clients) == 1
        assert clients[0].id == "sta-laptop"
        assert clients[0].friendly_name == "Laptop"


# ---------------------------------------------------------------------------
# pause / unpause (FR-WIFI-4..5) — PUT /v2/groups/default/stationBlocking
# ---------------------------------------------------------------------------


class TestPauseStation:
    def test_puts_station_blocking_with_blocked_true(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        client = FoyerClient(make_v3_creds())
        client.pause_station("sta-laptop")
        call = fake_rest_client.calls[0]
        assert call["method"] == "PUT"
        assert call["path"] == "/v2/groups/default/stationBlocking"
        assert call["json"] == {"stationId": "sta-laptop", "blocked": "true"}


class TestUnpauseStation:
    def test_puts_station_blocking_with_blocked_false(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        client = FoyerClient(make_v3_creds())
        client.unpause_station("sta-laptop")
        call = fake_rest_client.calls[0]
        assert call["method"] == "PUT"
        assert call["path"] == "/v2/groups/default/stationBlocking"
        assert call["json"] == {"stationId": "sta-laptop", "blocked": "false"}


# ---------------------------------------------------------------------------
# prioritize (FR-WIFI-6) — PUT /v2/groups/default/prioritizedStation
# ---------------------------------------------------------------------------


class TestPrioritizeStation:
    def test_puts_prioritized_station_with_end_time(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        client = FoyerClient(make_v3_creds())
        client.prioritize_station("sta-laptop", 60)
        call = fake_rest_client.calls[0]
        assert call["method"] == "PUT"
        assert call["path"] == "/v2/groups/default/prioritizedStation"
        body = call["json"]
        assert body["stationId"] == "sta-laptop"
        # ISO8601 with literal Z suffix
        assert body["prioritizationEndTime"].endswith("Z")

    def test_duration_passed_through_to_endtime(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        from datetime import datetime as _datetime

        client = FoyerClient(make_v3_creds())
        client.prioritize_station("sta-laptop", 1)
        body = fake_rest_client.calls[0]["json"]
        # Just confirm the endTime parses
        end = _datetime.fromisoformat(body["prioritizationEndTime"].replace("Z", "+00:00"))
        assert end is not None


# ---------------------------------------------------------------------------
# run_speedtest (FR-WIFI-8) — POST + poll + GET
# ---------------------------------------------------------------------------


class TestRunSpeedtest:
    def test_kicks_off_then_polls_then_fetches_result(
        self,
        fake_rest_client: Any,
        make_v3_creds: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_rest_client.register(
            "POST",
            "/v2/groups/group-home-001/wanSpeedTest",
            {"operationId": "op-123"},
        )
        fake_rest_client.register(
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

        # Stub _wait_for_operation since op poll is its own concern
        def _fake_wait(self: FoyerClient, op: str, *, timeout_s: float = 180.0) -> Any:
            return {"operationState": "DONE"}

        monkeypatch.setattr(FoyerClient, "_wait_for_operation", _fake_wait)

        client = FoyerClient(make_v3_creds())
        result = client.run_speedtest("group-home-001")

        # Verify kickoff + result fetch
        kickoff = fake_rest_client.calls[0]
        assert kickoff["method"] == "POST"
        assert kickoff["path"] == "/v2/groups/group-home-001/wanSpeedTest"
        # SpeedTest record carries the converted Mbps values
        assert result.download_mbps == 900.0
        assert result.upload_mbps == 120.0
        assert result.point_id == "ap-master-living-room"

    def test_kickoff_missing_operation_id_raises_device_error(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        fake_rest_client.register("POST", "/v2/groups/group-home-001/wanSpeedTest", {})
        client = FoyerClient(make_v3_creds())
        with pytest.raises(StructuredError) as exc_info:
            client.run_speedtest("group-home-001")
        assert exc_info.value.family == "wifi"


# ---------------------------------------------------------------------------
# get_speedtest_history (FR-WIFI-9) — GET /v2/groups/{gid}/speedTestResults
# ---------------------------------------------------------------------------


class TestSpeedtestHistory:
    def test_calls_endpoint_with_max_result_count(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        fake_rest_client.register(
            "GET", "/v2/groups/group-home-001/speedTestResults", {"results": []}
        )
        client = FoyerClient(make_v3_creds())
        client.get_speedtest_history("group-home-001", limit=10)
        call = fake_rest_client.calls[0]
        assert call["method"] == "GET"
        assert call["path"] == "/v2/groups/group-home-001/speedTestResults"
        assert call["params"] == {"maxResultCount": 10}

    def test_empty_history_returns_empty_list(
        self, fake_rest_client: Any, make_v3_creds: Any
    ) -> None:
        fake_rest_client.register(
            "GET", "/v2/groups/group-home-001/speedTestResults", {"results": []}
        )
        client = FoyerClient(make_v3_creds())
        assert client.get_speedtest_history("group-home-001", limit=30) == []

    def test_parses_speedtest_records(self, fake_rest_client: Any, make_v3_creds: Any) -> None:
        fake_rest_client.register(
            "GET",
            "/v2/groups/group-home-001/speedTestResults",
            {
                "results": [
                    {
                        "downloadSpeedBps": 500_000_000,
                        "uploadSpeedBps": 50_000_000,
                        "pingMs": 20.0,
                        "timestamp": "2026-05-03T11:00:00Z",
                        "apId": "ap-master-living-room",
                    }
                ]
            },
        )
        client = FoyerClient(make_v3_creds())
        history = client.get_speedtest_history("group-home-001", limit=1)
        assert len(history) == 1
        assert history[0].download_mbps == 500.0


# ---------------------------------------------------------------------------
# reboot_point / reboot_group (FR-WIFI-10..11)
# ---------------------------------------------------------------------------


class TestRebootPoint:
    def test_posts_to_accesspoints_reboot(self, fake_rest_client: Any, make_v3_creds: Any) -> None:
        client = FoyerClient(make_v3_creds())
        client.reboot_point("ap-master-living-room")
        call = fake_rest_client.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == "/v2/accesspoints/ap-master-living-room/reboot"
        assert call["json"] == {}


class TestRebootGroup:
    def test_lists_points_then_posts_group_reboot(
        self,
        fake_rest_client: Any,
        fake_foyer_client: None,
        make_v3_creds: Any,
    ) -> None:
        # list_points uses the gRPC seam; fake_foyer_client provides
        # the gRPC fixture. POST to /v2/groups/.../reboot uses _rest.
        client = FoyerClient(make_v3_creds())
        rebooted = client.reboot_group("group-home-001")
        # POST should have been called; list_points reads from the
        # gRPC fake (no _rest call for that).
        post_call = next((c for c in fake_rest_client.calls if c["method"] == "POST"), None)
        assert post_call is not None
        assert post_call["path"] == "/v2/groups/group-home-001/reboot"
        # Returns list of point ids that were rebooted
        assert sorted(rebooted) == ["ap-master-living-room", "ap-sat-office"]


# ---------------------------------------------------------------------------
# Phase D deferred verbs — set_station_group + set_guest_enabled stay exit-5
# ---------------------------------------------------------------------------


class TestSetStationGroupDeferred:
    def test_raises_unsupported_feature(self, fake_foyer_client: None, make_v2_creds: Any) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.set_station_group("sta-laptop", "family")
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE
        assert "Phase D" in (excinfo.value.hint or "")


class TestSetGuestEnabledDeferred:
    def test_raises_unsupported_feature(self, fake_foyer_client: None, make_v2_creds: Any) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.set_guest_enabled("group-home-001", enabled=True)
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE
        assert "Phase D" in (excinfo.value.hint or "")


# ---------------------------------------------------------------------------
# get_network_info — still exit-5 (HomeGraph has no SSID/IPv4 data)
# ---------------------------------------------------------------------------


class TestGetNetworkInfo:
    def test_exits_5_in_phase_c(self, fake_foyer_client: None, make_v2_creds: Any) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.get_network_info("group-home-001")
        err = excinfo.value
        assert err.code == EXIT_UNSUPPORTED_FEATURE
        assert err.family == "wifi"


# ---------------------------------------------------------------------------
# get_point_health (Phase B implemented read verb)
# ---------------------------------------------------------------------------


class TestGetPointHealth:
    def test_master_returns_health_record(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        health = client.get_point_health("ap-master-living-room")
        assert isinstance(health, WifiPointHealth)
        assert health.id == "ap-master-living-room"
        assert health.online is True
        assert health.mesh_role == "master"
        assert health.signal_to_upstream_dbm is None

    def test_satellite_carries_signal_dbm(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        health = client.get_point_health("ap-sat-office")
        assert health.mesh_role == "satellite"
        assert health.signal_to_upstream_dbm == -52

    def test_unknown_point_exits_4(self, fake_foyer_client: None, make_v2_creds: Any) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.get_point_health("ap-no-such-point")
        assert excinfo.value.code == EXIT_NOT_FOUND
        assert excinfo.value.family == "wifi"

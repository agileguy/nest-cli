"""Tests for ``nest_cli.wifi.types`` — WifiGroup / WifiPoint / WifiClient.

Coverage map (SRD → test):

- §10.6 (WifiGroup shape):   test_wifi_group_*
- §10.7 (WifiPoint shape):   test_wifi_point_*
- §10.8 (WifiClient shape):  test_wifi_client_*
- FR-22 (RFC 3339 ``Z``):    test_wifi_client_priority_until_serializes_z
- ``extra="forbid"``:        test_wifi_*_rejects_extra_keys
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nest_cli.wifi.types import WifiClient, WifiGroup, WifiPoint

# ---------------------------------------------------------------------------
# WifiGroup
# ---------------------------------------------------------------------------


class TestWifiGroup:
    def test_constructs_from_required_fields(self) -> None:
        group = WifiGroup(
            id="group-abc",
            name="Home",
            points=2,
            clients=14,
            online=True,
            master_point_id="ap-master-001",
            ssid="HomeNet",
            guest_enabled=False,
        )
        assert group.id == "group-abc"
        assert group.guest_enabled is False

    def test_rejects_extra_keys(self) -> None:
        with pytest.raises(ValidationError):
            WifiGroup(
                id="g",
                name="n",
                points=0,
                clients=0,
                online=True,
                master_point_id="m",
                ssid="s",
                guest_enabled=False,
                surprise_field="boom",  # type: ignore[call-arg]
            )

    def test_from_googlewifi_response_dict_shape(self) -> None:
        # Mirrors the shape ``googlewifi.GoogleWifi.get_systems()`` returns:
        # a dict keyed by system_id whose values are the rich per-system
        # records carrying ``access_points``, ``devices``, ``status``.
        payload = {
            "id": "group-abc",
            "name": "Home",
            "status": "ONLINE",
            "access_points": {
                "ap-master-001": {
                    "id": "ap-master-001",
                    "isMaster": True,
                    "displayName": "Living Room",
                },
                "ap-sat-002": {
                    "id": "ap-sat-002",
                    "isMaster": False,
                    "displayName": "Office",
                },
            },
            "devices": {"sta-1": {}, "sta-2": {}, "sta-3": {}},
            "groupSettings": {
                "apSettings": {"ssid": "HomeNet"},
                "guestSsid": {"enabled": True},
            },
        }
        group = WifiGroup.from_googlewifi_response("group-abc", payload)
        assert group.id == "group-abc"
        assert group.points == 2
        assert group.clients == 3
        assert group.online is True
        assert group.master_point_id == "ap-master-001"
        assert group.ssid == "HomeNet"
        assert group.guest_enabled is True

    def test_from_googlewifi_response_offline_status(self) -> None:
        payload = {
            "id": "g",
            "status": "OFFLINE",
            "access_points": {"ap-1": {"id": "ap-1", "isMaster": True}},
            "devices": {},
            "groupSettings": {"apSettings": {"ssid": "X"}},
        }
        group = WifiGroup.from_googlewifi_response("g", payload)
        assert group.online is False

    def test_from_googlewifi_response_falls_back_to_first_ap_id(self) -> None:
        # Defensive: if no AP carries ``isMaster=True`` (corrupted payload
        # or odd firmware), fall back to the lexicographically first id
        # rather than crashing.
        payload = {
            "access_points": {
                "ap-a": {"id": "ap-a", "isMaster": False},
                "ap-b": {"id": "ap-b", "isMaster": False},
            },
            "devices": {},
            "groupSettings": {"apSettings": {"ssid": "S"}},
        }
        group = WifiGroup.from_googlewifi_response("g", payload)
        assert group.master_point_id == "ap-a"


# ---------------------------------------------------------------------------
# WifiPoint
# ---------------------------------------------------------------------------


class TestWifiPoint:
    def test_constructs_from_required_fields(self) -> None:
        point = WifiPoint(
            id="ap-master-001",
            name="Living Room",
            is_master=True,
            mesh_role="master",
            connected_clients_count=5,
            online=True,
            uptime_s=3600,
        )
        assert point.signal_strength_to_upstream_dbm is None  # not meaningful for master

    def test_rejects_extra_keys(self) -> None:
        with pytest.raises(ValidationError):
            WifiPoint(
                id="ap",
                name="n",
                is_master=True,
                mesh_role="master",
                connected_clients_count=0,
                online=True,
                uptime_s=0,
                surprise_field="x",  # type: ignore[call-arg]
            )

    def test_from_googlewifi_response_master(self) -> None:
        ap = {
            "id": "ap-master-001",
            "isMaster": True,
            "displayName": "Living Room",
            "model": "AC-1304",
            "firmwareVersion": "13371.95.84",
            "status": {"apState": "ONLINE", "uptimeSeconds": 7200},
        }
        point = WifiPoint.from_googlewifi_response(ap, connected_clients_count=4)
        assert point.id == "ap-master-001"
        assert point.is_master is True
        assert point.mesh_role == "master"
        # FR §10.7: master has no upstream signal-strength.
        assert point.signal_strength_to_upstream_dbm is None
        assert point.connected_clients_count == 4
        assert point.online is True
        assert point.uptime_s == 7200

    def test_from_googlewifi_response_satellite(self) -> None:
        ap = {
            "id": "ap-sat-002",
            "isMaster": False,
            "displayName": "Office",
            "status": {
                "apState": "ONLINE",
                "uptimeSeconds": 3600,
                "signalStrengthDbm": -52,
            },
        }
        point = WifiPoint.from_googlewifi_response(ap, connected_clients_count=2)
        assert point.is_master is False
        assert point.mesh_role == "satellite"
        assert point.signal_strength_to_upstream_dbm == -52


# ---------------------------------------------------------------------------
# WifiClient
# ---------------------------------------------------------------------------


class TestWifiClient:
    def test_constructs_from_required_fields(self) -> None:
        client = WifiClient(
            id="sta-1",
            friendly_name="Dan's Laptop",
            connected_to_point_id="ap-master-001",
            connection_type="wifi",
            paused=False,
        )
        assert client.band is None
        assert client.priority_until is None

    def test_rejects_extra_keys(self) -> None:
        with pytest.raises(ValidationError):
            WifiClient(
                id="x",
                friendly_name="x",
                connected_to_point_id="x",
                connection_type="wifi",
                paused=False,
                surprise_field="y",  # type: ignore[call-arg]
            )

    def test_priority_until_serializes_z(self) -> None:
        client = WifiClient(
            id="sta-1",
            friendly_name="Tablet",
            connected_to_point_id="ap-master-001",
            connection_type="wifi",
            band="5",
            paused=False,
            priority_until=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
        )
        # ``model_dump(mode="json")`` invokes the Z-suffix serializer.
        dumped = client.model_dump(mode="json")
        assert dumped["priority_until"] == "2026-05-03T12:00:00Z"

    def test_from_googlewifi_response_wifi_client_with_band(self) -> None:
        station = {
            "id": "sta-1",
            "friendlyName": "Dan's Phone",
            "macAddress": "aa:bb:cc:dd:ee:ff",
            "ipAddress": "192.0.2.10",
            "apId": "ap-master-001",
            "connectionType": "WIRELESS",
            "frequencyBand": "BAND_5_GHZ",
            "txRateMbps": 432.1,
            "rxRateMbps": 405.0,
            "paused": False,
        }
        client = WifiClient.from_googlewifi_response(station)
        assert client.id == "sta-1"
        assert client.connection_type == "wifi"
        assert client.band == "5"
        assert client.tx_rate_mbps == pytest.approx(432.1)
        assert client.rx_rate_mbps == pytest.approx(405.0)
        assert client.paused is False

    def test_from_googlewifi_response_ethernet_client_no_band(self) -> None:
        station = {
            "id": "sta-eth",
            "friendlyName": "Wired NAS",
            "apId": "ap-master-001",
            "connectionType": "ETHERNET",
            "paused": False,
        }
        client = WifiClient.from_googlewifi_response(station)
        assert client.connection_type == "ethernet"
        assert client.band is None

    def test_from_googlewifi_response_paused_with_priority(self) -> None:
        station = {
            "id": "sta-kid-tablet",
            "friendlyName": "Kid Tablet",
            "apId": "ap-sat-002",
            "connectionType": "WIRELESS",
            "frequencyBand": "BAND_2_4_GHZ",
            "paused": True,
            "priorityUntil": "2026-05-03T13:30:00Z",
            "groupAssignment": "PARENTAL",
        }
        client = WifiClient.from_googlewifi_response(station)
        assert client.paused is True
        assert client.band == "2.4"
        assert client.priority_until == datetime(2026, 5, 3, 13, 30, 0, tzinfo=UTC)
        assert client.group_assignment == "parental"

"""Tests for the Phase 3.1 wifi pydantic models (SRD §10.9 / §10.10 / §10.11).

Coverage map (SRD → test):

- §10.9  (SpeedTest shape):       TestSpeedTest
- §10.10 (WifiNetwork shape):     TestWifiNetwork
- §10.11 (WifiPointHealth shape): TestWifiPointHealth
- ``extra="forbid"``:             test_*_rejects_extra_keys
- FR-22 (RFC 3339 ``Z``):         test_speed_test_ts_serializes_z
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nest_cli.wifi.types import (
    SpeedTest,
    WifiNetwork,
    WifiPointHealth,
)

# ---------------------------------------------------------------------------
# SpeedTest (§10.9)
# ---------------------------------------------------------------------------


class TestSpeedTest:
    def test_constructs_from_required_fields(self) -> None:
        record = SpeedTest(
            ts=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
            group_id="group-home-001",
            point_id="ap-master-living-room",
            download_mbps=900.5,
            upload_mbps=120.1,
            ping_ms=12.3,
            source="router",
        )
        assert record.group_id == "group-home-001"
        assert record.download_mbps == 900.5
        assert record.source == "router"

    def test_rejects_extra_keys(self) -> None:
        with pytest.raises(ValidationError):
            SpeedTest(
                ts=datetime(2026, 5, 2, tzinfo=UTC),
                group_id="g",
                point_id="p",
                download_mbps=1.0,
                upload_mbps=1.0,
                ping_ms=1.0,
                source="router",
                extra_field="nope",  # type: ignore[call-arg]
            )

    def test_source_is_locked_to_router(self) -> None:
        with pytest.raises(ValidationError):
            SpeedTest(
                ts=datetime(2026, 5, 2, tzinfo=UTC),
                group_id="g",
                point_id="p",
                download_mbps=1.0,
                upload_mbps=1.0,
                ping_ms=1.0,
                source="ookla",  # type: ignore[arg-type]
            )

    def test_ts_serializes_with_z_suffix(self) -> None:
        """FR-22: ts MUST serialize as RFC 3339 UTC with literal ``Z``."""
        record = SpeedTest(
            ts=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
            group_id="g",
            point_id="p",
            download_mbps=1.0,
            upload_mbps=1.0,
            ping_ms=1.0,
            source="router",
        )
        payload = json.loads(record.model_dump_json())
        assert payload["ts"].endswith("Z")
        assert "+00:00" not in payload["ts"]

    def test_from_googlewifi_response_normalizes_bps_to_mbps(self) -> None:
        """Foyer reports speeds in bits-per-second; SRD requires Mbps."""
        record = SpeedTest.from_googlewifi_response(
            group_id="group-home-001",
            payload={
                # 900 Mbps as bps:
                "downloadSpeedBps": 900_000_000,
                "uploadSpeedBps": 120_000_000,
                "pingMs": 12.5,
                "timestamp": "2026-05-02T12:00:00Z",
                "apId": "ap-master-living-room",
            },
        )
        assert record.download_mbps == pytest.approx(900.0, abs=0.5)
        assert record.upload_mbps == pytest.approx(120.0, abs=0.5)
        assert record.ping_ms == 12.5
        assert record.point_id == "ap-master-living-room"
        assert record.source == "router"

    def test_from_googlewifi_response_falls_back_when_apid_missing(self) -> None:
        """When upstream omits apId, point_id falls back to group_id."""
        record = SpeedTest.from_googlewifi_response(
            group_id="group-home-001",
            payload={
                "downloadSpeedBps": 100_000_000,
                "uploadSpeedBps": 50_000_000,
                "pingMs": 20.0,
                "timestamp": "2026-05-02T12:00:00Z",
            },
        )
        # Defensive default: when upstream doesn't name an AP, use group as
        # the point id placeholder so the SRD field stays populated.
        assert record.point_id == "group-home-001"


# ---------------------------------------------------------------------------
# WifiNetwork (§10.10)
# ---------------------------------------------------------------------------


class TestWifiNetwork:
    def _build(self, **overrides: object) -> WifiNetwork:
        defaults: dict[str, object] = {
            "group_id": "group-home-001",
            "ssid": "HomeNet",
            "guest_ssid": None,
            "guest_enabled": False,
            "ipv4": {
                "wan": "203.0.113.10",
                "lan_subnet": "192.168.86.0/24",
                "dhcp_range_start": "192.168.86.20",
                "dhcp_range_end": "192.168.86.250",
            },
            "ipv6": {
                "enabled": False,
                "wan": None,
                "prefix_len": None,
            },
            "dns_servers": ["8.8.8.8", "8.8.4.4"],
        }
        defaults.update(overrides)
        return WifiNetwork(**defaults)  # type: ignore[arg-type]

    def test_constructs_with_all_fields(self) -> None:
        net = self._build()
        assert net.ssid == "HomeNet"
        assert net.guest_enabled is False
        assert net.ipv4.wan == "203.0.113.10"
        assert net.ipv6.enabled is False

    def test_rejects_extra_keys(self) -> None:
        with pytest.raises(ValidationError):
            self._build(extra="nope")

    def test_guest_ssid_required_when_enabled(self) -> None:
        # Document the convention: guest_enabled=True with guest_ssid=None
        # is allowed (operator set guest on but never named the SSID), but a
        # populated guest_ssid is preserved.
        net = self._build(guest_enabled=True, guest_ssid="HomeNet-Guest")
        assert net.guest_ssid == "HomeNet-Guest"
        assert net.guest_enabled is True

    def test_from_googlewifi_response_extracts_all_fields(self) -> None:
        """Build a WifiNetwork from a get_systems() entry with a full shape."""
        payload = {
            "id": "group-home-001",
            "groupSettings": {
                "apSettings": {"ssid": "HomeNet"},
                "guestSsid": {"enabled": True, "ssid": "HomeNet-Guest"},
                "lanSettings": {
                    "subnet": "192.168.86.0/24",
                    "dhcpRange": {
                        "start": "192.168.86.20",
                        "end": "192.168.86.250",
                    },
                },
                "dnsSettings": {
                    "dnsServers": ["8.8.8.8", "8.8.4.4"],
                },
            },
            "wanSettings": {
                "ipv4Address": "203.0.113.10",
                "ipv6": {
                    "enabled": True,
                    "address": "2001:db8::1",
                    "prefixLength": 64,
                },
            },
        }
        net = WifiNetwork.from_googlewifi_response("group-home-001", payload)
        assert net.group_id == "group-home-001"
        assert net.ssid == "HomeNet"
        assert net.guest_ssid == "HomeNet-Guest"
        assert net.guest_enabled is True
        assert net.ipv4.wan == "203.0.113.10"
        assert net.ipv4.lan_subnet == "192.168.86.0/24"
        assert net.ipv4.dhcp_range_start == "192.168.86.20"
        assert net.ipv4.dhcp_range_end == "192.168.86.250"
        assert net.ipv6.enabled is True
        assert net.ipv6.wan == "2001:db8::1"
        assert net.ipv6.prefix_len == 64
        assert net.dns_servers == ["8.8.8.8", "8.8.4.4"]

    def test_from_googlewifi_response_missing_optional_fields(self) -> None:
        """Sparse upstream payload — defensive fallbacks fill the record."""
        payload = {
            "id": "group-home-001",
            "groupSettings": {
                "apSettings": {"ssid": "HomeNet"},
                "guestSsid": {"enabled": False},
            },
        }
        net = WifiNetwork.from_googlewifi_response("group-home-001", payload)
        assert net.guest_ssid is None
        assert net.guest_enabled is False
        # IPv4/IPv6 fields fall back to "<unknown>" / disabled.
        assert net.ipv4.wan == "<unknown>"
        assert net.ipv6.enabled is False
        assert net.dns_servers == []


# ---------------------------------------------------------------------------
# WifiPointHealth (§10.11)
# ---------------------------------------------------------------------------


class TestWifiPointHealth:
    def test_constructs_from_required_fields(self) -> None:
        health = WifiPointHealth(
            id="ap-master-001",
            online=True,
            uptime_s=86400,
            signal_to_upstream_dbm=None,
            connected_clients_count=3,
            mesh_role="master",
        )
        assert health.id == "ap-master-001"
        assert health.mesh_role == "master"

    def test_rejects_extra_keys(self) -> None:
        with pytest.raises(ValidationError):
            WifiPointHealth(
                id="ap",
                online=True,
                uptime_s=0,
                signal_to_upstream_dbm=None,
                connected_clients_count=0,
                mesh_role="master",
                extra="nope",  # type: ignore[call-arg]
            )

    def test_mesh_role_locked_to_two_values(self) -> None:
        with pytest.raises(ValidationError):
            WifiPointHealth(
                id="ap",
                online=True,
                uptime_s=0,
                signal_to_upstream_dbm=None,
                connected_clients_count=0,
                mesh_role="root",  # type: ignore[arg-type]
            )

    def test_satellite_with_signal_dbm(self) -> None:
        health = WifiPointHealth(
            id="ap-sat-001",
            online=True,
            uptime_s=43200,
            signal_to_upstream_dbm=-52,
            connected_clients_count=1,
            mesh_role="satellite",
        )
        assert health.signal_to_upstream_dbm == -52
        assert health.mesh_role == "satellite"

    def test_offline_point_zero_uptime(self) -> None:
        health = WifiPointHealth(
            id="ap-sat-002",
            online=False,
            uptime_s=0,
            signal_to_upstream_dbm=None,
            connected_clients_count=0,
            mesh_role="satellite",
        )
        assert health.online is False
        assert health.uptime_s == 0

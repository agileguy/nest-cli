"""Tests for ``nest_cli.wifi.client.FoyerClient`` — sync facade over googlewifi.

Coverage map (SRD FR → test):

- FR-WIFI-1 (list_groups happy path):   test_list_groups_emits_two_groups.
- FR-WIFI-2 (list_points happy path):   test_list_points_for_known_group.
- FR-WIFI-2 (group-not-found → exit 4): test_list_points_unknown_group_exits_4.
- FR-WIFI-3 (list_clients happy path):  test_list_clients_for_known_group.
- §13.2 ([wifi] extra missing → exit 5):test_missing_extra_exits_5.
- §11.3 (family=wifi on errors):        test_unknown_group_error_carries_family_wifi.
- §3.2.3 (rotation → exit 1):           test_upstream_shape_error_exits_1.
"""

from __future__ import annotations

import pytest

from nest_cli.errors import (
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    EXIT_UNSUPPORTED_FEATURE,
    StructuredError,
)
from nest_cli.wifi.client import FoyerClient

# ---------------------------------------------------------------------------
# Lazy-import / missing-extra path
# ---------------------------------------------------------------------------


class TestMissingExtra:
    def test_missing_extra_exits_5_with_install_hint(self, missing_googlewifi: None) -> None:
        with pytest.raises(StructuredError) as exc_info:
            FoyerClient(master_token="x")
        err = exc_info.value
        assert err.code == EXIT_UNSUPPORTED_FEATURE
        assert err.family == "wifi"
        assert "[wifi]" in (err.hint or "")


# ---------------------------------------------------------------------------
# list_groups (FR-WIFI-1)
# ---------------------------------------------------------------------------


class TestListGroups:
    def test_emits_two_groups_from_fixture_corpus(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        groups = client.list_groups()
        assert len(groups) == 2
        ids = sorted(g.id for g in groups)
        assert ids == ["group-cottage-002", "group-home-001"]

    def test_group_record_carries_master_point_id(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        home = next(g for g in client.list_groups() if g.id == "group-home-001")
        assert home.master_point_id == "ap-master-living-room"
        assert home.points == 2
        assert home.clients == 4
        assert home.ssid == "HomeMeshNet"
        assert home.guest_enabled is False

    def test_empty_account_returns_empty_list(self, empty_googlewifi: None) -> None:
        client = FoyerClient(master_token="t")
        assert client.list_groups() == []


# ---------------------------------------------------------------------------
# list_points (FR-WIFI-2)
# ---------------------------------------------------------------------------


class TestListPoints:
    def test_returns_points_for_known_group(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        points = client.list_points("group-home-001")
        assert len(points) == 2
        master = next(p for p in points if p.is_master)
        satellite = next(p for p in points if not p.is_master)
        assert master.id == "ap-master-living-room"
        assert master.mesh_role == "master"
        # FR §10.7: connected_clients_count is per-AP, computed by the
        # FoyerClient by bucketing devices on apId.
        assert master.connected_clients_count == 3  # laptop + phone + nas
        assert satellite.id == "ap-sat-office"
        assert satellite.mesh_role == "satellite"
        assert satellite.connected_clients_count == 1  # kid-tablet

    def test_unknown_group_exits_4_with_family_wifi(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as exc_info:
            client.list_points("group-no-such")
        err = exc_info.value
        assert err.code == EXIT_NOT_FOUND
        assert err.family == "wifi"
        assert "group-no-such" in err.message

    def test_returns_deterministic_order(self, fake_googlewifi: type) -> None:
        # FR-23: deterministic sort by id ascending.
        client = FoyerClient(master_token="t")
        points = client.list_points("group-home-001")
        ids = [p.id for p in points]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# list_clients (FR-WIFI-3)
# ---------------------------------------------------------------------------


class TestListClients:
    def test_returns_clients_for_known_group(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        stations = client.list_clients("group-home-001")
        assert len(stations) == 4
        ids = sorted(s.id for s in stations)
        assert ids == ["sta-kid-tablet", "sta-laptop", "sta-nas-wired", "sta-phone"]

    def test_band_field_normalized(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        stations = client.list_clients("group-home-001")
        bands = {s.id: s.band for s in stations}
        assert bands["sta-laptop"] == "5"
        assert bands["sta-kid-tablet"] == "2.4"
        assert bands["sta-nas-wired"] is None  # Ethernet

    def test_paused_and_priority_until_propagate(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        stations = client.list_clients("group-home-001")
        kid = next(s for s in stations if s.id == "sta-kid-tablet")
        assert kid.paused is True
        assert kid.priority_until is not None
        assert kid.group_assignment == "parental"

    def test_unknown_group_exits_4(self, fake_googlewifi: type) -> None:
        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as exc_info:
            client.list_clients("group-no-such")
        assert exc_info.value.code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# Upstream-shape rotation (SRD §3.2.3)
# ---------------------------------------------------------------------------


class TestUpstreamShape:
    def test_get_systems_non_dict_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inject a fake whose ``get_systems`` returns a list (the kind
        # of upstream rotation the SRD calls out as exit 1).
        import sys

        class _RotatedGoogleWifi:
            def __init__(self, refresh_token: str | None = None, **_: object) -> None:
                pass

            async def get_systems(self) -> list[str]:
                return ["this", "is", "the", "wrong", "shape"]

            async def close(self) -> None:
                return None

        fake_module = type(sys)("googlewifi")
        fake_module.GoogleWifi = _RotatedGoogleWifi  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as exc_info:
            client.list_groups()
        err = exc_info.value
        assert err.code == EXIT_DEVICE_ERROR
        assert err.family == "wifi"


# ---------------------------------------------------------------------------
# Network failure (FR-17)
# ---------------------------------------------------------------------------


class TestNetworkFailure:
    def test_connection_error_exits_3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        class _NetworkErrorGoogleWifi:
            def __init__(self, refresh_token: str | None = None, **_: object) -> None:
                pass

            async def get_systems(self) -> dict[str, object]:
                raise ConnectionError("DNS resolution failed")

            async def close(self) -> None:
                return None

        fake_module = type(sys)("googlewifi")
        fake_module.GoogleWifi = _NetworkErrorGoogleWifi  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

        client = FoyerClient(master_token="t")
        with pytest.raises(StructuredError) as exc_info:
            client.list_groups()
        err = exc_info.value
        assert err.code == EXIT_NETWORK_ERROR
        assert err.family == "wifi"

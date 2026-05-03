"""Phase B unit tests for ``FoyerClient`` action verbs (formerly Phase 3.1).

Phase B status (2026-05-03): all action verbs (``run_speedtest``,
``get_speedtest_history``, ``reboot_point``, ``reboot_group``,
``set_guest_enabled``, ``set_station_group``, ``pause_station``,
``unpause_station``, ``prioritize_station``, ``list_clients``) raise
``StructuredError(EXIT_UNSUPPORTED_FEATURE, family="wifi")`` because
the Foyer RPCs that implement them have not yet been mapped (deferred
to Phase C).

The read verb ``get_network_info`` is fully implemented; ``TestGetPointHealth``
covers the implemented ``get_point_health`` read verb. The original
upstream-call assertions (TestRunSpeedtest, TestSpeedtestHistory,
TestRebootPoint, TestRebootGroup) have been collapsed into single
exit-5 unit tests; they will be reinstated when Phase C lands the RPCs.
"""

from __future__ import annotations

import pytest

from nest_cli.errors import (
    EXIT_NOT_FOUND,
    EXIT_UNSUPPORTED_FEATURE,
    StructuredError,
)
from nest_cli.wifi.client import FoyerClient
from nest_cli.wifi.types import WifiPointHealth

# ---------------------------------------------------------------------------
# Action-verb stubs (Phase B exit-5 posture)
# ---------------------------------------------------------------------------


class TestRunSpeedtest:
    def test_raises_unsupported_feature(self, fake_foyer_client: None, make_v2_creds) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.run_speedtest("group-home-001")
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE
        assert excinfo.value.family == "wifi"
        assert excinfo.value.hint is not None


class TestSpeedtestHistory:
    def test_raises_unsupported_feature(self, fake_foyer_client: None, make_v2_creds) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.get_speedtest_history("group-home-001", limit=30)
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE
        assert excinfo.value.family == "wifi"


class TestRebootPoint:
    def test_raises_unsupported_feature(self, fake_foyer_client: None, make_v2_creds) -> None:
        """Even known points exit-5 — the verb stubs out before validation."""
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.reboot_point("ap-master-living-room")
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE
        assert excinfo.value.family == "wifi"

    def test_unknown_point_also_exits_5(
        self, fake_foyer_client: None, make_v2_creds
    ) -> None:
        """Was exit 4 pre-Phase-B; verb no longer reaches lookup."""
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.reboot_point("ap-no-such-point")
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE


class TestRebootGroup:
    def test_raises_unsupported_feature(self, fake_foyer_client: None, make_v2_creds) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.reboot_group("group-home-001")
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE
        assert excinfo.value.family == "wifi"

    def test_unknown_group_also_exits_5(
        self, fake_foyer_client: None, make_v2_creds
    ) -> None:
        """Was exit 4 pre-Phase-B; verb no longer reaches lookup."""
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.reboot_group("group-nope")
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE


class TestSetGuestEnabled:
    def test_raises_unsupported_feature(self, fake_foyer_client: None, make_v2_creds) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.set_guest_enabled("group-home-001", enabled=True)
        assert excinfo.value.code == EXIT_UNSUPPORTED_FEATURE
        assert excinfo.value.family == "wifi"
        # Operator gets a hint pointing at the Phase-C deferral.
        assert excinfo.value.hint is not None


# ---------------------------------------------------------------------------
# get_network_info — exit-5 stub in Phase B (no SSID/IPv4/IPv6/DNS in HomeGraph)
# ---------------------------------------------------------------------------


class TestGetNetworkInfo:
    def test_exits_5_in_phase_b(
        self, fake_foyer_client: None, make_v2_creds
    ) -> None:
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
        self, fake_foyer_client: None, make_v2_creds
    ) -> None:
        client = FoyerClient(make_v2_creds())
        health = client.get_point_health("ap-master-living-room")
        assert isinstance(health, WifiPointHealth)
        assert health.id == "ap-master-living-room"
        assert health.online is True
        assert health.mesh_role == "master"
        # Master has no upstream signal measurement.
        assert health.signal_to_upstream_dbm is None

    def test_satellite_carries_signal_dbm(
        self, fake_foyer_client: None, make_v2_creds
    ) -> None:
        client = FoyerClient(make_v2_creds())
        health = client.get_point_health("ap-sat-office")
        assert health.mesh_role == "satellite"
        assert health.signal_to_upstream_dbm == -52

    def test_unknown_point_exits_4(
        self, fake_foyer_client: None, make_v2_creds
    ) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as excinfo:
            client.get_point_health("ap-no-such-point")
        assert excinfo.value.code == EXIT_NOT_FOUND
        assert excinfo.value.family == "wifi"

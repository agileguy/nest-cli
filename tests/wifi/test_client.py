"""Tests for ``nest_cli.wifi.client.FoyerClient`` (Phase B, 2026-05-03).

The Phase B FoyerClient drops googlewifi + glocaltokens and talks to
Foyer directly via gpsoauth + gRPC. Tests fake at the
``FoyerClient._fetch_systems`` seam so we don't have to spin up a real
gRPC channel; integration with gpsoauth + the gRPC stubs is covered by
the dedicated ``TestAccessTokenRefresh`` class which patches those
imports directly.

Coverage map (SRD FR → test):

- FR-WIFI-1 (list_groups happy path):     test_emits_two_groups_from_fixture_corpus.
- FR-WIFI-2 (list_points happy path):     test_returns_points_for_known_group.
- FR-WIFI-2 (group-not-found → exit 4):   test_unknown_group_exits_4_with_family_wifi.
- FR-WIFI-13 (network info):              test_get_network_info_emits_record.
- FR-WIFI-15 (point health):              test_get_point_health_emits_record.
- FR-WIFI-3..15 (action verbs deferred):  test_action_verbs_exit_5.
- §13.2 ([wifi] extra missing → exit 5):  test_missing_extra_exits_5_with_install_hint.
- §3.2.3 (rotation → exit 1):             test_get_systems_non_dict_exits_1.
- FR-17 (network failure → exit 3):       test_connection_error_exits_3.
- Phase B (gpsoauth refresh + cache):     test_access_token_caching, etc.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    EXIT_UNSUPPORTED_FEATURE,
    StructuredError,
)
from nest_cli.wifi.client import (
    ACCESS_TOKEN_APP_NAME,
    ACCESS_TOKEN_CLIENT_SIGNATURE,
    ACCESS_TOKEN_DURATION_S,
    ACCESS_TOKEN_SERVICE,
    ACCESS_TOKEN_SKEW_S,
    FoyerClient,
)

# ---------------------------------------------------------------------------
# Lazy-import / missing-extra path
# ---------------------------------------------------------------------------


class TestMissingExtra:
    def test_missing_extra_exits_5_with_install_hint(
        self, missing_extras: None, make_v2_creds: Any
    ) -> None:
        with pytest.raises(StructuredError) as exc_info:
            FoyerClient(make_v2_creds())
        err = exc_info.value
        assert err.code == EXIT_UNSUPPORTED_FEATURE
        assert err.family == "wifi"
        assert "[wifi]" in (err.hint or "")


# ---------------------------------------------------------------------------
# list_groups (FR-WIFI-1)
# ---------------------------------------------------------------------------


class TestListGroups:
    def test_emits_two_groups_from_fixture_corpus(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        groups = client.list_groups()
        assert len(groups) == 2
        ids = sorted(g.id for g in groups)
        assert ids == ["group-cottage-002", "group-home-001"]

    def test_group_record_carries_master_point_id(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        home = next(g for g in client.list_groups() if g.id == "group-home-001")
        assert home.master_point_id == "ap-master-living-room"
        assert home.points == 2
        assert home.clients == 4
        assert home.ssid == "HomeMeshNet"
        assert home.guest_enabled is False

    def test_empty_account_returns_empty_list(
        self, empty_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        assert client.list_groups() == []


# ---------------------------------------------------------------------------
# list_points (FR-WIFI-2)
# ---------------------------------------------------------------------------


class TestListPoints:
    def test_returns_points_for_known_group(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        points = client.list_points("group-home-001")
        assert len(points) == 2
        master = next(p for p in points if p.is_master)
        satellite = next(p for p in points if not p.is_master)
        assert master.id == "ap-master-living-room"
        assert master.mesh_role == "master"
        assert master.connected_clients_count == 3
        assert satellite.id == "ap-sat-office"
        assert satellite.mesh_role == "satellite"
        assert satellite.connected_clients_count == 1

    def test_unknown_group_exits_4_with_family_wifi(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as exc_info:
            client.list_points("group-no-such")
        err = exc_info.value
        assert err.code == EXIT_NOT_FOUND
        assert err.family == "wifi"
        assert "group-no-such" in err.message

    def test_returns_deterministic_order(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        points = client.list_points("group-home-001")
        ids = [p.id for p in points]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# get_network_info (FR-WIFI-13) — exit-5 stub in Phase B
#
# The HomeGraph protobuf carries no SSID / IPv4 / IPv6 / DNS data,
# so returning a placeholder record would actively mislead operators.
# Phase C will land the real Foyer network-info RPC.
# ---------------------------------------------------------------------------


class TestNetworkInfo:
    def test_get_network_info_exits_5_in_phase_b(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as exc_info:
            client.get_network_info("group-home-001")
        err = exc_info.value
        assert err.code == EXIT_UNSUPPORTED_FEATURE
        assert err.family == "wifi"
        assert err.details and err.details.get("group_id") == "group-home-001"


# ---------------------------------------------------------------------------
# get_point_health (FR-WIFI-15)
# ---------------------------------------------------------------------------


class TestPointHealth:
    def test_get_point_health_emits_record(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        health = client.get_point_health("ap-master-living-room")
        assert health.id == "ap-master-living-room"
        assert health.mesh_role == "master"
        assert health.online is True

    def test_get_point_health_unknown_point_exits_4(
        self, fake_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as exc_info:
            client.get_point_health("ap-no-such")
        assert exc_info.value.code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# Action verbs — Phase B exit-5 stubs (deferred to Phase C)
# ---------------------------------------------------------------------------


class TestActionVerbsAreDeferred:
    """All action verbs exit-5 with family=wifi in Phase B.

    The CLI surface ships so operators can wire scripts; Phase C will
    map each verb to its specific Foyer RPC.
    """

    def _client(self, make_v2_creds: Any) -> FoyerClient:
        return FoyerClient(make_v2_creds())

    @pytest.mark.parametrize(
        ("verb_call",),
        [
            (lambda c: c.list_clients("group-home-001"),),
            (lambda c: c.pause_station("sta-laptop"),),
            (lambda c: c.unpause_station("sta-laptop"),),
            (lambda c: c.prioritize_station("sta-laptop", 60),),
            (lambda c: c.set_station_group("sta-laptop", "family"),),
            (lambda c: c.run_speedtest("group-home-001"),),
            (lambda c: c.get_speedtest_history("group-home-001", limit=10),),
            (lambda c: c.reboot_point("ap-master-living-room"),),
            (lambda c: c.reboot_group("group-home-001"),),
            (lambda c: c.set_guest_enabled("group-home-001", enabled=True),),
        ],
    )
    def test_verb_exits_5_with_family_wifi(
        self,
        verb_call: Any,
        fake_foyer_client: None,
        make_v2_creds: Any,
    ) -> None:
        client = self._client(make_v2_creds)
        with pytest.raises(StructuredError) as exc_info:
            verb_call(client)
        err = exc_info.value
        assert err.code == EXIT_UNSUPPORTED_FEATURE
        assert err.family == "wifi"


# ---------------------------------------------------------------------------
# Upstream-shape rotation (SRD §3.2.3)
# ---------------------------------------------------------------------------


class TestUpstreamShape:
    def test_get_systems_non_dict_exits_1(
        self, rotated_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as exc_info:
            client.list_groups()
        err = exc_info.value
        assert err.code == EXIT_DEVICE_ERROR
        assert err.family == "wifi"


# ---------------------------------------------------------------------------
# Network failure (FR-17)
# ---------------------------------------------------------------------------


class TestNetworkFailure:
    def test_connection_error_exits_3(
        self, network_error_foyer_client: None, make_v2_creds: Any
    ) -> None:
        client = FoyerClient(make_v2_creds())
        with pytest.raises(StructuredError) as exc_info:
            client.list_groups()
        err = exc_info.value
        assert err.code == EXIT_NETWORK_ERROR
        assert err.family == "wifi"


# ---------------------------------------------------------------------------
# Access-token mint + refresh + cache (Phase B core)
# ---------------------------------------------------------------------------


class TestAccessTokenRefresh:
    """Patch ``gpsoauth.perform_oauth`` directly to verify the refresh path.

    These tests exercise the real ``_refresh_access_token`` body (no
    ``_patch_skip_extras_check`` here — the constructor lazy-import path
    runs unaltered, and gpsoauth is stubbed in-place via mock.patch).
    """

    def test_refresh_calls_gpsoauth_with_correct_constants(
        self, make_v2_creds: Any
    ) -> None:
        creds = make_v2_creds(
            google_account_email="me@example.com",
            master_token="aas_et/test-master-token",
            android_id="0123456789abcdef",
        )
        with patch("gpsoauth.perform_oauth") as fake_oauth:
            fake_oauth.return_value = {"Auth": "ya29.test-access-token"}
            client = FoyerClient(creds)
            token = client._refresh_access_token()

        assert token == "ya29.test-access-token"
        fake_oauth.assert_called_once_with(
            "me@example.com",
            "aas_et/test-master-token",
            "0123456789abcdef",
            app=ACCESS_TOKEN_APP_NAME,
            service=ACCESS_TOKEN_SERVICE,
            client_sig=ACCESS_TOKEN_CLIENT_SIGNATURE,
        )

    def test_refresh_caches_token_until_skew_window(
        self, make_v2_creds: Any
    ) -> None:
        creds = make_v2_creds()
        with patch("gpsoauth.perform_oauth") as fake_oauth:
            fake_oauth.return_value = {"Auth": "tok"}
            client = FoyerClient(creds)
            client._ensure_access_token()
            client._ensure_access_token()
            client._ensure_access_token()
        # Three calls, only one mint — token cached.
        assert fake_oauth.call_count == 1

    def test_refresh_re_mints_after_expiry(self, make_v2_creds: Any) -> None:
        creds = make_v2_creds()
        with patch("gpsoauth.perform_oauth") as fake_oauth:
            fake_oauth.return_value = {"Auth": "tok"}
            client = FoyerClient(creds)
            client._ensure_access_token()
            # Simulate the cached expiry having passed.
            client._access_token_expiry = time.time() - 1.0
            client._ensure_access_token()
        assert fake_oauth.call_count == 2

    def test_skew_window_is_applied(self, make_v2_creds: Any) -> None:
        """The cached expiry sits ACCESS_TOKEN_SKEW_S before real expiry."""
        creds = make_v2_creds()
        with patch("gpsoauth.perform_oauth") as fake_oauth:
            fake_oauth.return_value = {"Auth": "tok"}
            client = FoyerClient(creds)
            t_before = time.time()
            client._refresh_access_token()
            t_after = time.time()
        expected_lower = t_before + ACCESS_TOKEN_DURATION_S - ACCESS_TOKEN_SKEW_S
        expected_upper = t_after + ACCESS_TOKEN_DURATION_S - ACCESS_TOKEN_SKEW_S
        assert expected_lower <= client._access_token_expiry <= expected_upper

    def test_missing_auth_key_exits_2_with_hint(self, make_v2_creds: Any) -> None:
        creds = make_v2_creds()
        with patch("gpsoauth.perform_oauth") as fake_oauth:
            fake_oauth.return_value = {"Error": "BadAuthentication"}
            client = FoyerClient(creds)
            with pytest.raises(StructuredError) as exc_info:
                client._refresh_access_token()
        err = exc_info.value
        assert err.code == EXIT_AUTH_ERROR
        assert err.family == "wifi"
        assert "BadAuthentication" in err.message
        assert "auth wifi-setup --overwrite" in (err.hint or "")

    def test_network_error_during_oauth_exits_3(self, make_v2_creds: Any) -> None:
        creds = make_v2_creds()
        with patch("gpsoauth.perform_oauth") as fake_oauth:
            fake_oauth.side_effect = ConnectionError("DNS resolution failed")
            client = FoyerClient(creds)
            with pytest.raises(StructuredError) as exc_info:
                client._refresh_access_token()
        assert exc_info.value.code == EXIT_NETWORK_ERROR

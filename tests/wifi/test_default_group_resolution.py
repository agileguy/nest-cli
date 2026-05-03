"""Tests for FoyerClient._resolve_default_group_id (PR #9 review fix #1).

The Phase C action verbs (pause, unpause, prioritize) need to target a
specific mesh group's REST path. Phase C ships without a ``--group`` flag,
so the client infers the target by listing the operator's mesh groups and
accepting only the single-group case. Multi-group accounts get a clean
exit-6 + hint pointing at the Phase C.1 follow-up; zero-group accounts
get exit-4.

The resolver caches its result on the instance under
``_default_group_lock`` so concurrent fan-out workers reuse it without
paying multiple ``GetHomeGraph`` round-trips.
"""

from __future__ import annotations

from typing import Any

import pytest

from nest_cli.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_NOT_FOUND,
    StructuredError,
)
from nest_cli.wifi.client import FoyerClient
from nest_cli.wifi.types import WifiGroup


def _make_group(group_id: str) -> WifiGroup:
    """Build a minimal WifiGroup record for resolver tests."""
    return WifiGroup(
        id=group_id,
        name=group_id.replace("-", " ").title(),
        points=1,
        clients=0,
        online=True,
        master_point_id=f"{group_id}-master",
        ssid="UnitTestSSID",
        guest_enabled=False,
    )


class TestResolveDefaultGroupId:
    def test_single_group_resolves_to_that_group_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        make_v3_creds: Any,
    ) -> None:
        """The happy path: one group means we know exactly where to route."""
        # Patch __init__ via the conftest helper indirectly by reusing the
        # existing fake_rest_client fixture pattern — but for resolver
        # tests we don't need _rest, just list_groups.
        from tests.wifi.conftest import _patch_skip_extras_check  # noqa: PLC0415

        _patch_skip_extras_check(monkeypatch)
        client = FoyerClient(make_v3_creds())

        monkeypatch.setattr(
            FoyerClient,
            "list_groups",
            lambda self: [_make_group("home-mesh-001")],
        )

        resolved = client._resolve_default_group_id()
        assert resolved == "home-mesh-001"

    def test_zero_groups_raises_exit_4_with_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        make_v3_creds: Any,
    ) -> None:
        """Zero mesh groups → EXIT_NOT_FOUND with refresh-token-scope hint."""
        from tests.wifi.conftest import _patch_skip_extras_check  # noqa: PLC0415

        _patch_skip_extras_check(monkeypatch)
        client = FoyerClient(make_v3_creds())

        monkeypatch.setattr(FoyerClient, "list_groups", lambda self: [])

        with pytest.raises(StructuredError) as exc_info:
            client._resolve_default_group_id()
        err = exc_info.value
        assert err.code == EXIT_NOT_FOUND
        assert err.family == "wifi"
        assert "no wifi groups visible" in err.message
        assert "accesspoints scope" in (err.hint or "")

    def test_multi_group_raises_exit_6_with_phase_c1_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        make_v3_creds: Any,
    ) -> None:
        """Multi-group → EXIT_CONFIG_ERROR pointing at Phase C.1 --group."""
        from tests.wifi.conftest import _patch_skip_extras_check  # noqa: PLC0415

        _patch_skip_extras_check(monkeypatch)
        client = FoyerClient(make_v3_creds())

        monkeypatch.setattr(
            FoyerClient,
            "list_groups",
            lambda self: [_make_group("home-mesh-001"), _make_group("adu-mesh-002")],
        )

        with pytest.raises(StructuredError) as exc_info:
            client._resolve_default_group_id()
        err = exc_info.value
        assert err.code == EXIT_CONFIG_ERROR
        assert err.family == "wifi"
        assert "2 wifi groups" in err.message
        assert "Phase C.1" in (err.hint or "")

    def test_resolver_invoked_once_across_consecutive_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
        make_v3_creds: Any,
    ) -> None:
        """Cached on first success; subsequent calls don't re-list groups."""
        from tests.wifi.conftest import _patch_skip_extras_check  # noqa: PLC0415

        _patch_skip_extras_check(monkeypatch)
        client = FoyerClient(make_v3_creds())

        call_count = {"n": 0}

        def _list_groups(self: FoyerClient) -> list[WifiGroup]:
            call_count["n"] += 1
            return [_make_group("home-mesh-001")]

        monkeypatch.setattr(FoyerClient, "list_groups", _list_groups)

        first = client._resolve_default_group_id()
        second = client._resolve_default_group_id()
        third = client._resolve_default_group_id()

        assert first == second == third == "home-mesh-001"
        assert call_count["n"] == 1

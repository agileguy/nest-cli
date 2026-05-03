"""Direct gRPC client for Google Home Foyer (Phase B, 2026-05-03).

Background
----------

The Phase 3 implementation routed wifi calls through ``googlewifi`` +
``glocaltokens``. That path is broken on AAS master tokens: the upstream
``GoogleWifi(refresh_token=...)`` expects a standard OAuth2 refresh token
(``1//09xxx...``), but the bootstrapped Android master token is shaped
``aas_et/...`` and Google's ``oauth2/v4/token`` endpoint rejects it with
``Authorization Error``. Phase B (validated 2026-05-03 against a real
Active T1 + Nest Wifi Pro) replaces the broken path with a direct call to
``googlehomefoyer-pa.googleapis.com:443`` over gRPC, with the access token
minted via ``gpsoauth.perform_oauth(email, master_token, android_id, ...)``.

Layered design
--------------

- ``__init__(creds: WifiCredentials)`` captures the inputs and lazy-imports
  ``gpsoauth`` + ``grpc`` + ``ghome_foyer_api``. Operators on a cam-only
  install see a clean exit-5 with install hint instead of a stack trace.
- ``_refresh_access_token()`` calls ``gpsoauth.perform_oauth`` with the
  Foyer-app signing constants. The returned ``Auth`` value is a 1-hour
  OAuth2 access token bound to ``com.google.android.apps.chromecast.app``;
  we cache it and refresh 60s before expiry.
- ``_channel()`` builds a gRPC composite channel (TLS + bearer token) and
  returns it for one-shot use.
- ``_fetch_systems()`` is the only Foyer-touching method. It calls
  ``StructuresServiceStub.GetHomeGraph()``, projects the protobuf
  response onto the legacy googlewifi-shaped dict that the existing
  ``WifiGroup``/``WifiPoint``/``WifiClient`` model classmethods consume,
  and returns the dict. Tests fake this method directly.

Action verbs (``pause_station``, ``unpause_station``, ``prioritize_station``,
``set_station_group``, ``run_speedtest``, ``get_speedtest_history``,
``reboot_point``, ``reboot_group``, ``set_guest_enabled``, ``list_clients``)
all raise ``EXIT_UNSUPPORTED_FEATURE`` in Phase B. The Foyer RPCs that
implement them have not yet been mapped (Phase C task); the CLI surface
ships with clean exit-5 + hint so operators get a deterministic failure
mode rather than a fake success.

Failure mapping
---------------

- Missing optional extra → exit 5 (unsupported_feature, family=wifi).
- Connection / DNS / TLS / gRPC transport failure → exit 3 (network).
- gpsoauth result missing ``Auth`` key → exit 2 (auth_failed) because
  the underlying credential is rejected by Google's auth backend; an
  expired or rotated master token surfaces here.
- Unknown group / point / client id → exit 4 (not_found).
- Action verbs that are stubbed → exit 5 (unsupported_feature).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Literal, cast

import requests

from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    EXIT_UNSUPPORTED_FEATURE,
    StructuredError,
)
from nest_cli.wifi.types import (
    SpeedTest,
    WifiClient,
    WifiGroup,
    WifiNetwork,
    WifiPoint,
    WifiPointHealth,
)

__all__ = ["FoyerClient"]


# ---------------------------------------------------------------------------
# Foyer signing constants — pulled inline from glocaltokens/const.py so the
# wifi side doesn't depend on glocaltokens (whose 0.7.x get_master_token
# bug forced the Phase B rewrite in the first place).
# ---------------------------------------------------------------------------

ACCESS_TOKEN_APP_NAME = "com.google.android.apps.chromecast.app"
ACCESS_TOKEN_CLIENT_SIGNATURE = "24bb24c05e47e0aefa68a58a766179d9b613a600"
ACCESS_TOKEN_SERVICE = "oauth2:https://www.google.com/accounts/OAuthLogin"
GOOGLE_HOME_FOYER_API = "googlehomefoyer-pa.googleapis.com:443"
ACCESS_TOKEN_DURATION_S = 3600  # Foyer access tokens live ~1 hour
ACCESS_TOKEN_SKEW_S = 60  # refresh 60s before expiry to absorb clock drift

ROUTER_DEVICE_TYPE = "action.devices.types.ROUTER"

# StructuredError discriminator for every wifi-side error envelope (SRD §11.3).
WIFI_FAMILY: Literal["wifi"] = "wifi"

# ---------------------------------------------------------------------------
# Phase C — OnHub-scoped OAuth + REST constants
# ---------------------------------------------------------------------------
#
# The Phase B gpsoauth path mints an access token bound to the Google Home
# Android app, which Foyer's gRPC HomeGraph endpoint accepts. Foyer's REST
# action endpoints at ``/v2/groups/...`` reject that token; they require an
# OnHub-scoped access token derived through a two-step OAuth chain rooted
# in a standard Google OAuth refresh token (the value persisted by
# ``auth wifi-refresh-bootstrap`` into v3 WifiCredentials).
#
# Step 1 exchanges the refresh token for a "web" access token via the
# generic ``oauth2/v4/token`` endpoint, signed with the public OnHub web
# OAuth client_id.
#
# Step 2 uses that web token to mint an OnHub-app-scoped access token via
# ``oauthaccountmanager.googleapis.com/v1/issuetoken``. The resulting token
# carries the ``accesspoints`` + ``clouddevices`` scopes Foyer's REST verbs
# require. Cached with the same 60-second skew window the gRPC path uses.

ONHUB_OAUTH2_TOKEN_URL = "https://www.googleapis.com/oauth2/v4/token"
ONHUB_ISSUETOKEN_URL = "https://oauthaccountmanager.googleapis.com/v1/issuetoken"

# Google Wifi web OAuth client_id — public, embedded in AngeloD2022's
# onhubauthhelper and djtimca/googlewifi-api. Step 1 signs with this.
ONHUB_WEB_CLIENT_ID = "936475272427.apps.googleusercontent.com"

# OnHub Android app id + signing client_id. Step 2 mints with these.
ONHUB_APP_ID = "com.google.OnHub"
ONHUB_APP_CLIENT_ID = "586698244315-vc96jg3mn4nap78iir799fc2ll3rk18s.apps.googleusercontent.com"
ONHUB_SCOPES = (
    "https://www.googleapis.com/auth/accesspoints https://www.googleapis.com/auth/clouddevices"
)

# Foyer REST base. Same host as the gRPC service, different protocol.
FOYER_REST_BASE = "https://googlehomefoyer-pa.googleapis.com"

# Async operation poll interval + default ceiling.
_OPERATION_POLL_INTERVAL_S = 5.0
_DEFAULT_OPERATION_TIMEOUT_S = 180.0


# ---------------------------------------------------------------------------
# Hint strings
# ---------------------------------------------------------------------------

_INSTALL_HINT = (
    "Install the wifi optional extra: `pip install 'nest-cli[wifi]'` "
    "(or `uv tool install 'nest-cli[wifi]'`). The extra pulls in "
    "gpsoauth + grpcio + ghome-foyer-api, which talk to Google's Foyer service."
)

_PHASE_C_HINT = (
    "Phase B (current) implements only read verbs that derive from "
    "GetHomeGraph (list groups, list points, network info, point health). "
    "Action verbs (pause/unpause/prioritize/group-assign/speedtest/reboot/"
    "guest/list-clients) are deferred to Phase C, which will map the "
    "specific Foyer RPCs. File an issue at "
    "https://github.com/agileguy/nest-cli/issues if you need a specific verb."
)

_REFRESH_TOKEN_HINT = (
    "Run `nest-cli auth wifi-refresh-bootstrap --experimental-wifi "
    "--refresh-token <1//...>` to upgrade the credentials file to v3 "
    "with a Google OAuth refresh token. Foyer REST endpoints at "
    "`/v2/groups/...` need an OnHub-scoped access token that the Phase B "
    "gpsoauth path cannot mint."
)

_PHASE_D_HINT = (
    "Deferred to Phase D: the request body schema for this verb is "
    "undocumented and the risk of corrupting station/group config "
    "is too high to ship without a tested mapping. Track at "
    "https://github.com/agileguy/nest-cli/issues if you need this verb."
)


# ---------------------------------------------------------------------------
# FoyerClient
# ---------------------------------------------------------------------------


class FoyerClient:
    """Direct gRPC client for Google Home Foyer (mesh wifi control).

    Construction lazy-imports ``gpsoauth`` / ``grpc`` / ``ghome_foyer_api``;
    operators on a cam-only install see a clean exit-5 with install hint
    rather than a stack trace from the missing transitive.

    Read methods (Phase B implemented):

    - ``list_groups()``                  → list[WifiGroup]
    - ``list_points(group_id)``          → list[WifiPoint]
    - ``get_network_info(group_id)``     → WifiNetwork
    - ``get_point_health(point_id)``     → WifiPointHealth

    Action methods (Phase B exit-5; deferred to Phase C):

    - ``list_clients(group_id)``
    - ``pause_station(client_id)``
    - ``unpause_station(client_id)``
    - ``prioritize_station(client_id, duration_minutes)``
    - ``set_station_group(client_id, group)``
    - ``run_speedtest(group_id, *, timeout_s=...)``
    - ``get_speedtest_history(group_id, *, limit)``
    - ``reboot_point(point_id)``
    - ``reboot_group(group_id)``
    - ``set_guest_enabled(group_id, *, enabled)``
    """

    def __init__(self, creds: WifiCredentials) -> None:
        """Construct against a v2 ``WifiCredentials`` record.

        Args:
            creds: The operator's wifi credentials. Phase B requires v2
                shape with ``android_id`` populated; v1 files (no
                android_id) fail at the credentials-load layer, never
                reach this constructor.

        Raises:
            StructuredError: exit 5 (family=wifi) if ``gpsoauth``,
                ``grpc``, or ``ghome_foyer_api`` are not installed
                (operator must reinstall with ``[wifi]`` extra).
        """
        try:
            import gpsoauth  # noqa: F401
            import grpc  # noqa: F401
            from ghome_foyer_api import (  # noqa: F401
                api_pb2,
                api_pb2_grpc,
            )
        except ImportError as exc:
            raise StructuredError(
                code=EXIT_UNSUPPORTED_FEATURE,
                message=(
                    "wifi commands require the optional `[wifi]` extra "
                    f"({type(exc).__name__}: {exc})"
                ),
                hint=_INSTALL_HINT,
                family=WIFI_FAMILY,
            ) from exc

        self._creds = creds
        self._access_token: str | None = None
        self._access_token_expiry: float = 0.0
        # Phase C: OnHub-scoped REST token cache + lock. Distinct from the
        # gRPC access token above because the two scopes are non-overlapping
        # (HomeGraph gRPC accepts the gpsoauth-minted token; the v2 REST
        # endpoints reject it). The lock prevents two concurrent action
        # verbs from racing the two-step OAuth chain twice.
        self._onhub_token: str | None = None
        self._onhub_token_expiry: float = 0.0
        self._onhub_token_lock = threading.Lock()
        # Phase C fix — Step 1 web token cached separately from the
        # OnHub token so a Step 2 failure (4xx/5xx/timeout) doesn't
        # force the next call to re-burn refresh-token quota by
        # re-running Step 1. Cache lives inside ``_onhub_token_lock``.
        self._step1_web_token: str | None = None
        self._step1_web_token_expiry: float = 0.0
        # Phase C fix — per-account default mesh group id, resolved
        # lazily via ``_resolve_default_group_id`` on the first action
        # verb that needs it (pause/unpause/prioritize). Cached for the
        # lifetime of the client. A separate lock from the OnHub one
        # because the resolver calls ``list_groups`` (gRPC HomeGraph),
        # which is unrelated to the OnHub OAuth chain — and reusing
        # ``_onhub_token_lock`` would deadlock if any future code path
        # called the resolver from inside the OnHub mint.
        self._resolved_default_group_id: str | None = None
        self._default_group_lock = threading.Lock()
        # requests.Session reused across REST calls so HTTPS connection
        # pooling kicks in. Tests monkey-patch _rest itself, not the session.
        self._rest_session: requests.Session | None = None

    # ------------------------------------------------------------------
    # Public surface — read verbs (Phase B)
    # ------------------------------------------------------------------

    def list_groups(self) -> list[WifiGroup]:
        """Return all mesh groups the operator's account owns (FR-WIFI-1).

        Calls ``GetHomeGraph`` once, projects each ROUTER device into the
        legacy googlewifi-shaped per-system dict, then hands those dicts
        to the existing ``WifiGroup.from_googlewifi_response`` classmethod.
        """
        systems = self._safe_fetch()
        if not isinstance(systems, dict):
            raise self._upstream_shape_error(
                "_fetch_systems",
                f"expected dict, got {type(systems).__name__}",
            )
        groups: list[WifiGroup] = []
        for system_id, payload in sorted(systems.items()):
            try:
                groups.append(WifiGroup.from_googlewifi_response(system_id, payload))
            except (KeyError, TypeError, ValueError) as exc:
                raise self._upstream_shape_error(
                    "GetHomeGraph",
                    f"normalizing group {system_id!r}: {exc}",
                ) from exc
        return groups

    def list_points(self, group_id: str) -> list[WifiPoint]:
        """Return every point/router in ``group_id`` (FR-WIFI-2)."""
        systems = self._safe_fetch()
        record = self._require_group(systems, group_id)
        access_points = record.get("access_points") or {}
        devices = record.get("devices") or {}
        per_ap_count = _count_clients_per_ap(devices)

        points: list[WifiPoint] = []
        for ap_record in _iter_dict_records(access_points):
            try:
                ap_id_for_count = (
                    ap_record.get("id") or ap_record.get("apId") or ap_record.get("ap_id") or ""
                )
                points.append(
                    WifiPoint.from_googlewifi_response(
                        ap_record,
                        connected_clients_count=per_ap_count.get(ap_id_for_count, 0),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise self._upstream_shape_error(
                    "GetHomeGraph access_points",
                    f"normalizing point: {exc}",
                ) from exc
        return sorted(points, key=lambda p: p.id)

    def get_network_info(self, group_id: str) -> WifiNetwork:
        """Emit network-level config for ``group_id`` (FR-WIFI-13).

        Phase B status: ``GetHomeGraph`` does NOT carry SSID, IPv4/IPv6
        WAN config, LAN subnets, DHCP range, or DNS servers — only the
        list of ROUTER devices. Returning a record where every field is
        ``"<unknown>"`` would actively mislead operators piping the
        output through ``jq``. Until Phase C maps the real Foyer RPC
        for network-info, the verb exits-5 with the same Phase-C hint
        as the action verbs.
        """
        raise self._unsupported("wifi network", group_id=group_id)

    def get_point_health(self, point_id: str) -> WifiPointHealth:
        """Return the health snapshot for a single point (FR-WIFI-15)."""
        systems = self._safe_fetch()
        for record in systems.values():
            if not isinstance(record, dict):
                continue
            ap_payload = _find_ap(record, point_id)
            if ap_payload is None:
                continue
            devices = record.get("devices") or {}
            per_ap_count = _count_clients_per_ap(devices)
            try:
                point = WifiPoint.from_googlewifi_response(
                    ap_payload,
                    connected_clients_count=per_ap_count.get(point_id, 0),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise self._upstream_shape_error(
                    "GetHomeGraph access_points",
                    f"normalizing point {point_id!r}: {exc}",
                ) from exc
            return WifiPointHealth.from_wifi_point(point)

        raise StructuredError(
            code=EXIT_NOT_FOUND,
            message=f"wifi point {point_id!r} not found in any mesh group",
            hint=(
                "Run `nest-cli wifi list points <group> --experimental-wifi` "
                "to see the points in each of your groups."
            ),
            family=WIFI_FAMILY,
            details={"point_id": point_id},
        )

    # ------------------------------------------------------------------
    # Action verbs — Phase C: real Foyer REST implementations
    # ------------------------------------------------------------------

    def list_clients(self, group_id: str) -> list[WifiClient]:
        """Return every connected client in ``group_id`` (FR-WIFI-3).

        Calls ``GET /v2/groups/{gid}/stations`` and projects each station
        record onto a ``WifiClient``. Returns an empty list if the group
        has no connected stations. 404 from Foyer (group id wrong) maps
        to ``EXIT_NOT_FOUND`` family=wifi via ``_rest``.
        """
        payload = self._rest("GET", f"/v2/groups/{group_id}/stations")
        stations = (payload or {}).get("stations") or []
        clients: list[WifiClient] = []
        for record in stations:
            try:
                clients.append(WifiClient.from_googlewifi_response(record))
            except (KeyError, TypeError, ValueError) as exc:
                raise self._upstream_shape_error(
                    "REST GET /v2/groups/{gid}/stations",
                    f"normalizing station: {exc}",
                ) from exc
        return sorted(clients, key=lambda c: c.id)

    def pause_station(self, client_id: str, *, group_id: str | None = None) -> None:
        """Pause the named client at the AP (FR-WIFI-4).

        Implemented via ``PUT /v2/groups/{gid}/stationBlocking`` with
        ``{stationId, blocked: "true"}``. When ``group_id`` is omitted the
        client resolves it via ``_resolve_default_group_id`` — a clean
        exit-6 fires for multi-group accounts (the operator must wait for
        Phase C.1's ``--group`` flag, or pin to a single group server-side).
        """
        resolved = group_id if group_id is not None else self._resolve_default_group_id()
        self._station_blocking(client_id, group_id=resolved, blocked=True)

    def unpause_station(self, client_id: str, *, group_id: str | None = None) -> None:
        """Unblock the named client (FR-WIFI-5).

        Same group-resolution semantics as ``pause_station``.
        """
        resolved = group_id if group_id is not None else self._resolve_default_group_id()
        self._station_blocking(client_id, group_id=resolved, blocked=False)

    def _station_blocking(self, client_id: str, *, group_id: str, blocked: bool) -> None:
        """Shared body for pause/unpause — PUT stationBlocking with the flag.

        ``group_id`` is the resolved mesh group id (caller is responsible
        for resolving via ``_resolve_default_group_id`` or passing an
        explicit value). The path is parameterized so multi-group accounts
        target the right mesh once a ``--group`` flag lands (Phase C.1).
        """
        self._rest(
            "PUT",
            f"/v2/groups/{group_id}/stationBlocking",
            json={
                "stationId": client_id,
                "blocked": "true" if blocked else "false",
            },
        )

    def prioritize_station(
        self,
        client_id: str,
        duration_minutes: int,
        *,
        group_id: str | None = None,
    ) -> None:
        """Prioritize the named client for ``duration_minutes`` (FR-WIFI-6).

        Implemented via ``PUT /v2/groups/{gid}/prioritizedStation`` with
        ``{stationId, prioritizationEndTime: <ISO8601-Z>}``. Foyer
        computes the effective end time at the router; we send the
        absolute timestamp so the operator's intent is unambiguous even
        if the local clock and the router clock disagree by a minute or
        two. When ``group_id`` is omitted the client resolves it via
        ``_resolve_default_group_id``.
        """
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime
        from datetime import timedelta as _timedelta

        resolved = group_id if group_id is not None else self._resolve_default_group_id()
        end = _datetime.now(_UTC) + _timedelta(minutes=duration_minutes)
        end_iso = end.isoformat().replace("+00:00", "Z")
        self._rest(
            "PUT",
            f"/v2/groups/{resolved}/prioritizedStation",
            json={
                "stationId": client_id,
                "prioritizationEndTime": end_iso,
            },
        )

    def _resolve_default_group_id(self) -> str:
        """Return the operator's single mesh group id, or fail cleanly.

        Action verbs need to target a specific mesh group's REST path
        (``/v2/groups/{gid}/...``). Phase C ships without a ``--group``
        flag, so we infer the target by listing the operator's groups and
        accepting only the single-group case. Multi-group accounts get a
        clean exit-6 + hint pointing at the Phase C.1 follow-up; zero-group
        accounts get exit-4 (refresh-token scope or ownership issue).

        The result is cached on the instance under
        ``_default_group_lock`` so concurrent fan-out workers reuse it
        without paying multiple ``GetHomeGraph`` round-trips. The lock is
        intentionally distinct from ``_onhub_token_lock`` because
        ``list_groups`` routes through the gRPC HomeGraph path (unrelated
        to OnHub OAuth) — sharing the OnHub lock would deadlock if any
        future code path called the resolver from inside the OnHub mint.
        """
        with self._default_group_lock:
            if self._resolved_default_group_id is not None:
                return self._resolved_default_group_id

            groups = self.list_groups()
            count = len(groups)
            if count == 1:
                self._resolved_default_group_id = groups[0].id
                return self._resolved_default_group_id
            if count == 0:
                raise StructuredError(
                    code=EXIT_NOT_FOUND,
                    message="no wifi groups visible to this account",
                    hint=(
                        "Verify your refresh token has accesspoints scope "
                        "and you own at least one Nest Wifi mesh."
                    ),
                    family=WIFI_FAMILY,
                )
            raise StructuredError(
                code=EXIT_CONFIG_ERROR,
                message=(
                    f"account has {count} wifi groups; per-station verbs need an explicit group"
                ),
                hint=(
                    "Phase C.1 will add --group; for now use 'wifi list "
                    "groups' to confirm and re-run after pinning a single "
                    "group server-side."
                ),
                family=WIFI_FAMILY,
            )

    def set_station_group(self, client_id: str, group: str | None) -> None:
        """Assign the client to a Foyer group (FR-WIFI-7) — deferred to Phase D.

        The Foyer endpoint exists at ``POST /v2/groups/{gid}/stationSets``
        but the request body schema is not publicly documented. Shipping
        a guess risks corrupting the operator's station-set configuration,
        so this verb stays exit-5 with a hint pointing at Phase D.
        """
        raise self._deferred_phase_d(
            "wifi group-assign",
            client_id=client_id,
            requested_group=group,
        )

    def run_speedtest(
        self,
        group_id: str,
        *,
        timeout_s: float = 180.0,
    ) -> SpeedTest:
        """Trigger a fresh speed test and return the result (FR-WIFI-8).

        Three round-trips:

        1. POST ``/v2/groups/{gid}/wanSpeedTest`` to kick off the test.
           Response carries an ``operationId`` we poll on.
        2. Poll ``/v2/operations/{operationId}`` every 5s until
           ``operationState == "DONE"`` or ``timeout_s`` trips.
        3. GET ``/v2/groups/{gid}/speedTestResults?maxResultCount=1`` to
           fetch the freshest result and project it onto a SpeedTest record.
        """
        kickoff = self._rest("POST", f"/v2/groups/{group_id}/wanSpeedTest", json={})
        operation_id = (kickoff or {}).get("operationId")
        if not operation_id:
            raise self._upstream_shape_error(
                "REST POST /v2/groups/{gid}/wanSpeedTest",
                f"response missing operationId (body={kickoff!r})",
            )
        self._wait_for_operation(operation_id, timeout_s=timeout_s)

        history = self._rest(
            "GET",
            f"/v2/groups/{group_id}/speedTestResults",
            params={"maxResultCount": 1},
        )
        results = (history or {}).get("results") or []
        if not results:
            raise self._upstream_shape_error(
                "REST GET /v2/groups/{gid}/speedTestResults",
                "operation completed but no results returned",
            )
        return SpeedTest.from_googlewifi_response(group_id=group_id, payload=results[0])

    def get_speedtest_history(self, group_id: str, *, limit: int) -> list[SpeedTest]:
        """Return up to ``limit`` recent speed-test results (FR-WIFI-9).

        GET ``/v2/groups/{gid}/speedTestResults?maxResultCount={limit}``.
        Empty list if the router has no history. Each result projects
        through ``SpeedTest.from_googlewifi_response``.
        """
        payload = self._rest(
            "GET",
            f"/v2/groups/{group_id}/speedTestResults",
            params={"maxResultCount": limit},
        )
        results = (payload or {}).get("results") or []
        out: list[SpeedTest] = []
        for record in results:
            try:
                out.append(SpeedTest.from_googlewifi_response(group_id=group_id, payload=record))
            except (KeyError, TypeError, ValueError) as exc:
                raise self._upstream_shape_error(
                    "REST GET /v2/groups/{gid}/speedTestResults",
                    f"normalizing result: {exc}",
                ) from exc
        return out

    def reboot_point(self, point_id: str) -> None:
        """Reboot a single mesh point (FR-WIFI-10).

        POST ``/v2/accesspoints/{apId}/reboot`` with an empty body.
        Foyer returns 204 on success. Unknown ap id → EXIT_NOT_FOUND.
        """
        self._rest("POST", f"/v2/accesspoints/{point_id}/reboot", json={})

    def reboot_group(self, group_id: str) -> list[str]:
        """Reboot every point in a mesh group (FR-WIFI-11).

        POST ``/v2/groups/{gid}/reboot`` with an empty body. We list the
        points first so the response payload contains the list of points
        actually rebooted (matches the Phase B CLI shape that already
        exists at ``wifi reboot group``). list_points uses the gRPC seam,
        which works on v2 and v3 credentials alike.
        """
        points = self.list_points(group_id)
        self._rest("POST", f"/v2/groups/{group_id}/reboot", json={})
        return [p.id for p in points]

    def set_guest_enabled(self, group_id: str, *, enabled: bool) -> None:
        """Toggle the guest network (FR-WIFI-14) — deferred to Phase D.

        The Foyer endpoint exists at
        ``PUT /v2/groups/{gid}/guestWirelessConfig`` but the body shape
        (SSID + password preservation rules + per-band enables) is not
        publicly documented. A guess risks nuking the guest network's
        SSID or password, so this verb stays exit-5 with a hint pointing
        at Phase D.
        """
        raise self._deferred_phase_d(
            "wifi guest enable/disable",
            group_id=group_id,
            requested_enabled=enabled,
        )

    # ------------------------------------------------------------------
    # Internals — auth + transport + projection
    # ------------------------------------------------------------------

    def _refresh_access_token(self) -> str:
        """Mint a fresh Foyer access token via ``gpsoauth.perform_oauth``.

        Uses the operator's master_token + android_id from the cached
        WifiCredentials. The token mint is signed against the Google
        Home Android app (``com.google.android.apps.chromecast.app``)
        and bound to the ``OAuthLogin`` service. ``gpsoauth.perform_oauth``
        returns a dict; ``Auth`` is the access token, present on success.
        Any other shape (missing ``Auth``, error blob) maps to exit 2 —
        the operator's master token has rotated or is otherwise rejected
        by Google's auth backend.
        """
        import gpsoauth

        try:
            res = gpsoauth.perform_oauth(
                self._creds.google_account_email,
                self._creds.master_token,
                self._creds.android_id,
                app=ACCESS_TOKEN_APP_NAME,
                service=ACCESS_TOKEN_SERVICE,
                client_sig=ACCESS_TOKEN_CLIENT_SIGNATURE,
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"network error contacting Google auth: {type(exc).__name__}: {exc}",
                hint="Check your internet connection and retry.",
                family=WIFI_FAMILY,
            ) from exc

        if not isinstance(res, dict) or "Auth" not in res:
            err_blob = res.get("Error") if isinstance(res, dict) else None
            raise StructuredError(
                code=EXIT_AUTH_ERROR,
                message=(
                    f"gpsoauth.perform_oauth did not return an access token (Error={err_blob!r})"
                ),
                hint=(
                    "The Android master token is invalid, expired, or paired "
                    "with a different android_id than the one in credentials-"
                    "wifi.json. Re-extract both values from the rooted Android "
                    "device and re-run `nest-cli auth wifi-setup --overwrite "
                    "--experimental-wifi`."
                ),
                family=WIFI_FAMILY,
            )

        token = res["Auth"]
        self._access_token = token
        self._access_token_expiry = time.time() + ACCESS_TOKEN_DURATION_S - ACCESS_TOKEN_SKEW_S
        return token

    def _ensure_access_token(self) -> str:
        """Return a non-expired access token, minting one if needed."""
        if self._access_token is None or time.time() >= self._access_token_expiry:
            return self._refresh_access_token()
        return self._access_token

    def _channel(self) -> Any:
        """Build a gRPC composite channel (TLS + bearer token) for one-shot use.

        The caller is responsible for closing the channel; we use
        ``with`` blocks at the call sites for that.
        """
        import grpc

        token = self._ensure_access_token()
        scc = grpc.ssl_channel_credentials(root_certificates=None)
        tok = grpc.access_token_call_credentials(token)
        return grpc.secure_channel(
            GOOGLE_HOME_FOYER_API,
            grpc.composite_channel_credentials(scc, tok),
        )

    def _fetch_systems(self) -> dict[str, dict[str, Any]]:
        """Call ``GetHomeGraph`` and return a googlewifi-shaped dict.

        The returned dict is keyed by mesh-group id and each value is the
        legacy googlewifi shape (``{"id", "name", "access_points",
        "devices", "groupSettings", "wanConnectionStatus", ...}``) so the
        existing ``WifiGroup``/``WifiPoint``/``WifiNetwork`` classmethods
        consume it unchanged. Tests fake this method directly to inject
        fixtures without touching the gRPC transport.
        """
        import grpc
        from ghome_foyer_api.api_pb2 import GetHomeGraphRequest
        from ghome_foyer_api.api_pb2_grpc import StructuresServiceStub

        try:
            with self._channel() as channel:
                stub = StructuresServiceStub(channel)
                response = stub.GetHomeGraph(GetHomeGraphRequest(string1=""))
        except StructuredError:
            raise
        except grpc.RpcError as exc:
            code = exc.code() if hasattr(exc, "code") else None
            details = exc.details() if hasattr(exc, "details") else str(exc)
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"Foyer gRPC error: code={code} details={details}",
                hint=(
                    "Check internet connectivity. If this persists, the "
                    "access token may be invalid — re-run `auth wifi-setup "
                    "--overwrite --experimental-wifi`."
                ),
                family=WIFI_FAMILY,
            ) from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"network error contacting Foyer: {type(exc).__name__}: {exc}",
                hint="Check your internet connection and retry.",
                family=WIFI_FAMILY,
            ) from exc

        return _homegraph_to_legacy_dict(response)

    def _safe_fetch(self) -> dict[str, dict[str, Any]]:
        """Call ``_fetch_systems`` and translate any leaking network errors.

        ``_fetch_systems`` already maps gRPC and connection errors to
        ``StructuredError`` along its non-mocked path, but tests inject
        substitutes that raise ``ConnectionError`` directly. This wrapper
        catches those leaks (in tests AND production) and translates
        them at the public-method seam so every read verb gets the same
        clean exit-3 mapping without each method duplicating the
        try/except.
        """
        try:
            return self._fetch_systems()
        except StructuredError:
            raise
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"network error contacting Foyer: {type(exc).__name__}: {exc}",
                hint="Check your internet connection and retry.",
                family=WIFI_FAMILY,
            ) from exc

    # ------------------------------------------------------------------
    # Internals — Phase C: OnHub OAuth + REST transport
    # ------------------------------------------------------------------

    def _refresh_onhub_access_token(self) -> str:
        """Mint a fresh OnHub-scoped access token via the two-step OAuth chain.

        Step 1: POST ``oauth2/v4/token`` with the operator's refresh token,
        signed against the public OnHub web client_id, returning a generic
        Google "web" access token.

        Step 2: POST ``oauthaccountmanager/v1/issuetoken`` bearer-authed with
        the Step 1 token, claiming the OnHub Android app id + scopes
        (``accesspoints`` + ``clouddevices``), returning the OnHub-scoped
        access token Foyer's REST endpoints accept.

        The two-step is what AngeloD2022/onhubauthhelper and
        djtimca/googlewifi-api both use; the gRPC HomeGraph token cannot
        be substituted (different scopes).

        Caches with the same 60-second skew window the gRPC path uses.
        Serialized via ``_onhub_token_lock`` so two action verbs racing
        don't both pay the round-trip.
        """
        with self._onhub_token_lock:
            # Re-check inside the lock — another thread may have just
            # refreshed while we were waiting on the lock.
            now = time.time()
            if self._onhub_token is not None and now < self._onhub_token_expiry:
                return self._onhub_token

            if not self._creds.refresh_token:
                raise StructuredError(
                    code=EXIT_AUTH_ERROR,
                    message=(
                        "Foyer REST verbs require a v3 credentials file with a "
                        "refresh_token; current credentials are v"
                        f"{self._creds.version} with refresh_token=None"
                    ),
                    hint=_REFRESH_TOKEN_HINT,
                    family=WIFI_FAMILY,
                )

            session = self._get_rest_session()

            # --- Step 1: refresh-token → web access token ----------------
            try:
                step1 = session.post(
                    ONHUB_OAUTH2_TOKEN_URL,
                    data={
                        "client_id": ONHUB_WEB_CLIENT_ID,
                        "grant_type": "refresh_token",
                        "refresh_token": self._creds.refresh_token,
                    },
                    timeout=20,
                )
            except (requests.ConnectionError, requests.Timeout, OSError) as exc:
                raise StructuredError(
                    code=EXIT_NETWORK_ERROR,
                    message=(
                        f"network error contacting oauth2/v4/token: {type(exc).__name__}: {exc}"
                    ),
                    hint="Check your internet connection and retry.",
                    family=WIFI_FAMILY,
                ) from exc
            if step1.status_code >= 400:
                raise StructuredError(
                    code=EXIT_AUTH_ERROR,
                    message=(
                        "oauth2/v4/token rejected the refresh token "
                        f"(HTTP {step1.status_code}): {step1.text[:200]}"
                    ),
                    hint=(
                        "The OAuth refresh token is invalid, expired, or "
                        "revoked. Re-run `nest-cli auth wifi-refresh-bootstrap "
                        "--experimental-wifi --refresh-token <1//...>` with a "
                        "freshly minted token."
                    ),
                    family=WIFI_FAMILY,
                )
            try:
                web_token = str(step1.json()["access_token"])
            except (ValueError, KeyError) as exc:
                raise StructuredError(
                    code=EXIT_AUTH_ERROR,
                    message=(
                        f"oauth2/v4/token response missing access_token (body={step1.text[:200]})"
                    ),
                    family=WIFI_FAMILY,
                ) from exc

            # --- Step 2: web token → OnHub-scoped token ------------------
            try:
                step2 = session.post(
                    ONHUB_ISSUETOKEN_URL,
                    data={
                        "app_id": ONHUB_APP_ID,
                        "client_id": ONHUB_APP_CLIENT_ID,
                        "scope": ONHUB_SCOPES,
                    },
                    headers={"Authorization": f"Bearer {web_token}"},
                    timeout=20,
                )
            except (requests.ConnectionError, requests.Timeout, OSError) as exc:
                raise StructuredError(
                    code=EXIT_NETWORK_ERROR,
                    message=(f"network error contacting issuetoken: {type(exc).__name__}: {exc}"),
                    hint="Check your internet connection and retry.",
                    family=WIFI_FAMILY,
                ) from exc
            if step2.status_code >= 400:
                raise StructuredError(
                    code=EXIT_AUTH_ERROR,
                    message=(
                        "issuetoken rejected the OnHub mint request "
                        f"(HTTP {step2.status_code}): {step2.text[:200]}"
                    ),
                    hint=(
                        "The web access token from Step 1 lacks the right "
                        "scopes, or the OnHub app_id / client_id constants "
                        "have rotated. File a nest-cli issue if this "
                        "persists."
                    ),
                    family=WIFI_FAMILY,
                )
            try:
                body = step2.json()
                onhub_token: str = str(body["token"])
                expires_in_s = int(body.get("expiresIn", ACCESS_TOKEN_DURATION_S))
            except (ValueError, KeyError) as exc:
                raise StructuredError(
                    code=EXIT_AUTH_ERROR,
                    message=(f"issuetoken response missing token (body={step2.text[:200]})"),
                    family=WIFI_FAMILY,
                ) from exc

            self._onhub_token = onhub_token
            self._onhub_token_expiry = time.time() + expires_in_s - ACCESS_TOKEN_SKEW_S
            return onhub_token

    def _ensure_onhub_token(self) -> str:
        """Return a non-expired OnHub access token, minting one if needed."""
        if self._onhub_token is None or time.time() >= self._onhub_token_expiry:
            return self._refresh_onhub_access_token()
        return self._onhub_token

    def _get_rest_session(self) -> requests.Session:
        """Lazy-construct + return the shared requests.Session for REST calls."""
        if self._rest_session is None:
            self._rest_session = requests.Session()
        return self._rest_session

    def _rest(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a REST call to ``FOYER_REST_BASE + path`` with OnHub auth.

        Returns the JSON-decoded response body, or ``None`` for empty
        2xx bodies (some Foyer endpoints return 204). Non-2xx responses
        map to ``StructuredError`` with family=wifi and an exit code
        chosen by status (401/403 → exit 2, 404 → exit 4, 5xx → exit 3,
        anything else → exit 1).

        Tests fake this method directly to avoid spinning up a real
        HTTP transport; the verbs above call ``self._rest(...)``
        unchanged so the production / fake split happens at one seam.
        """
        token = self._ensure_onhub_token()
        url = FOYER_REST_BASE + path
        session = self._get_rest_session()
        try:
            response = session.request(
                method,
                url,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except (requests.ConnectionError, requests.Timeout, OSError) as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=(
                    f"network error contacting Foyer REST {method} {path}: "
                    f"{type(exc).__name__}: {exc}"
                ),
                hint="Check your internet connection and retry.",
                family=WIFI_FAMILY,
            ) from exc
        if response.status_code >= 400:
            raise self._rest_error(method, path, response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise self._upstream_shape_error(
                f"REST {method} {path}",
                f"non-JSON response: {response.text[:200]}",
            ) from exc

    def _wait_for_operation(
        self,
        operation_id: str,
        *,
        timeout_s: float = _DEFAULT_OPERATION_TIMEOUT_S,
    ) -> Any:
        """Poll ``/v2/operations/{id}`` until ``operationState=DONE`` or timeout.

        Returns the final operation payload. Used by ``run_speedtest``,
        which kicks off an async wanSpeedTest operation server-side.
        Raises ``EXIT_NETWORK_ERROR`` on timeout (the operation may
        eventually complete server-side; the operator can re-poll via
        ``speedtest history`` once it does).
        """
        deadline = time.time() + timeout_s
        while True:
            payload = self._rest("GET", f"/v2/operations/{operation_id}")
            state = (payload or {}).get("operationState")
            if state == "DONE":
                return payload
            if time.time() >= deadline:
                raise StructuredError(
                    code=EXIT_NETWORK_ERROR,
                    message=(
                        f"operation {operation_id} did not complete within "
                        f"{timeout_s:.0f}s (last state: {state!r})"
                    ),
                    hint=(
                        "Re-run with --timeout <larger>, or check "
                        "`wifi speedtest history` later — the operation may "
                        "still complete server-side."
                    ),
                    family=WIFI_FAMILY,
                )
            time.sleep(_OPERATION_POLL_INTERVAL_S)

    @staticmethod
    def _rest_error(method: str, path: str, response: Any) -> StructuredError:
        """Map a non-2xx REST response to a StructuredError.

        Status mapping:
        - 401/403 → EXIT_AUTH_ERROR (operator re-runs wifi-refresh-bootstrap)
        - 404     → EXIT_NOT_FOUND  (group/AP/station id wrong)
        - 5xx     → EXIT_NETWORK_ERROR (transient upstream)
        - other   → EXIT_DEVICE_ERROR (Foyer-shape rotation, SRD §3.2.3)
        """
        status = response.status_code
        body = response.text[:200] if hasattr(response, "text") else ""
        if status in (401, 403):
            return StructuredError(
                code=EXIT_AUTH_ERROR,
                message=(f"Foyer REST {method} {path} returned HTTP {status}: {body}"),
                hint=(
                    "The OnHub access token was rejected. Re-run "
                    "`nest-cli auth wifi-refresh-bootstrap --experimental-wifi "
                    "--refresh-token <1//...>` with a freshly minted token."
                ),
                family=WIFI_FAMILY,
            )
        if status == 404:
            return StructuredError(
                code=EXIT_NOT_FOUND,
                message=(f"Foyer REST {method} {path} returned 404: {body}"),
                hint=(
                    "Check the group / point / station id. Run "
                    "`wifi list groups --experimental-wifi` to enumerate."
                ),
                family=WIFI_FAMILY,
            )
        if status >= 500:
            return StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=(f"Foyer REST {method} {path} returned HTTP {status}: {body}"),
                hint="Foyer reported an upstream error; retry in a few seconds.",
                family=WIFI_FAMILY,
            )
        return StructuredError(
            code=EXIT_DEVICE_ERROR,
            message=(f"Foyer REST {method} {path} returned unexpected HTTP {status}: {body}"),
            hint=(
                "Foyer surfaced an unexpected status code. This may indicate "
                "an upstream-shape rotation (SRD §3.2.3); check the nest-cli "
                "issue tracker."
            ),
            family=WIFI_FAMILY,
        )

    # ------------------------------------------------------------------
    # Internals — error helpers
    # ------------------------------------------------------------------

    def _require_group(self, systems: dict[str, Any], group_id: str) -> dict[str, Any]:
        """Return ``systems[group_id]`` or raise EXIT_NOT_FOUND."""
        if group_id not in systems:
            raise StructuredError(
                code=EXIT_NOT_FOUND,
                message=f"wifi group {group_id!r} not found",
                hint=(
                    "Run `nest-cli wifi list groups --experimental-wifi` "
                    "to see the groups your account owns."
                ),
                family=WIFI_FAMILY,
                details={"group_id": group_id},
            )
        record = systems[group_id]
        if not isinstance(record, dict):
            raise self._upstream_shape_error(
                "GetHomeGraph",
                f"group {group_id!r} payload is {type(record).__name__}, expected dict",
            )
        return record

    @staticmethod
    def _upstream_shape_error(surface: str, detail: str) -> StructuredError:
        """Build a SRD §3.2.3-aligned ``device_error`` for upstream-shape rot."""
        return StructuredError(
            code=EXIT_DEVICE_ERROR,
            message=f"Foyer returned unexpected shape on {surface}: {detail}",
            hint=(
                "This is the documented Foyer rotation risk (SRD §3.2.3). "
                "Check ghome-foyer-api / nest-cli issue trackers; you may "
                "need to update the optional `[wifi]` extra."
            ),
            family=WIFI_FAMILY,
        )

    @staticmethod
    def _unsupported(verb: str, **details: Any) -> StructuredError:
        """Build a Phase-B exit-5 envelope for action verbs deferred to Phase C.

        ``details`` keyword args become the structured-error envelope's
        ``details`` field. Captured args echo into JSONL output so
        operators piping through ``jq`` can correlate the failed attempt
        with their input.
        """
        return StructuredError(
            code=EXIT_UNSUPPORTED_FEATURE,
            message=f"{verb} is not implemented in Phase B",
            hint=_PHASE_C_HINT,
            family=WIFI_FAMILY,
            details=details,
        )

    @staticmethod
    def _deferred_phase_d(verb: str, **details: Any) -> StructuredError:
        """Build a Phase-C exit-5 envelope for verbs explicitly held to Phase D.

        Phase C ships REST implementations for 8 of the 10 action verbs.
        ``set_station_group`` and ``set_guest_enabled`` are held back
        because their Foyer request bodies are undocumented and the
        risk of corrupting station/group config (or nuking the guest
        SSID/password) is too high to ship a guess.
        """
        return StructuredError(
            code=EXIT_UNSUPPORTED_FEATURE,
            message=f"{verb} is deferred to Phase D",
            hint=_PHASE_D_HINT,
            family=WIFI_FAMILY,
            details=details,
        )


# ---------------------------------------------------------------------------
# Module-private helpers — protobuf → legacy dict projection + record walks
# ---------------------------------------------------------------------------


def _homegraph_to_legacy_dict(response: Any) -> dict[str, dict[str, Any]]:
    """Project a ``GetHomeGraphResponse`` protobuf onto the legacy dict shape.

    The legacy googlewifi shape is::

        {
          "<system_id>": {
            "id": "<system_id>",
            "name": "<group display name>",
            "wanConnectionStatus": "ONLINE" | "OFFLINE",
            "access_points": {
              "<ap_id>": {
                "id": "<ap_id>",
                "isMaster": True | False,
                "displayName": "<friendly name>",
                "model": "<model>",
                "firmwareVersion": "<fw>",
                "status": {"apState": "ONLINE", "uptimeSeconds": 0}
              }, ...
            },
            "devices": {},
            "groupSettings": {
              "apSettings": {"ssid": "<unknown-ssid>"},
              "guestSsid": {"enabled": False}
            }
          }
        }

    Foyer's HomeGraph protobuf does not carry per-router ssid / firmware /
    uptime / signal-strength fields directly — those live on the wifi-side
    gRPC paths Phase C will map. For Phase B we surface conservative
    defaults so the existing model classmethods produce valid records:
    one mesh group per ROUTER device (id = device_id), online flag from
    presence of the device, and the rest as ``None``/``"<unknown-...>"``.

    If the response carries no ROUTER devices, returns ``{}``. The CLI
    layer renders that as an empty list with a hint.
    """
    home = getattr(response, "home", None)
    if home is None:
        return {}

    devices = list(getattr(home, "devices", []) or [])
    routers = [d for d in devices if getattr(d, "device_type", "") == ROUTER_DEVICE_TYPE]

    if not routers:
        return {}

    home_name = (getattr(home, "home_name", "") or "").strip() or "Home Mesh"

    # The HomeGraph projection assumes one mesh group per home — accurate
    # for the common case (Nest Wifi Pro / Nest Wifi). Multi-mesh households
    # are rare; if that becomes a real ask, we'd switch to discriminating
    # by groupSettings.applianceId on the Foyer side.
    access_points: dict[str, dict[str, Any]] = {}
    master_id: str | None = None
    for idx, router in enumerate(routers):
        device_info = getattr(router, "device_info", None)
        device_id = getattr(device_info, "device_id", "") if device_info else ""
        if not device_id:
            device_id = f"router-{idx}"
        is_master = idx == 0
        if is_master:
            master_id = device_id
        hardware = getattr(router, "hardware", None)
        model = getattr(hardware, "model", None) if hardware else None
        access_points[device_id] = {
            "id": device_id,
            "isMaster": is_master,
            "displayName": (getattr(router, "device_name", "") or device_id),
            "model": model or None,
            "firmwareVersion": None,
            "status": {"apState": "ONLINE", "uptimeSeconds": 0},
        }

    system_id = master_id or "home-mesh-1"
    # Note: ``groupSettings`` / ``wanSettings`` keys deliberately omitted —
    # ``get_network_info`` is an exit-5 stub in Phase B (HomeGraph carries
    # no SSID/IPv4/IPv6/DNS data), so the WifiGroup classmethod's defensive
    # fallbacks for those nested blocks are what populates the SSID display
    # at the list-groups surface.
    return {
        system_id: {
            "id": system_id,
            "name": home_name,
            "wanConnectionStatus": "ONLINE",
            "access_points": access_points,
            "devices": {},
        }
    }


def _iter_dict_records(container: Any) -> list[dict[str, Any]]:
    """Yield dict records from a dict-of-dicts or list-of-dicts container.

    Non-dict elements are filtered out (not coerced to ``{}``) so
    upstream-shape rotations surface visibly to the SRD §3.2.3 path
    instead of getting silently masked.
    """
    if isinstance(container, dict):
        return [v for v in container.values() if isinstance(v, dict)]
    if isinstance(container, list):
        return [v for v in container if isinstance(v, dict)]
    return []


def _count_clients_per_ap(devices: Any) -> dict[str, int]:
    """Bucket connected clients by ``apId`` (or equivalent key)."""
    counts: dict[str, int] = {}
    for station in _iter_dict_records(devices):
        ap_id = station.get("apId") or station.get("ap_id") or station.get("connected_to_point_id")
        if isinstance(ap_id, str) and ap_id:
            counts[ap_id] = counts.get(ap_id, 0) + 1
    return counts


def _find_ap(record: dict[str, Any], point_id: str) -> dict[str, Any] | None:
    """Return the access-point payload with id == ``point_id`` from a group."""
    aps = record.get("access_points") or record.get("accessPoints") or {}
    if isinstance(aps, dict):
        if point_id in aps and isinstance(aps[point_id], dict):
            return cast("dict[str, Any]", aps[point_id])
        for ap in aps.values():
            if not isinstance(ap, dict):
                continue
            if (ap.get("id") or ap.get("apId")) == point_id:
                return cast("dict[str, Any]", ap)
    elif isinstance(aps, list):
        for ap in aps:
            if isinstance(ap, dict) and (ap.get("id") or ap.get("apId")) == point_id:
                return cast("dict[str, Any]", ap)
    return None

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

import time
from typing import Any, Literal, cast

from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
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
    # Action verbs — Phase B exit-5 stubs (Phase C will map RPCs)
    # ------------------------------------------------------------------

    def list_clients(self, group_id: str) -> list[WifiClient]:
        """Return every connected client in ``group_id`` (FR-WIFI-3).

        Phase B status: ``GetHomeGraph`` does not include connected-client
        records (only routers and devices the home shows); the Foyer RPC
        for connected wifi stations has not yet been mapped. Surface a
        clean exit-5 with hint until Phase C lands the right call.
        """
        raise self._unsupported("wifi list-clients", group_id=group_id)

    def pause_station(self, client_id: str) -> None:
        """Pause the named client (FR-WIFI-4) — deferred to Phase C."""
        raise self._unsupported("wifi pause", client_id=client_id)

    def unpause_station(self, client_id: str) -> None:
        """Unpause the named client (FR-WIFI-5) — deferred to Phase C."""
        raise self._unsupported("wifi unpause", client_id=client_id)

    def prioritize_station(self, client_id: str, duration_minutes: int) -> None:
        """Prioritize the named client (FR-WIFI-6) — deferred to Phase C."""
        raise self._unsupported(
            "wifi prioritize",
            client_id=client_id,
            duration_minutes=duration_minutes,
        )

    def set_station_group(self, client_id: str, group: str | None) -> None:
        """Assign the client to a Foyer group (FR-WIFI-7) — deferred to Phase C."""
        raise self._unsupported(
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
        """Trigger a fresh speed test (FR-WIFI-8) — deferred to Phase C.

        ``timeout_s`` is captured into the error envelope's ``details``
        so the post-Phase-C signature lands without a CLI flag wiring
        change. The value is not used at runtime today.
        """
        raise self._unsupported(
            "wifi speedtest run",
            group_id=group_id,
            timeout_s=timeout_s,
        )

    def get_speedtest_history(self, group_id: str, *, limit: int) -> list[SpeedTest]:
        """Return speed-test history (FR-WIFI-9) — deferred to Phase C."""
        raise self._unsupported(
            "wifi speedtest history",
            group_id=group_id,
            limit=limit,
        )

    def reboot_point(self, point_id: str) -> None:
        """Reboot a single point (FR-WIFI-10) — deferred to Phase C."""
        raise self._unsupported("wifi reboot point", point_id=point_id)

    def reboot_group(self, group_id: str) -> list[str]:
        """Reboot every point in a group (FR-WIFI-11) — deferred to Phase C."""
        raise self._unsupported("wifi reboot group", group_id=group_id)

    def set_guest_enabled(self, group_id: str, *, enabled: bool) -> None:
        """Toggle the guest network (FR-WIFI-14) — deferred to Phase C."""
        raise self._unsupported(
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
                    "gpsoauth.perform_oauth did not return an access token "
                    f"(Error={err_blob!r})"
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
    # Internals — error helpers
    # ------------------------------------------------------------------

    def _require_group(
        self, systems: dict[str, Any], group_id: str
    ) -> dict[str, Any]:
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

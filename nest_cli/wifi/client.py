"""Thin sync wrapper around ``googlewifi.GoogleWifi`` (Foyer mesh control).

``googlewifi.GoogleWifi`` is async-only and depends on ``aiohttp``. The
nest-cli verbs are sync (Click commands return after one operation), so
this module exposes a sync facade that drives the async upstream via
``asyncio.run`` per call.

Lazy imports
------------

The ``googlewifi`` and ``glocaltokens`` packages are optional install
extras (SRD §13.2 / Decision 5 — ``pip install 'nest-cli[wifi]'``). We
do NOT import them at module level. ``FoyerClient.__init__`` performs
the lazy import and surfaces a missing-extras failure as a structured
error (exit 5, family="wifi") with a hint pointing the operator at the
correct install command. Operators on a cam-only install never trigger
the import.

Failure mapping (SRD §11.1):

- Missing optional extra → exit 5 (unsupported_feature, family=wifi).
- Connection / DNS / TLS failure → exit 3 (network, family=wifi).
- googlewifi raises GoogleWifiException for upstream-shape rotations
  (SRD §3.2.3) → exit 1 (device_error, family=wifi).
- Unknown group id passed to list_points / list_clients → exit 4
  (not_found, family=wifi).
- Unknown client id passed to a per-client action → exit 4 with
  family="wifi" and a hint pointing at ``wifi list clients``.

Phase 3B (FR-WIFI-4..7) — per-client actions
--------------------------------------------

The action methods (pause / unpause / prioritize / group-assign) take
ONLY a ``client_id`` because the operator-facing CLI verbs take only
``<client-id>`` (SRD §5.4.3). Foyer's underlying endpoints, however,
all require both ``system_id`` (group_id) AND the station id. The
``_resolve_group_for_client`` private helper does the lookup by
walking ``get_systems()`` once per action call and locating the group
whose ``devices`` dict contains the target client.

Upstream googlewifi version notes
---------------------------------

- ``pause_device(system_id, device_id, pause_state: bool)``
    Pause and unpause are the same endpoint with a boolean flag.
    FR-WIFI-4 / FR-WIFI-5 idempotence is asserted by SRD: Foyer
    accepts pause-of-already-paused without error.
- ``prioritize_device(system_id, device_id, duration_hours: int)``
    HOURS, not minutes. The library clamps 1 ≤ hours ≤ 6. SRD's
    ``--duration`` is in MINUTES (1..240); we ceil-convert at the
    facade boundary (45min → 1h, 91min → 2h, 240min → 4h).
- group-assign — NO upstream method exists. The Foyer-side endpoint
    that the Google Home app uses for "set this device's group
    assignment" is not currently wrapped by ``googlewifi``. Until
    upstream lands it, ``set_station_group`` raises EXIT_UNSUPPORTED_FEATURE
    so the CLI surface (FR-WIFI-7) ships, but the operator gets a
    clean exit-5 with a hint pointing at the upstream gap.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nest_cli.errors import (
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    EXIT_UNSUPPORTED_FEATURE,
    StructuredError,
)
from nest_cli.wifi.types import WifiClient, WifiGroup, WifiPoint

__all__ = ["FoyerClient"]


# ---------------------------------------------------------------------------
# Hint strings
# ---------------------------------------------------------------------------

_INSTALL_HINT = (
    "Install the wifi optional extra: `pip install 'nest-cli[wifi]'` "
    "(or `uv tool install 'nest-cli[wifi]'`). The extra pulls in "
    "googlewifi + glocaltokens, which talk to Google's Foyer service."
)


# ---------------------------------------------------------------------------
# FoyerClient
# ---------------------------------------------------------------------------


class FoyerClient:
    """Sync wrapper around the async ``googlewifi.GoogleWifi`` API.

    Construction lazy-imports ``googlewifi``; operators on a cam-only
    install see a clean exit-5 with install hint rather than a stack
    trace from the missing transitive.

    Methods:

    - ``list_groups()``         → list[WifiGroup]
    - ``list_points(group_id)`` → list[WifiPoint]
    - ``list_clients(group_id)``→ list[WifiClient]

    Each public method runs the underlying async coroutine via
    ``asyncio.run`` and translates upstream exceptions into
    ``StructuredError`` with ``family="wifi"`` so the CLI's error
    envelope carries the discriminator.
    """

    def __init__(self, master_token: str) -> None:
        """Construct against an Android master token.

        Args:
            master_token: The operator's Android master token (the same
                value persisted in ``credentials-wifi.json`` after
                ``auth wifi-setup --experimental-wifi``). Foyer-bearing
                requests are derived from this token by the upstream
                library.

        Raises:
            StructuredError: exit 5 (family=wifi) if ``googlewifi``
                or ``glocaltokens`` are not installed (operator must
                reinstall with ``[wifi]`` extra).
        """
        try:
            from googlewifi import GoogleWifi as _GoogleWifi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise StructuredError(
                code=EXIT_UNSUPPORTED_FEATURE,
                message=(
                    "wifi commands require the optional `[wifi]` extra "
                    f"({type(exc).__name__}: {exc})"
                ),
                hint=_INSTALL_HINT,
                family="wifi",
            ) from exc

        # ``GoogleWifi(refresh_token=<master_token>, session=None)`` —
        # the param is named ``refresh_token`` upstream but semantically
        # carries the operator's Android master token. ``session=None``
        # means the library lazily constructs an aiohttp session per
        # call; we accept that overhead because the CLI is one-shot.
        self._gw_class = _GoogleWifi
        self._master_token = master_token

    # ------------------------------------------------------------------
    # Public surface (sync facades over async upstream)
    # ------------------------------------------------------------------

    def list_groups(self) -> list[WifiGroup]:
        """Return all mesh groups the operator's account owns (FR-WIFI-1).

        Calls ``GoogleWifi.get_systems()`` and normalizes each top-level
        entry into a §10.6 WifiGroup. Returns ``[]`` for an account with
        no groups.
        """
        systems = self._run(self._fetch_systems)
        groups: list[WifiGroup] = []
        for system_id, payload in sorted(systems.items()):
            try:
                groups.append(WifiGroup.from_googlewifi_response(system_id, payload))
            except (KeyError, TypeError, ValueError) as exc:
                raise self._upstream_shape_error(
                    "get_systems",
                    f"normalizing group {system_id!r}: {exc}",
                ) from exc
        return groups

    def list_points(self, group_id: str) -> list[WifiPoint]:
        """Return every point/router in ``group_id`` (FR-WIFI-2).

        Group not found → exit 4 (family=wifi). The upstream library
        returns the full topology in ``get_systems()``; we look up
        ``group_id`` in that map and raise if absent.
        """
        systems = self._run(self._fetch_systems)
        if group_id not in systems:
            raise StructuredError(
                code=EXIT_NOT_FOUND,
                message=f"wifi group {group_id!r} not found",
                hint=(
                    "Run `nest-cli wifi list groups --experimental-wifi` "
                    "to see the groups your account owns."
                ),
                family="wifi",
                details={"group_id": group_id},
            )

        record = systems[group_id]
        access_points = record.get("access_points") or record.get("accessPoints") or {}
        devices = record.get("devices") or record.get("stations") or {}

        # Compute connected_clients_count per ap by walking the devices
        # list and bucketing on apId. ``structure_systems()`` already
        # merges per-station ``apId``, but we tolerate either dict-of-
        # stations or list-of-stations.
        per_ap_count = _count_clients_per_ap(devices)

        points: list[WifiPoint] = []
        ap_records = _iter_dict_records(access_points)
        for ap_record in ap_records:
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
                    "access_points",
                    f"normalizing point: {exc}",
                ) from exc
        # Deterministic order — by id ascending (FR-23).
        return sorted(points, key=lambda p: p.id)

    def list_clients(self, group_id: str) -> list[WifiClient]:
        """Return every connected client in ``group_id`` (FR-WIFI-3).

        Group not found → exit 4 (family=wifi). Walks
        ``get_systems()[group_id]['devices']`` (already enriched by
        ``structure_systems()`` with ``paused`` + ``macAddress`` keys)
        and normalizes each entry into a §10.8 WifiClient.
        """
        systems = self._run(self._fetch_systems)
        if group_id not in systems:
            raise StructuredError(
                code=EXIT_NOT_FOUND,
                message=f"wifi group {group_id!r} not found",
                hint=(
                    "Run `nest-cli wifi list groups --experimental-wifi` "
                    "to see the groups your account owns."
                ),
                family="wifi",
                details={"group_id": group_id},
            )

        record = systems[group_id]
        devices = record.get("devices") or record.get("stations") or {}

        clients: list[WifiClient] = []
        for station in _iter_dict_records(devices):
            try:
                clients.append(WifiClient.from_googlewifi_response(station))
            except (KeyError, TypeError, ValueError) as exc:
                raise self._upstream_shape_error(
                    "devices",
                    f"normalizing client: {exc}",
                ) from exc
        return sorted(clients, key=lambda c: c.id)

    # ------------------------------------------------------------------
    # Per-client actions (FR-WIFI-4..7)
    # ------------------------------------------------------------------

    def pause_station(self, client_id: str) -> None:
        """Pause the named client (FR-WIFI-4).

        Idempotent — pausing an already-paused client is a no-op at the
        Foyer level (SRD §5.4.3). Unknown client_id → exit 4
        (family=wifi). Network / shape errors map per ``_run``.
        """
        self._run(lambda: self._action_pause(client_id, pause_state=True))

    def unpause_station(self, client_id: str) -> None:
        """Unpause the named client (FR-WIFI-5)."""
        self._run(lambda: self._action_pause(client_id, pause_state=False))

    def prioritize_station(self, client_id: str, duration_minutes: int) -> None:
        """Prioritize the named client for ``duration_minutes`` (FR-WIFI-6).

        SRD takes minutes (1..240); the upstream ``prioritize_device``
        takes hours (and self-clamps to 1..6). We ceil-convert minutes
        to hours at this boundary so a 45-minute request still lands as
        a 1-hour boost rather than rounding-down to zero (which the
        library would clamp back to 1 anyway, but we want the conversion
        to be auditable).
        """
        # Ceiling division: 60→1, 61→2, 91→2, 120→2, 240→4.
        duration_hours = (duration_minutes + 59) // 60
        self._run(lambda: self._action_prioritize(client_id, duration_hours))

    def set_station_group(self, client_id: str, group: str | None) -> None:
        """Assign the client to a Foyer group (FR-WIFI-7).

        Phase 3B status: ``googlewifi`` does NOT expose a group-assign
        method. The Foyer endpoint that the Google Home app uses for
        this operation is not currently wrapped by upstream. Rather
        than silently no-op or fake success, this method raises
        ``StructuredError(EXIT_UNSUPPORTED_FEATURE, family="wifi")``
        with a hint pointing at the upstream gap. The CLI verb still
        ships so the operator-facing surface matches FR-WIFI-7; the
        verb wires through, fails clean, and the operator can act on
        the hint (file an issue against ``googlewifi`` upstream, or
        wait for an alternate library).

        Args:
            client_id: The station id. Captured on the structured-error
                ``details`` so operators piping JSONL through ``jq``
                can correlate the failed attempt with their input.
            group: One of ``"family"|"parental"|"guest"|None``, also
                captured on ``details``. ``None`` means the operator
                wanted to remove the assignment.
        """
        raise StructuredError(
            code=EXIT_UNSUPPORTED_FEATURE,
            message=(
                "wifi group-assign is not yet supported by the upstream "
                "googlewifi library (no set_station_group method)."
            ),
            hint=(
                "The Foyer endpoint exists but is not wrapped by "
                "googlewifi as of the version pinned in nest-cli's "
                "[wifi] extra. Track the upstream issue / fork at "
                "https://pypi.org/project/googlewifi/ — once a "
                "set_station_group / set_device_group method ships, "
                "this verb will be re-enabled."
            ),
            family="wifi",
            details={"client_id": client_id, "requested_group": group},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _action_pause(self, client_id: str, *, pause_state: bool) -> None:
        """Resolve group_id then call upstream ``pause_device``.

        Both steps run in the same coroutine so a single ``asyncio.run``
        powers the whole sequence. The fresh ``GoogleWifi`` instance is
        reused across both calls — re-deriving the api token twice for
        a single CLI invocation would be wasteful.
        """
        gw = self._gw_class(refresh_token=self._master_token)
        try:
            systems = await gw.get_systems()
            group_id = _resolve_group_for_client(systems, client_id)
            await gw.pause_device(group_id, client_id, pause_state)
        finally:
            await _maybe_close(gw)

    async def _action_prioritize(self, client_id: str, duration_hours: int) -> None:
        """Resolve group_id then call upstream ``prioritize_device``."""
        gw = self._gw_class(refresh_token=self._master_token)
        try:
            systems = await gw.get_systems()
            group_id = _resolve_group_for_client(systems, client_id)
            await gw.prioritize_device(group_id, client_id, duration_hours)
        finally:
            await _maybe_close(gw)

    async def _fetch_systems(self) -> dict[str, Any]:
        """Drive the upstream ``GoogleWifi.get_systems()`` coroutine.

        Constructs a fresh ``GoogleWifi`` per invocation. The CLI is
        one-shot so connection-reuse buys little; a fresh client per
        call also means a token rotation at Google's end surfaces on
        the next invocation rather than getting stuck on a cached
        ``aiohttp.ClientSession``.
        """
        gw = self._gw_class(refresh_token=self._master_token)
        try:
            systems = await gw.get_systems()
        finally:
            await _maybe_close(gw)
        if not isinstance(systems, dict):
            raise self._upstream_shape_error(
                "get_systems",
                f"expected dict, got {type(systems).__name__}",
            )
        return systems

    def _run(self, coro_factory: Any) -> Any:
        """Run an async coroutine factory under ``asyncio.run`` with mapping.

        ``coro_factory`` is a 0-arg callable returning a coroutine
        (we deliberately don't take a coroutine directly — that
        avoids "coroutine was never awaited" warnings on the
        translation paths). Network-layer errors map to exit 3
        (family=wifi); upstream-shape rotations to exit 1.
        """
        try:
            return asyncio.run(coro_factory())
        except StructuredError:
            raise
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"network error contacting Foyer: {type(exc).__name__}: {exc}",
                hint="Check your internet connection and retry.",
                family="wifi",
            ) from exc
        except (KeyError, AttributeError, TypeError) as exc:
            # SRD §3.2.3: googlewifi rotation surfaces here. Map to
            # exit 1 (device_error) per §11.2 disambiguation table.
            raise self._upstream_shape_error("googlewifi", f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:
            # ``googlewifi.GoogleWifiException`` and other upstream-only
            # exception types — we map any non-network error to exit 1
            # so the operator sees a clean "device error" rather than
            # an opaque traceback. The library version comes through
            # the message body so SRD §3.2.3's correlation goal stays
            # satisfied.
            raise StructuredError(
                code=EXIT_DEVICE_ERROR,
                message=f"upstream wifi library error: {type(exc).__name__}: {exc}",
                hint=(
                    "This is the documented Foyer rotation risk (SRD §3.2.3). "
                    "Check googlewifi / glocaltokens issue trackers; you may need "
                    "to update the optional `[wifi]` extra."
                ),
                family="wifi",
            ) from exc

    @staticmethod
    def _upstream_shape_error(surface: str, detail: str) -> StructuredError:
        """Build a SRD §3.2.3-aligned ``device_error`` for upstream-shape rot.

        Centralized because both ``list_points`` and ``list_clients``
        normalize per-record and need to translate a normalizer
        exception into the same operator-facing message shape.
        """
        return StructuredError(
            code=EXIT_DEVICE_ERROR,
            message=f"googlewifi returned unexpected shape on {surface}: {detail}",
            hint=(
                "This is the documented Foyer rotation risk (SRD §3.2.3). "
                "Check googlewifi / glocaltokens issue trackers; you may need "
                "to update the optional `[wifi]` extra."
            ),
            family="wifi",
        )


# ---------------------------------------------------------------------------
# Module-private helpers (keep iteration logic out of the methods)
# ---------------------------------------------------------------------------


def _resolve_group_for_client(systems: dict[str, Any], client_id: str) -> str:
    """Return the group_id whose ``devices`` map contains ``client_id``.

    Walks every group in ``systems`` (the dict returned by
    ``GoogleWifi.get_systems()``). Returns the first matching group_id;
    if the same client id appears in two groups (unusual but possible
    on multi-mesh accounts), raises EXIT_NOT_FOUND with a disambiguation
    hint so the operator can act on the conflict rather than getting a
    silently wrong group. If the client id appears in no group, raises
    EXIT_NOT_FOUND pointing at ``wifi list clients``.

    Centralized here (not on ``FoyerClient``) so the logic is easy to
    test in isolation and so the module-level type signature is dict-
    in / str-out, no class state.
    """
    matches: list[str] = []
    for group_id, record in systems.items():
        if not isinstance(record, dict):
            continue
        devices = record.get("devices") or record.get("stations") or {}
        # Devices are typically a dict-of-dicts keyed by station id, but
        # tolerate list-shaped responses too (mirrors ``_iter_dict_records``).
        if isinstance(devices, dict) and client_id in devices:
            matches.append(group_id)
            continue
        if isinstance(devices, list):
            for entry in devices:
                if isinstance(entry, dict) and entry.get("id") == client_id:
                    matches.append(group_id)
                    break

    if not matches:
        raise StructuredError(
            code=EXIT_NOT_FOUND,
            message=f"wifi client {client_id!r} not found in any mesh group",
            hint=(
                "Run `nest-cli wifi list clients <group> --experimental-wifi` "
                "for each of your groups to see the connected clients. The "
                "client id is the `id` field on each record, not the friendly "
                "name."
            ),
            family="wifi",
            details={"client_id": client_id},
        )
    if len(matches) > 1:
        raise StructuredError(
            code=EXIT_NOT_FOUND,
            message=(
                f"wifi client {client_id!r} appears in multiple mesh groups "
                f"({', '.join(sorted(matches))}); cannot disambiguate"
            ),
            hint=(
                "The same station id is reported by more than one of your "
                "mesh groups. This is unusual; verify by running "
                "`nest-cli wifi list clients <group>` against each group "
                "and confirm whether the device is actually present in both."
            ),
            family="wifi",
            details={"client_id": client_id, "groups": sorted(matches)},
        )
    return matches[0]


async def _maybe_close(gw: Any) -> None:
    """Call ``close()`` on an upstream GoogleWifi if it exposes one."""
    close = getattr(gw, "close", None)
    if callable(close):
        result = close()
        if asyncio.iscoroutine(result):
            await result


def _iter_dict_records(container: Any) -> list[dict[str, Any]]:
    """Yield dict records from a dict-of-dicts or list-of-dicts container.

    Foyer's payloads use both shapes interchangeably across firmware
    revisions: ``{"id1": {...}, "id2": {...}}`` is common, but some
    list-shaped responses appear too. Non-dict elements are coerced
    to ``{}`` so the caller's normalizer doesn't have to defend against
    them.
    """
    if isinstance(container, dict):
        return [v if isinstance(v, dict) else {} for v in container.values()]
    if isinstance(container, list):
        return [v if isinstance(v, dict) else {} for v in container]
    return []


def _count_clients_per_ap(devices: Any) -> dict[str, int]:
    """Bucket connected clients by ``apId`` (or equivalent key).

    Returns a ``{ap_id: count}`` mapping; AP ids that don't appear
    in the devices list are absent (default ``0`` at the call site).
    Tolerates missing ``apId`` on individual entries by skipping them.
    """
    counts: dict[str, int] = {}
    for station in _iter_dict_records(devices):
        ap_id = station.get("apId") or station.get("ap_id") or station.get("connected_to_point_id")
        if isinstance(ap_id, str) and ap_id:
            counts[ap_id] = counts.get(ap_id, 0) + 1
    return counts

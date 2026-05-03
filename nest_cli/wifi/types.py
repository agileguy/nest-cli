"""Pydantic data records for the wifi (Foyer) surface (SRD §10.6 / §10.7 / §10.8).

Three records are surfaced by ``wifi list``:

- ``WifiGroup``  — one mesh group / "system" the operator's account owns.
- ``WifiPoint``  — one router or access-point inside a group.
- ``WifiClient`` — one connected station (laptop, phone, IoT device).

The on-the-wire shape is normalized via ``from_googlewifi_response``
classmethods. ``googlewifi.GoogleWifi.get_systems()`` returns a dict keyed
by system id; each entry contains ``access_points``, ``devices``, and a
``status`` block. We accept either a dict or an attribute-bag (some forks
of ``googlewifi`` expose dataclass-like objects) and walk the keys
defensively. Unknown upstream fields are dropped — ``extra="forbid"``
locks our wire shape so any new SRD field surfaces as a typed addition
rather than silently leaking arbitrary upstream data through the CLI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

# ---------------------------------------------------------------------------
# Shared helpers — googlewifi response normalizers
# ---------------------------------------------------------------------------


def _as_mapping(obj: Any) -> dict[str, Any]:
    """Coerce an upstream record into a dict.

    ``googlewifi.GoogleWifi.get_systems()`` returns plain dicts in the
    versions we support today, but a future upstream refactor (or a fork
    like ``python-google-wifi``) might return attribute-bag objects. We
    accept both so the FoyerClient's seam stays simple.
    """
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    raise TypeError(f"unsupported googlewifi record type: {type(obj).__name__!r}")


def _opt_str(payload: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string value found at any of ``keys``.

    Foyer's payloads use mixed-case names — sometimes ``ssid``, sometimes
    ``ssidName``, sometimes ``apId`` vs ``ap_id``. The classmethods walk a
    short list of likely keys; the first value that's a non-empty string
    wins. ``None`` if none match.
    """
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _opt_int(payload: dict[str, Any], *keys: str) -> int | None:
    """Return the first int value found at any of ``keys`` (excluding bools)."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _opt_bool(payload: dict[str, Any], *keys: str) -> bool | None:
    """Return the first bool value found at any of ``keys``."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    return None


def _opt_float(payload: dict[str, Any], *keys: str) -> float | None:
    """Return the first numeric value (int or float, not bool) at any of ``keys``."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return float(value)
    return None


# ---------------------------------------------------------------------------
# WifiGroup (SRD §10.6)
# ---------------------------------------------------------------------------


class WifiGroup(BaseModel):
    """One Wi-Fi mesh group ("system") on the operator's Google account.

    SRD §10.6 fields exactly. ``ssid`` is the primary network SSID;
    ``guest_enabled`` is the toggle state of the guest network. Counts
    (``points``, ``clients``) come from the same ``get_systems()``
    response — the FoyerClient enriches the upstream record with these
    counts before constructing the model.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    points: int = Field(..., ge=0)
    clients: int = Field(..., ge=0)
    online: bool
    master_point_id: str = Field(..., min_length=1)
    ssid: str = Field(..., min_length=1)
    guest_enabled: bool

    @classmethod
    def from_googlewifi_response(cls, system_id: str, payload: Any) -> WifiGroup:
        """Build a WifiGroup from a single entry of ``get_systems()``.

        ``system_id`` is the dict key the upstream returns; ``payload``
        is the value (dict or attr-bag describing the group). The
        classmethod normalizes:

        - ``access_points`` → ``points`` count.
        - ``devices`` → ``clients`` count.
        - ``status`` (string from ``wanConnectionStatus``) → ``online``
          bool ("ONLINE" → True, anything else → False).
        - ``groupSettings.apSettings.ssid`` → ``ssid``.
        - ``groupSettings.apSettings.guestSsid``-paired ``enabled`` →
          ``guest_enabled``.
        - First access point with ``isMaster`` (or the lexicographically
          first ap id) → ``master_point_id``.
        """
        record = _as_mapping(payload)

        access_points = record.get("access_points") or record.get("accessPoints") or {}
        if isinstance(access_points, dict):
            ap_ids = sorted(access_points.keys())
            ap_records = [_as_mapping(ap) for ap in access_points.values()]
        elif isinstance(access_points, list):
            ap_records = [_as_mapping(ap) for ap in access_points]
            ap_ids = sorted(
                _opt_str(ap, "id", "apId") or ""
                for ap in ap_records  # type: ignore[arg-type]
            )
        else:
            ap_records = []
            ap_ids = []

        devices = record.get("devices") or record.get("stations") or {}
        client_count = len(devices) if isinstance(devices, dict | list) else 0

        master_id: str | None = None
        for ap in ap_records:
            if _opt_bool(ap, "isMaster", "is_master"):
                master_id = _opt_str(ap, "id", "apId")
                if master_id:
                    break
        if master_id is None and ap_ids:
            master_id = ap_ids[0]
        if master_id is None:
            master_id = system_id  # defensive: lone-master meshes still have one ap

        wan_status = _opt_str(record, "status", "wanConnectionStatus")
        online = wan_status == "ONLINE" if wan_status is not None else True

        ssid = _extract_ssid(record)
        guest_enabled = _extract_guest_enabled(record)

        name = _opt_str(record, "name", "displayName") or ssid

        return cls(
            id=system_id,
            name=name,
            points=len(ap_records),
            clients=client_count,
            online=online,
            master_point_id=master_id,
            ssid=ssid,
            guest_enabled=guest_enabled,
        )


def _extract_ssid(record: dict[str, Any]) -> str:
    """Pull the primary SSID from the nested ``groupSettings`` block.

    Defensive: Foyer's payload sometimes nests this under
    ``groupSettings.apSettings.ssid`` and sometimes flatly as ``ssid``.
    Falls back to ``"<unknown-ssid>"`` if neither path resolves — the
    operator sees a placeholder rather than a crash on the rare
    rotation where the field disappears.
    """
    direct = _opt_str(record, "ssid")
    if direct:
        return direct
    settings = record.get("groupSettings") or {}
    if isinstance(settings, dict):
        ap_settings = settings.get("apSettings") or {}
        if isinstance(ap_settings, dict):
            ssid = _opt_str(ap_settings, "ssid")
            if ssid:
                return ssid
    return "<unknown-ssid>"


def _extract_guest_enabled(record: dict[str, Any]) -> bool:
    """Pull the guest-network toggle from ``groupSettings.guestSsid``.

    The shape we look for is::

        {"groupSettings": {"guestSsid": {"enabled": true, ...}}}

    Any failure path returns False — the safer default for an
    optionally-enabled secondary network.
    """
    settings = record.get("groupSettings") or {}
    if isinstance(settings, dict):
        guest = settings.get("guestSsid") or {}
        if isinstance(guest, dict):
            enabled = _opt_bool(guest, "enabled")
            if enabled is not None:
                return enabled
    return False


# ---------------------------------------------------------------------------
# WifiPoint (SRD §10.7)
# ---------------------------------------------------------------------------


class WifiPoint(BaseModel):
    """One access point / router inside a mesh group (SRD §10.7).

    Fields directly mirror the SRD record. ``mesh_role`` is the
    discriminator that says whether this point is the upstream master
    (the one talking to the WAN) or a satellite mesh node.
    ``signal_strength_to_upstream_dbm`` is the satellite-to-master
    backhaul signal; ``None`` on the master itself (which has no upstream
    point to measure against).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    is_master: bool
    model: str | None = None
    firmware_version: str | None = None
    mesh_role: Literal["master", "satellite"]
    signal_strength_to_upstream_dbm: int | None = None
    connected_clients_count: int = Field(..., ge=0)
    online: bool
    uptime_s: int = Field(..., ge=0)

    @classmethod
    def from_googlewifi_response(
        cls,
        ap_payload: Any,
        *,
        connected_clients_count: int = 0,
    ) -> WifiPoint:
        """Build a WifiPoint from one ``access_points`` dict entry.

        ``connected_clients_count`` is computed by the FoyerClient (it
        counts ``devices`` whose ``apId`` matches this point's id) and
        injected, since the upstream ``get_systems()`` payload doesn't
        carry the count on the per-ap record itself.
        """
        record = _as_mapping(ap_payload)

        ap_id = _opt_str(record, "id", "apId") or ""
        if not ap_id:
            raise ValueError("googlewifi access-point record missing 'id'/'apId'")

        is_master = _opt_bool(record, "isMaster", "is_master") or False
        mesh_role: Literal["master", "satellite"] = "master" if is_master else "satellite"

        ap_status = record.get("status")
        if isinstance(ap_status, dict):
            online = _opt_str(ap_status, "apState", "state") == "ONLINE"
            uptime_s = _opt_int(ap_status, "uptimeSeconds", "uptime_s") or 0
            signal_dbm = _opt_int(ap_status, "signalStrengthDbm", "signal_strength_to_upstream_dbm")
        elif isinstance(ap_status, str):
            online = ap_status == "ONLINE"
            uptime_s = 0
            signal_dbm = None
        else:
            online = False
            uptime_s = 0
            signal_dbm = None

        # Master point: signal-to-upstream isn't meaningful, force None.
        if is_master:
            signal_dbm = None

        return cls(
            id=ap_id,
            name=_opt_str(record, "displayName", "friendlyName", "name") or ap_id,
            is_master=is_master,
            model=_opt_str(record, "model", "hardwareModel"),
            firmware_version=_opt_str(record, "firmwareVersion", "firmware_version"),
            mesh_role=mesh_role,
            signal_strength_to_upstream_dbm=signal_dbm,
            connected_clients_count=connected_clients_count,
            online=online,
            uptime_s=uptime_s,
        )


# ---------------------------------------------------------------------------
# WifiClient (SRD §10.8)
# ---------------------------------------------------------------------------


class WifiClient(BaseModel):
    """One connected station inside a mesh group (SRD §10.8).

    ``connection_type`` distinguishes Wi-Fi clients from Ethernet-attached
    devices (some Nest WiFi points have a LAN port). ``band`` is the
    radio band the client is currently associated with — ``None`` on
    Ethernet clients. ``priority_until`` is RFC 3339 UTC ``Z`` if the
    operator has prioritized this client (Google's "boost" feature) and
    the boost is still active; ``None`` otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    friendly_name: str = Field(..., min_length=1)
    mac: str | None = None
    ip: str | None = None
    connected_to_point_id: str = Field(..., min_length=1)
    connection_type: Literal["wifi", "ethernet"]
    band: Literal["2.4", "5", "6"] | None = None
    tx_rate_mbps: float | None = None
    rx_rate_mbps: float | None = None
    paused: bool
    priority_until: datetime | None = None
    group_assignment: Literal["family", "parental", "guest"] | None = None

    @field_serializer("priority_until", when_used="json")
    def _serialize_priority_until(self, dt: datetime | None) -> str | None:
        """Render ``priority_until`` as RFC 3339 UTC with the literal ``Z`` suffix.

        Pydantic v2's default JSON datetime serializer emits ``+00:00``;
        SRD FR-22 mandates the literal ``Z`` form. ``None`` is preserved
        because the field is optional.
        """
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_googlewifi_response(cls, station_payload: Any) -> WifiClient:
        """Build a WifiClient from one ``devices``/``stations`` entry.

        ``googlewifi.GoogleWifi.get_devices(system_id)`` returns a dict
        with a ``stations`` list; this classmethod takes one element of
        that list. The ``paused`` flag comes from
        ``structure_systems()`` cross-referencing the
        ``familyHubSettings.stationPolicies`` block, which is already
        merged onto each station record before the FoyerClient hands it
        here.
        """
        record = _as_mapping(station_payload)

        station_id = _opt_str(record, "id", "stationId") or ""
        if not station_id:
            raise ValueError("googlewifi station record missing 'id'/'stationId'")

        connection_type: Literal["wifi", "ethernet"] = (
            "ethernet" if _opt_str(record, "connectionType") == "ETHERNET" else "wifi"
        )

        band = _normalize_band(record)
        priority_until = _extract_priority_until(record)
        group_assignment = _normalize_group_assignment(record)

        return cls(
            id=station_id,
            friendly_name=_opt_str(record, "friendlyName", "displayName", "name") or station_id,
            mac=_opt_str(record, "macAddress", "mac"),
            ip=_opt_str(record, "ipAddress", "ip"),
            connected_to_point_id=_opt_str(record, "apId", "connected_to_point_id") or "unknown",
            connection_type=connection_type,
            band=band,
            tx_rate_mbps=_opt_float(record, "txRateMbps", "tx_rate_mbps"),
            rx_rate_mbps=_opt_float(record, "rxRateMbps", "rx_rate_mbps"),
            paused=_opt_bool(record, "paused") or False,
            priority_until=priority_until,
            group_assignment=group_assignment,
        )


def _normalize_band(record: dict[str, Any]) -> Literal["2.4", "5", "6"] | None:
    """Map an upstream band field to one of the SRD's three closed values.

    Foyer reports the band as a frequency-band enum (``BAND_2_4_GHZ``,
    ``BAND_5_GHZ``, ``BAND_6_GHZ``) on Wi-Fi clients and omits it on
    Ethernet. Anything else (or missing) → ``None``.
    """
    raw = _opt_str(record, "band", "frequencyBand")
    if raw is None:
        return None
    if "2_4" in raw or raw == "2.4":
        return "2.4"
    if "5" in raw and "6" not in raw:
        return "5"
    if "6" in raw:
        return "6"
    return None


def _extract_priority_until(record: dict[str, Any]) -> datetime | None:
    """Pull a prioritization expiry timestamp if present.

    The shape we look for is ``{"priorityUntil": "<rfc3339>"}`` or, in
    older payloads, ``{"prioritization": {"endTimestamp": "<rfc3339>"}}``.
    Returns ``None`` if no priority is set or the value can't be parsed.
    """
    raw = _opt_str(record, "priorityUntil", "priority_until")
    if raw is None:
        prioritization = record.get("prioritization") or {}
        if isinstance(prioritization, dict):
            raw = _opt_str(prioritization, "endTimestamp")
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_group_assignment(
    record: dict[str, Any],
) -> Literal["family", "parental", "guest"] | None:
    """Map a Foyer group-assignment value onto the SRD's closed set.

    Foyer's internal policy ids are opaque; ``googlewifi`` flattens them
    onto a ``groupAssignment`` field for the operator to consume, and we
    re-map onto the SRD's three SRD-aligned values. Anything outside the
    set → ``None`` (operator can inspect the raw field via ``-vv``).
    """
    raw = _opt_str(record, "groupAssignment", "group_assignment")
    if raw in ("family", "parental", "guest"):
        return raw  # type: ignore[return-value]
    # Tolerate uppercase variants from the upstream enum surface.
    if isinstance(raw, str):
        lowered = raw.lower()
        if lowered in ("family", "parental", "guest"):
            return lowered  # type: ignore[return-value]
    return None

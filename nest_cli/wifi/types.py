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

__all__ = [
    "SpeedTest",
    "WifiClient",
    "WifiGroup",
    "WifiNetwork",
    "WifiPoint",
    "WifiPointHealth",
]

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


# ---------------------------------------------------------------------------
# SpeedTest (SRD §10.9)
# ---------------------------------------------------------------------------


class SpeedTest(BaseModel):
    """One Wi-Fi speed-test result emitted by the master router (§10.9).

    The Foyer router reports speeds in bits-per-second; the SRD field
    contract is megabits-per-second so ``from_googlewifi_response`` divides
    the upstream value by 1_000_000 at the boundary. ``ts`` is RFC 3339
    UTC; the JSON serializer emits the literal ``Z`` suffix per FR-22.
    ``source`` is locked to ``"router"`` — every speed test in v0.3.1
    runs on the master router. Future surfaces (Ookla, Speedtest.net) will
    be discriminated via this field.
    """

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    group_id: str = Field(..., min_length=1)
    point_id: str = Field(..., min_length=1)
    download_mbps: float = Field(..., ge=0.0)
    upload_mbps: float = Field(..., ge=0.0)
    ping_ms: float = Field(..., ge=0.0)
    source: Literal["router"] = "router"

    @field_serializer("ts", when_used="json")
    def _serialize_ts(self, dt: datetime) -> str:
        """Render ``ts`` as RFC 3339 UTC with the literal ``Z`` (FR-22)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_googlewifi_response(cls, *, group_id: str, payload: Any) -> SpeedTest:
        """Build a SpeedTest from one ``speed_test_results`` entry.

        Foyer's ``GET /v2/groups/{system_id}/speedTestResults`` returns
        a list of objects shaped roughly like::

            {
              "downloadSpeedBps": 900_000_000,
              "uploadSpeedBps":   120_000_000,
              "pingMs":           12.5,
              "timestamp":        "2026-05-02T12:00:00Z",
              "apId":             "ap-master-living-room"
            }

        Field names rotate occasionally on the upstream side; the
        helpers below try several plausible aliases. Bits-per-second
        values are converted to Mbps (divide by 1_000_000) so downstream
        consumers see the SRD §10.9 wire shape directly.
        """
        record = _as_mapping(payload)

        download_bps = _opt_float(record, "downloadSpeedBps", "download_speed_bps")
        upload_bps = _opt_float(record, "uploadSpeedBps", "upload_speed_bps")
        # If upstream gives Mbps directly (older fork / future variant),
        # accept it as the fallback path.
        download_mbps = _opt_float(record, "downloadMbps", "download_mbps")
        upload_mbps = _opt_float(record, "uploadMbps", "upload_mbps")
        if download_mbps is None and download_bps is not None:
            download_mbps = download_bps / 1_000_000.0
        if upload_mbps is None and upload_bps is not None:
            upload_mbps = upload_bps / 1_000_000.0
        if download_mbps is None:
            download_mbps = 0.0
        if upload_mbps is None:
            upload_mbps = 0.0

        ping_ms = _opt_float(record, "pingMs", "ping_ms", "latencyMs")
        if ping_ms is None:
            ping_ms = 0.0

        ts_raw = _opt_str(record, "timestamp", "ts", "time")
        ts = _parse_rfc3339(ts_raw) if ts_raw else datetime.now(tz=UTC)

        point_id = _opt_str(record, "apId", "ap_id", "point_id") or group_id

        return cls(
            ts=ts,
            group_id=group_id,
            point_id=point_id,
            download_mbps=download_mbps,
            upload_mbps=upload_mbps,
            ping_ms=ping_ms,
            source="router",
        )


def _parse_rfc3339(raw: str) -> datetime:
    """Parse an RFC 3339 string with either ``Z`` or ``+HH:MM`` offset.

    Falls back to a UTC ``now()`` if the string can't be parsed —
    same defensive posture as ``_extract_priority_until``.
    """
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# WifiNetwork (SRD §10.10)
# ---------------------------------------------------------------------------


class WifiNetworkIPv4(BaseModel):
    """Nested IPv4 block of WifiNetwork (SRD §10.10)."""

    model_config = ConfigDict(extra="forbid")

    wan: str = Field(..., min_length=1)
    lan_subnet: str = Field(..., min_length=1)
    dhcp_range_start: str = Field(..., min_length=1)
    dhcp_range_end: str = Field(..., min_length=1)


class WifiNetworkIPv6(BaseModel):
    """Nested IPv6 block of WifiNetwork (SRD §10.10).

    ``enabled`` is the toggle; ``wan`` and ``prefix_len`` are populated
    only when IPv6 is on. A disabled IPv6 network surfaces as
    ``{"enabled": false, "wan": null, "prefix_len": null}``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    wan: str | None = None
    prefix_len: int | None = Field(default=None, ge=0, le=128)


class WifiNetwork(BaseModel):
    """Network-level configuration for a Wi-Fi mesh group (SRD §10.10).

    SRD fields exactly. The classmethod ``from_googlewifi_response``
    extracts SSIDs, guest toggle, IPv4/IPv6 WAN+LAN settings, and DNS
    servers from a ``get_systems()`` per-group entry. Missing nested
    fields fall back to ``"<unknown>"`` (strings) or ``False`` (bools)
    so the operator sees a populated record on a sparse Foyer payload
    rather than a 500.
    """

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(..., min_length=1)
    ssid: str = Field(..., min_length=1)
    guest_ssid: str | None = None
    guest_enabled: bool
    ipv4: WifiNetworkIPv4
    ipv6: WifiNetworkIPv6
    dns_servers: list[str]

    @classmethod
    def from_googlewifi_response(cls, group_id: str, payload: Any) -> WifiNetwork:
        """Build a WifiNetwork from a ``get_systems()`` entry.

        Defensive extraction: every nested block is treated as optional.
        Foyer's payload occasionally omits ``wanSettings`` on offline
        groups; we surface ``"<unknown>"`` rather than failing the model.
        """
        record = _as_mapping(payload)
        group_settings = record.get("groupSettings") or {}
        if not isinstance(group_settings, dict):
            group_settings = {}

        ap_settings = group_settings.get("apSettings") or {}
        if not isinstance(ap_settings, dict):
            ap_settings = {}
        ssid = _opt_str(ap_settings, "ssid") or _opt_str(record, "ssid") or "<unknown-ssid>"

        guest = group_settings.get("guestSsid") or {}
        if not isinstance(guest, dict):
            guest = {}
        guest_enabled = _opt_bool(guest, "enabled") or False
        guest_ssid = _opt_str(guest, "ssid")

        ipv4 = _extract_ipv4(record, group_settings)
        ipv6 = _extract_ipv6(record)
        dns_servers = _extract_dns_servers(group_settings)

        return cls(
            group_id=group_id,
            ssid=ssid,
            guest_ssid=guest_ssid,
            guest_enabled=guest_enabled,
            ipv4=ipv4,
            ipv6=ipv6,
            dns_servers=dns_servers,
        )


def _extract_ipv4(record: dict[str, Any], group_settings: dict[str, Any]) -> WifiNetworkIPv4:
    """Build a WifiNetworkIPv4 from a Foyer per-group record.

    WAN address comes from ``wanSettings.ipv4Address``; LAN subnet and
    DHCP range come from ``groupSettings.lanSettings``. Missing values
    fall back to ``"<unknown>"`` so the model stays valid (the field
    is non-empty per the SRD).
    """
    wan_settings = record.get("wanSettings") or {}
    if not isinstance(wan_settings, dict):
        wan_settings = {}
    wan = _opt_str(wan_settings, "ipv4Address", "wan", "ipv4_address") or "<unknown>"

    lan_settings = group_settings.get("lanSettings") or {}
    if not isinstance(lan_settings, dict):
        lan_settings = {}
    lan_subnet = _opt_str(lan_settings, "subnet", "lan_subnet", "cidr") or "<unknown>"

    dhcp_range = lan_settings.get("dhcpRange") or {}
    if not isinstance(dhcp_range, dict):
        dhcp_range = {}
    dhcp_start = _opt_str(dhcp_range, "start", "dhcp_range_start") or "<unknown>"
    dhcp_end = _opt_str(dhcp_range, "end", "dhcp_range_end") or "<unknown>"

    return WifiNetworkIPv4(
        wan=wan,
        lan_subnet=lan_subnet,
        dhcp_range_start=dhcp_start,
        dhcp_range_end=dhcp_end,
    )


def _extract_ipv6(record: dict[str, Any]) -> WifiNetworkIPv6:
    """Build a WifiNetworkIPv6 from a Foyer per-group record.

    ``wanSettings.ipv6.enabled`` toggles the surface; the address and
    prefix length only populate when enabled is true. Disabled / missing
    paths return ``{enabled: false, wan: None, prefix_len: None}``.
    """
    wan_settings = record.get("wanSettings") or {}
    if not isinstance(wan_settings, dict):
        return WifiNetworkIPv6(enabled=False)
    ipv6_block = wan_settings.get("ipv6") or {}
    if not isinstance(ipv6_block, dict):
        return WifiNetworkIPv6(enabled=False)
    enabled = _opt_bool(ipv6_block, "enabled") or False
    if not enabled:
        return WifiNetworkIPv6(enabled=False)
    wan = _opt_str(ipv6_block, "address", "wan")
    prefix_len = _opt_int(ipv6_block, "prefixLength", "prefix_len")
    return WifiNetworkIPv6(enabled=True, wan=wan, prefix_len=prefix_len)


def _extract_dns_servers(group_settings: dict[str, Any]) -> list[str]:
    """Extract DNS servers from ``groupSettings.dnsSettings.dnsServers``."""
    dns_settings = group_settings.get("dnsSettings") or {}
    if not isinstance(dns_settings, dict):
        return []
    raw = dns_settings.get("dnsServers")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


# ---------------------------------------------------------------------------
# WifiPointHealth (SRD §10.11)
# ---------------------------------------------------------------------------


class WifiPointHealth(BaseModel):
    """Health snapshot of a single Wi-Fi point (SRD §10.11).

    A health snapshot is a subset of the WifiPoint record (SRD §10.7) —
    `point-health` is the operator-facing verb for one-shot status
    diagnostics. ``signal_to_upstream_dbm`` is None on the master point
    (no upstream node to measure against) and on offline satellites.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    online: bool
    uptime_s: int = Field(..., ge=0)
    signal_to_upstream_dbm: int | None = None
    connected_clients_count: int = Field(..., ge=0)
    mesh_role: Literal["master", "satellite"]

    @classmethod
    def from_wifi_point(cls, point: WifiPoint) -> WifiPointHealth:
        """Build a WifiPointHealth from an existing WifiPoint record.

        FoyerClient.get_point_health uses this to project the existing
        list_points result onto the §10.11 surface — single source of
        upstream truth, no duplicated normalization logic.
        """
        return cls(
            id=point.id,
            online=point.online,
            uptime_s=point.uptime_s,
            signal_to_upstream_dbm=point.signal_strength_to_upstream_dbm,
            connected_clients_count=point.connected_clients_count,
            mesh_role=point.mesh_role,
        )

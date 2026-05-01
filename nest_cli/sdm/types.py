"""Pydantic data records for the SDM camera surface (SRD §10.1).

The SDM API returns ``traits`` as a JSON object keyed by trait name —
e.g. ``{"sdm.devices.traits.Info": {"customName": "Front Door"}}``. We
normalize that into a list of ``CameraTrait{name, ...}`` records so that
downstream consumers ( ``cam capabilities`` derives ``supported_verbs``,
``cam info`` echoes the list) can iterate over a stable shape.

``CameraTrait`` uses ``extra="allow"`` because SDM's per-trait payloads
are heterogeneous and evolve. We capture the trait ``name`` explicitly
and let everything else fall through.

``Camera`` uses ``extra="forbid"`` so adding a new field upstream that
the CLI doesn't know about lights up in tests rather than being silently
dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class CameraTrait(BaseModel):
    """One entry in a camera's SDM trait list.

    ``name`` is the fully-qualified SDM trait id (e.g.
    ``sdm.devices.traits.Info``). All other fields are passed through
    via ``extra="allow"`` because SDM's trait payloads are not uniform
    and evolve over time.
    """

    model_config = ConfigDict(extra="allow")

    name: str


class Camera(BaseModel):
    """Normalized camera record (SRD §10.1).

    ``target_id`` is the full SDM device path (``enterprises/{proj}/devices/{id}``).
    ``traits`` is a list of ``CameraTrait`` records (the SDM dict is
    flattened on ingestion). The remaining fields are derived from the
    ``Info`` and ``parentRelations`` blocks of the upstream response and
    are nullable when the upstream omits them.
    """

    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(..., min_length=1)
    type: str = Field(..., min_length=1)
    traits: list[CameraTrait] = Field(default_factory=list)
    online: bool | None = None
    room_name: str | None = None
    structure_name: str | None = None
    battery_pct: int | None = None
    signal_strength: int | None = None
    firmware_version: str | None = None
    last_event_ts: datetime | None = None

    @field_serializer("last_event_ts", when_used="json")
    def _serialize_last_event_ts(self, dt: datetime | None) -> str | None:
        """Render ``last_event_ts`` as RFC 3339 UTC with the literal ``Z`` suffix.

        Pydantic v2's default JSON datetime serializer emits ``+00:00``;
        SRD FR-22 mandates the literal ``Z`` form. ``None`` is preserved
        because the field is optional (no recent events).
        """
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def has_trait(self, trait_name: str) -> bool:
        """Return True if ``trait_name`` is in this camera's trait list."""
        return any(t.name == trait_name for t in self.traits)

    @classmethod
    def from_sdm_response(cls, payload: dict[str, Any]) -> Camera:
        """Build a Camera from a raw SDM ``devices.get``/``devices.list`` entry.

        Maps:

        - ``name`` (full path) → ``target_id``
        - ``type`` → ``type`` (no transformation; the CLI surfaces the
          raw enum so ``cam capabilities`` doesn't lose specificity).
        - ``traits`` (dict) → ``traits`` (list of CameraTrait).
        - ``parentRelations[0].displayName`` → ``room_name``.

        Optional fields (battery, signal, firmware) are looked up under
        well-known trait paths if present, else left None. The SDM API
        does not always populate these; the CLI surfaces the gap as
        ``null`` rather than synthesizing a value.
        """
        target_id = payload.get("name") or payload.get("target_id")
        if not isinstance(target_id, str) or not target_id:
            raise ValueError("SDM payload missing required 'name' field")

        device_type = payload.get("type")
        if not isinstance(device_type, str) or not device_type:
            raise ValueError("SDM payload missing required 'type' field")

        raw_traits = payload.get("traits") or {}
        traits: list[CameraTrait] = []
        if isinstance(raw_traits, dict):
            for trait_name, trait_body in raw_traits.items():
                merged: dict[str, Any] = {"name": trait_name}
                if isinstance(trait_body, dict):
                    merged.update(trait_body)
                traits.append(CameraTrait.model_validate(merged))
        elif isinstance(raw_traits, list):
            # Tolerate the alternative list-of-objects shape (some
            # internal SDM endpoints emit this directly).
            for entry in raw_traits:
                if isinstance(entry, dict) and "name" in entry:
                    traits.append(CameraTrait.model_validate(entry))
                elif isinstance(entry, str):
                    traits.append(CameraTrait(name=entry))

        room_name: str | None = None
        parent_relations = payload.get("parentRelations")
        if isinstance(parent_relations, list) and parent_relations:
            first = parent_relations[0]
            if isinstance(first, dict):
                display = first.get("displayName")
                if isinstance(display, str):
                    room_name = display

        return cls(
            target_id=target_id,
            type=device_type,
            traits=traits,
            online=_extract_optional_bool(payload, "online"),
            room_name=room_name,
            structure_name=_extract_optional_str(payload, "structure_name"),
            battery_pct=_extract_optional_int(payload, "battery_pct"),
            signal_strength=_extract_optional_int(payload, "signal_strength"),
            firmware_version=_extract_optional_str(payload, "firmware_version"),
            last_event_ts=_extract_optional_datetime(payload, "last_event_ts"),
        )


def _extract_optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _extract_optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _extract_optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _extract_optional_datetime(payload: dict[str, Any], key: str) -> datetime | None:
    value = payload.get(key)
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None

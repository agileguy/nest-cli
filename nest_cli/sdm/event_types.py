"""Pydantic record + parser for SDM events delivered via Pub/Sub (SRD §10.3).

Per SRD §3.1.3 / §5.3.8 / Decision 10, SDM does not expose events via
REST polling — events flow exclusively through Google Cloud Pub/Sub.
The CLI's ``cam events`` verb pulls from the operator's subscription
and normalizes each Pub/Sub message into the §10.3 ``Event`` record.

Pub/Sub message inner shape (sanitized example):

```
{
  "eventId": "wrap-1",
  "timestamp": "2026-05-03T00:30:00Z",
  "resourceUpdate": {
    "name": "enterprises/{proj}/devices/{id}",
    "events": {
      "sdm.devices.events.CameraMotion.Motion": {
        "eventSessionId": "session-xyz",
        "eventId": "abc-123"           # present → has_image true
      }
    }
  },
  "userId": "...",
  "resourceGroup": [...]
}
```

The inner ``eventId`` (under each event-type entry) — when present —
is the marker for ``CameraEventImage.GenerateImage`` eligibility (SRD
§5.3.2 tier 2 fallback). It is distinct from the outer wrapping
``eventId``. ``has_image`` reflects whether the inner eventId is
present AND the event is within its 30-second image-fetch window.

The mapping from SDM event-type id to the SRD §10.3 ``event_type``
enum is closed:

| SDM id                                            | event_type      |
|---------------------------------------------------|-----------------|
| ``sdm.devices.events.CameraMotion.Motion``        | ``motion``      |
| ``sdm.devices.events.CameraPerson.Person``        | ``person``      |
| ``sdm.devices.events.CameraSound.Sound``          | ``sound``       |
| ``sdm.devices.events.CameraClipPreview.ClipPreview`` | ``unknown``  |
| ``sdm.devices.events.DoorbellChime.Chime``        | ``doorbell-press`` |
| anything else                                     | ``unknown``     |

Note: ``package`` is in the SRD enum (§10.3) for forward compatibility
with hardware that emits a distinct package-detection event id; SDM
v1.0.0 does not currently publish a separate package event id, so the
CLI surfaces it only when we see the topic in the wild.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

# ---------------------------------------------------------------------------
# SDM event-type id → SRD §10.3 event_type enum mapping
# ---------------------------------------------------------------------------

EventTypeEnum = Literal["motion", "person", "package", "sound", "doorbell-press", "unknown"]

_SDM_TYPE_TO_ENUM: dict[str, EventTypeEnum] = {
    "sdm.devices.events.CameraMotion.Motion": "motion",
    "sdm.devices.events.CameraPerson.Person": "person",
    "sdm.devices.events.CameraSound.Sound": "sound",
    "sdm.devices.events.DoorbellChime.Chime": "doorbell-press",
}

# Per SDM, the eventId in an event payload is valid for the
# ``CameraEventImage.GenerateImage`` call for ~30 seconds after the
# event publish_time. SRD §10.3 names ``image_eligibility_window_s``
# as "the seconds remaining" — at parse time we compute
# ``IMAGE_WINDOW_S - (now - publish_time)`` and clamp at 0.
IMAGE_WINDOW_S = 30


class Event(BaseModel):
    """Normalized SDM event (SRD §10.3).

    Fields mirror §10.3 verbatim. ``ts`` serializes as RFC 3339 UTC
    with the literal ``Z`` suffix; the constant ``source: "pubsub"``
    is present on every record (no other source path exists in v1).
    """

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    target: str = Field(..., min_length=1)
    event_type: EventTypeEnum
    has_image: bool
    image_eligibility_window_s: int
    room: str | None = None
    structure: str | None = None
    source: Literal["pubsub"] = "pubsub"

    @field_serializer("ts", when_used="json")
    def _serialize_ts(self, dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_pubsub_event(
    payload: dict[str, Any],
    *,
    publish_time: datetime,
    now: datetime | None = None,
) -> Event | None:
    """Parse a single Pub/Sub message inner payload into an Event.

    Returns ``None`` if the message has no ``resourceUpdate`` /
    ``events`` block we can interpret (e.g., a relation-update message
    that names a renamed device but emits no event entry — the SDM
    Pub/Sub topic publishes those occasionally per §3.1.3).

    ``now`` defaults to ``datetime.now(UTC)`` and is the reference
    point for ``image_eligibility_window_s``. Passing it explicitly is
    helpful in tests.
    """
    resource_update = payload.get("resourceUpdate")
    if not isinstance(resource_update, dict):
        return None

    target = resource_update.get("name")
    if not isinstance(target, str) or not target:
        return None

    events = resource_update.get("events")
    if not isinstance(events, dict) or not events:
        return None

    # Pick the first event entry; SDM rarely publishes more than one
    # per message but the schema technically allows it. If a message
    # has multiple, the caller can repeat the parse on subsequent
    # entries — for v0.2.0 we surface the first.
    sdm_type, event_body = next(iter(events.items()))
    event_type = _SDM_TYPE_TO_ENUM.get(sdm_type, "unknown")

    inner_event_id = event_body.get("eventId") if isinstance(event_body, dict) else None
    has_image = isinstance(inner_event_id, str) and bool(inner_event_id)

    if has_image:
        ref = now or datetime.now(UTC)
        elapsed = ref - publish_time
        remaining_s = IMAGE_WINDOW_S - int(elapsed.total_seconds())
        window_s = max(remaining_s, 0)
    else:
        window_s = 0

    # Prefer the wrapper ``timestamp`` if it parses; fall back to the
    # publish_time the Pub/Sub envelope already gave us.
    ts = _parse_timestamp(payload.get("timestamp")) or publish_time

    return Event(
        ts=ts,
        target=target,
        event_type=event_type,
        has_image=has_image,
        image_eligibility_window_s=window_s,
    )


def _parse_timestamp(value: Any) -> datetime | None:
    """Best-effort parse of an RFC 3339 timestamp string."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = [
    "Event",
    "EventTypeEnum",
    "IMAGE_WINDOW_S",
    "parse_pubsub_event",
]

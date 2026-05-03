"""Pydantic data records for the SDM stream surface (SRD §10.2).

Two raw-result types parse the bytes-on-the-wire shape that SDM's
``executeCommand`` returns for stream commands:

- ``RtspStreamResult`` — the response to ``GenerateRtspStream`` /
  ``ExtendRtspStream``. Carries the rtsps:// URL, the stream token,
  the extension token, and the expiry.
- ``WebRtcStreamResult`` — the response to ``GenerateWebRtcStream`` /
  ``ExtendWebRtcStream``. Carries the answer SDP, the media-session
  identifier, and the expiry.

The CLI-facing record is ``Stream`` (SRD §10.2), a single Pydantic model
with all the fields from both protocol variants. The ``protocol`` field
discriminates between the two; the unused fields are ``None`` on each
variant per the SRD.

Why a single model and not a tagged union?

- Click verbs construct it once at emission time; a discriminated union
  adds typing ergonomics with no operator-visible benefit.
- ``--json`` output uses ``model_dump(mode="json")`` to render the full
  shape including the nullable fields, which matches §10.2 verbatim.
- Downstream tooling pattern-matches on the ``protocol`` literal.

RFC 3339 UTC ``Z`` serialization for ``expires_at`` mirrors the same
``field_serializer`` pattern Engineer A used in ``Camera.last_event_ts``
(SRD FR-22).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class RtspStreamResult(BaseModel):
    """Parsed ``GenerateRtspStream`` / ``ExtendRtspStream`` response.

    Source shape (SDM):
    ``{"results": {"streamUrls": {"rtspUrl": "..."}, "streamToken": "...",
    "streamExtensionToken": "...", "expiresAt": "..."}}``.
    """

    model_config = ConfigDict(extra="ignore")

    url: str
    stream_token: str
    extension_token: str
    expires_at: datetime

    @classmethod
    def from_sdm_response(cls, payload: dict[str, Any]) -> RtspStreamResult:
        results = payload.get("results") or {}
        if not isinstance(results, dict):
            raise ValueError("RTSP stream response missing 'results' object")

        stream_urls = results.get("streamUrls") or {}
        if not isinstance(stream_urls, dict):
            stream_urls = {}

        url = stream_urls.get("rtspUrl")
        if not isinstance(url, str) or not url:
            raise ValueError("RTSP stream response missing 'streamUrls.rtspUrl'")

        stream_token = results.get("streamToken")
        if not isinstance(stream_token, str) or not stream_token:
            raise ValueError("RTSP stream response missing 'streamToken'")

        extension_token = results.get("streamExtensionToken")
        if not isinstance(extension_token, str) or not extension_token:
            raise ValueError("RTSP stream response missing 'streamExtensionToken'")

        expires_at_raw = results.get("expiresAt")
        if not isinstance(expires_at_raw, str) or not expires_at_raw:
            raise ValueError("RTSP stream response missing 'expiresAt'")

        return cls(
            url=url,
            stream_token=stream_token,
            extension_token=extension_token,
            expires_at=_parse_rfc3339(expires_at_raw),
        )


class WebRtcStreamResult(BaseModel):
    """Parsed ``GenerateWebRtcStream`` / ``ExtendWebRtcStream`` response.

    Source shape (SDM):
    ``{"results": {"answerSdp": "...", "expiresAt": "...",
    "mediaSessionId": "..."}}``.
    """

    model_config = ConfigDict(extra="ignore")

    answer_sdp: str
    expires_at: datetime
    media_session_id: str

    @classmethod
    def from_sdm_response(cls, payload: dict[str, Any]) -> WebRtcStreamResult:
        results = payload.get("results") or {}
        if not isinstance(results, dict):
            raise ValueError("WebRTC stream response missing 'results' object")

        answer_sdp = results.get("answerSdp")
        if not isinstance(answer_sdp, str) or not answer_sdp:
            raise ValueError("WebRTC stream response missing 'answerSdp'")

        media_session_id = results.get("mediaSessionId")
        if not isinstance(media_session_id, str) or not media_session_id:
            raise ValueError("WebRTC stream response missing 'mediaSessionId'")

        expires_at_raw = results.get("expiresAt")
        if not isinstance(expires_at_raw, str) or not expires_at_raw:
            raise ValueError("WebRTC stream response missing 'expiresAt'")

        return cls(
            answer_sdp=answer_sdp,
            media_session_id=media_session_id,
            expires_at=_parse_rfc3339(expires_at_raw),
        )


class Stream(BaseModel):
    """CLI-facing Stream record (SRD §10.2).

    Single model with optional fields keyed off ``protocol``. RTSP
    cameras populate ``url``, ``stream_token``, ``extension_token`` and
    leave ``answer_sdp`` / ``media_session_id`` null. WebRTC cameras
    populate ``answer_sdp`` / ``media_session_id`` and leave the RTSP
    fields null. ``expires_at`` is mandatory on both protocols.
    """

    model_config = ConfigDict(extra="forbid")

    target: str = Field(..., min_length=1)
    protocol: Literal["rtsp", "webrtc"]
    expires_at: datetime
    url: str | None = None
    stream_token: str | None = None
    extension_token: str | None = None
    answer_sdp: str | None = None
    media_session_id: str | None = None

    @field_serializer("expires_at", when_used="json")
    def _serialize_expires_at(self, dt: datetime) -> str:
        """Render ``expires_at`` as RFC 3339 UTC with the literal ``Z``.

        Mirrors ``Camera.last_event_ts`` serialization (FR-22 / §10).
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_rtsp_result(cls, *, target: str, result: RtspStreamResult) -> Stream:
        """Build the §10.2 Stream record from a parsed RTSP result."""
        return cls(
            target=target,
            protocol="rtsp",
            expires_at=result.expires_at,
            url=result.url,
            stream_token=result.stream_token,
            extension_token=result.extension_token,
        )

    @classmethod
    def from_webrtc_result(cls, *, target: str, result: WebRtcStreamResult) -> Stream:
        """Build the §10.2 Stream record from a parsed WebRTC result."""
        return cls(
            target=target,
            protocol="webrtc",
            expires_at=result.expires_at,
            answer_sdp=result.answer_sdp,
            media_session_id=result.media_session_id,
        )


def _parse_rfc3339(text: str) -> datetime:
    """Parse a Google-emitted RFC 3339 timestamp into a tz-aware datetime.

    SDM occasionally emits the literal ``Z`` form; ``datetime.fromisoformat``
    on Python 3.11+ handles ``+00:00`` only, so we normalize first.
    """
    return datetime.fromisoformat(text.replace("Z", "+00:00"))

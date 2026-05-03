"""Tests for ``nest_cli.sdm.stream_types`` — Stream record (SRD §10.2).

Covers FR-CAM-7 (RTSP shape) and FR-CAM-8 (WebRTC shape) at the
data-model level. Verb-level emission is covered by ``tests/cam/test_stream*``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from nest_cli.sdm.stream_types import (
    RtspStreamResult,
    Stream,
    WebRtcStreamResult,
)


class TestRtspStreamResult:
    def test_parses_sdm_response(self) -> None:
        raw = {
            "results": {
                "streamUrls": {"rtspUrl": "rtsps://example/stream?auth=tok"},
                "streamExtensionToken": "ext-tok",
                "streamToken": "stream-tok",
                "expiresAt": "2026-05-03T01:00:00Z",
            }
        }
        result = RtspStreamResult.from_sdm_response(raw)
        assert result.url == "rtsps://example/stream?auth=tok"
        assert result.stream_token == "stream-tok"
        assert result.extension_token == "ext-tok"
        assert result.expires_at == datetime(2026, 5, 3, 1, 0, 0, tzinfo=UTC)


class TestWebRtcStreamResult:
    def test_parses_sdm_response(self) -> None:
        raw = {
            "results": {
                "answerSdp": "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n",
                "expiresAt": "2026-05-03T01:00:00Z",
                "mediaSessionId": "ms-1234",
            }
        }
        result = WebRtcStreamResult.from_sdm_response(raw)
        assert result.answer_sdp.startswith("v=0")
        assert result.media_session_id == "ms-1234"
        assert result.expires_at == datetime(2026, 5, 3, 1, 0, 0, tzinfo=UTC)


class TestStreamRtspShape:
    def test_rtsp_stream_serializes_to_srd_record(self) -> None:
        stream = Stream(
            target="front-door",
            protocol="rtsp",
            expires_at=datetime(2026, 5, 3, 1, 0, 0, tzinfo=UTC),
            url="rtsps://example/stream",
            stream_token="stream-tok",  # noqa: S106
            extension_token="ext-tok",  # noqa: S106
        )
        payload = json.loads(stream.model_dump_json())
        assert payload["target"] == "front-door"
        assert payload["protocol"] == "rtsp"
        assert payload["url"] == "rtsps://example/stream"
        assert payload["stream_token"] == "stream-tok"
        assert payload["extension_token"] == "ext-tok"
        # WebRTC fields null on RTSP record per §10.2.
        assert payload["answer_sdp"] is None
        assert payload["media_session_id"] is None
        # RFC 3339 UTC Z form (FR-22).
        assert payload["expires_at"] == "2026-05-03T01:00:00Z"


class TestStreamWebRtcShape:
    def test_webrtc_stream_serializes_to_srd_record(self) -> None:
        stream = Stream(
            target="back-door",
            protocol="webrtc",
            expires_at=datetime(2026, 5, 3, 1, 0, 0, tzinfo=UTC),
            answer_sdp="v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n",
            media_session_id="ms-1234",
        )
        payload = json.loads(stream.model_dump_json())
        assert payload["protocol"] == "webrtc"
        assert payload["answer_sdp"].startswith("v=0")
        assert payload["media_session_id"] == "ms-1234"
        # RTSP fields null on WebRTC record per §10.2.
        assert payload["url"] is None
        assert payload["stream_token"] is None
        assert payload["extension_token"] is None
        assert payload["expires_at"] == "2026-05-03T01:00:00Z"

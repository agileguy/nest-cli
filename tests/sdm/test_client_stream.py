"""Tests for ``nest_cli.sdm.client`` stream methods.

Covers ``generate_rtsp_stream``, ``generate_webrtc_stream``,
``extend_stream``, ``stop_stream`` — the four methods the
``cam stream`` / ``cam stream-extend`` / ``cam stream-stop`` verbs
depend on.

Mocks all HTTP via ``responses``. Refresh path mocked via monkeypatch
on ``refresh_access_token_if_needed`` so we don't hit Google's OAuth
endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses

from nest_cli.auth.types import CamCredentials
from nest_cli.errors import EXIT_DEVICE_ERROR, StructuredError
from nest_cli.sdm.client import SDM_API_ROOT, SdmClient


@pytest.fixture
def fresh_credentials() -> CamCredentials:
    return CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id="proj",
        oauth_client_id="client-id-12345678",
        oauth_client_secret="client-secret",  # noqa: S106
        refresh_token="refresh-tok",  # noqa: S106
        access_token="access-tok",  # noqa: S106
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def credentials_path(tmp_path: Path) -> Path:
    return tmp_path / "credentials-cam.json"


@pytest.fixture
def patch_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_refresh(creds: CamCredentials, path: Path, *, force: bool = False) -> CamCredentials:
        return creds

    monkeypatch.setattr("nest_cli.sdm.client.refresh_access_token_if_needed", _no_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.save_credentials", lambda p, c: None)


_TARGET = "enterprises/proj/devices/doorbell-1"
_EXEC_URL = f"{SDM_API_ROOT}/{_TARGET}:executeCommand"


class TestGenerateRtspStream:
    @responses.activate
    def test_returns_parsed_rtsp_result(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: None,
    ) -> None:
        responses.add(
            responses.POST,
            _EXEC_URL,
            json={
                "results": {
                    "streamUrls": {"rtspUrl": "rtsps://stream.example/abc?auth=tok"},
                    "streamToken": "stream-tok",
                    "streamExtensionToken": "ext-tok",
                    "expiresAt": "2026-05-03T01:00:00Z",
                }
            },
            status=200,
        )
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        result = client.generate_rtsp_stream(_TARGET)
        assert result.url.startswith("rtsps://")
        assert result.stream_token == "stream-tok"
        assert result.extension_token == "ext-tok"
        assert result.expires_at == datetime(2026, 5, 3, 1, 0, 0, tzinfo=UTC)
        # Body should contain the right command id.
        assert len(responses.calls) == 1
        body = responses.calls[0].request.body
        assert b"GenerateRtspStream" in (body if isinstance(body, bytes) else body.encode())


class TestGenerateWebRtcStream:
    @responses.activate
    def test_returns_parsed_webrtc_result_and_sends_offer_sdp(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: None,
    ) -> None:
        responses.add(
            responses.POST,
            _EXEC_URL,
            json={
                "results": {
                    "answerSdp": "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n",
                    "expiresAt": "2026-05-03T01:00:00Z",
                    "mediaSessionId": "ms-9999",
                }
            },
            status=200,
        )
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        result = client.generate_webrtc_stream(_TARGET, offer_sdp="v=0\r\no=offerer\r\n")
        assert result.answer_sdp.startswith("v=0")
        assert result.media_session_id == "ms-9999"
        # Confirm offer SDP was sent.
        body = responses.calls[0].request.body
        body_text = body.decode("utf-8") if isinstance(body, bytes) else body
        assert "GenerateWebRtcStream" in body_text
        assert "offerSdp" in body_text
        assert "offerer" in body_text


class TestExtendStream:
    @responses.activate
    def test_returns_updated_rtsp_result(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: None,
    ) -> None:
        responses.add(
            responses.POST,
            _EXEC_URL,
            json={
                "results": {
                    "streamUrls": {"rtspUrl": "rtsps://stream.example/abc?auth=tok"},
                    "streamToken": "stream-tok-2",
                    "streamExtensionToken": "ext-tok-2",
                    "expiresAt": "2026-05-03T01:05:00Z",
                }
            },
            status=200,
        )
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        result = client.extend_stream(_TARGET, extension_token="ext-tok")
        assert result.extension_token == "ext-tok-2"
        body = responses.calls[0].request.body
        body_text = body.decode("utf-8") if isinstance(body, bytes) else body
        assert "ExtendRtspStream" in body_text
        assert "ext-tok" in body_text


class TestStopStream:
    @responses.activate
    def test_stop_returns_none_on_success(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: None,
    ) -> None:
        responses.add(
            responses.POST,
            _EXEC_URL,
            json={"results": {}},
            status=200,
        )
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        client.stop_stream(_TARGET, extension_token="ext-tok")
        body = responses.calls[0].request.body
        body_text = body.decode("utf-8") if isinstance(body, bytes) else body
        assert "StopRtspStream" in body_text


class TestStreamErrorMapping:
    @responses.activate
    def test_4xx_on_malformed_offer_sdp_maps_to_device_error(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: None,
    ) -> None:
        responses.add(
            responses.POST,
            _EXEC_URL,
            json={"error": {"code": 400, "message": "INVALID_ARGUMENT"}},
            status=400,
        )
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as ei:
            client.generate_webrtc_stream(_TARGET, offer_sdp="garbage")
        assert ei.value.code == EXIT_DEVICE_ERROR

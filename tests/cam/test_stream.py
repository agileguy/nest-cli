"""Tests for ``nest_cli.cli.cam_stream_cmd`` — ``cam stream`` verb.

Covers FR-CAM-6 through FR-CAM-12 — the RTSP / WebRTC stream-negotiate
verb. HTTP mocked via ``responses``; credentials and config paths
redirected to a tmp dir.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

import nest_cli.cli.cam_cmd  # noqa: F401  (resolves monkeypatch path)
from nest_cli.auth.types import CamCredentials
from nest_cli.cli import cli as cli_root
from nest_cli.sdm.client import SDM_API_ROOT


def _write_creds(path: Path) -> None:
    creds = CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id="proj",
        oauth_client_id="client-id-12345678",
        oauth_client_secret="client-secret",  # noqa: S106
        refresh_token="refresh-tok",  # noqa: S106
        access_token="access-tok",  # noqa: S106
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(creds.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    config_path = tmp_path / "config.toml"
    credentials_path = tmp_path / "credentials-cam.json"

    monkeypatch.setattr("nest_cli.config.default_config_path", lambda: config_path)
    monkeypatch.setattr("nest_cli.cli.cam_cmd.default_config_path", lambda: config_path)
    monkeypatch.setattr("nest_cli.cli.cam_stream_cmd.default_config_path", lambda: config_path)
    monkeypatch.setattr("nest_cli.cli._shared.default_credentials_path", lambda: credentials_path)

    def _no_refresh(creds: CamCredentials, path: Path, *, force: bool = False) -> CamCredentials:
        return creds

    monkeypatch.setattr("nest_cli.cli._shared.refresh_access_token_if_needed", _no_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.refresh_access_token_if_needed", _no_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.save_credentials", lambda p, c: None)

    _write_creds(credentials_path)

    return {"config": config_path, "credentials": credentials_path}


_RTSP_TARGET = "enterprises/proj/devices/indoor-1"
_WEBRTC_TARGET = "enterprises/proj/devices/doorbell-1"


def _indoor_payload() -> dict:
    raw = (
        Path(__file__).parent.parent / "fixtures" / "sdm" / "samples" / "sample_indoor_cam.json"
    ).read_text()
    payload = json.loads(raw)
    payload["name"] = _RTSP_TARGET
    return payload


def _doorbell_payload() -> dict:
    raw = (
        Path(__file__).parent.parent
        / "fixtures"
        / "sdm"
        / "samples"
        / "sample_battery_doorbell.json"
    ).read_text()
    payload = json.loads(raw)
    payload["name"] = _WEBRTC_TARGET
    return payload


# ---------------------------------------------------------------------------
# RTSP path (FR-CAM-7)
# ---------------------------------------------------------------------------


class TestCamStreamRtsp:
    @responses.activate
    def test_rtsp_emits_stream_record(self, fake_paths: dict[str, Path]) -> None:
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{_RTSP_TARGET}",
            json=_indoor_payload(),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{_RTSP_TARGET}:executeCommand",
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
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "stream", _RTSP_TARGET, "--json"])
        assert result.exit_code == 0, result.output + result.stderr
        payload = json.loads(result.output)
        assert payload["protocol"] == "rtsp"
        assert payload["url"].startswith("rtsps://")
        assert payload["stream_token"] == "stream-tok"
        assert payload["extension_token"] == "ext-tok"
        assert payload["expires_at"] == "2026-05-03T01:00:00Z"
        assert payload["answer_sdp"] is None
        assert payload["media_session_id"] is None
        assert payload["target"] == _RTSP_TARGET


# ---------------------------------------------------------------------------
# WebRTC path (FR-CAM-8, FR-CAM-9, FR-CAM-10)
# ---------------------------------------------------------------------------


class TestCamStreamWebrtcMissingOfferSdp:
    @responses.activate
    def test_missing_offer_sdp_exits_64(self, fake_paths: dict[str, Path]) -> None:
        # FR-CAM-9: WebRTC camera + no --offer-sdp → exit 64.
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{_WEBRTC_TARGET}",
            json=_doorbell_payload(),
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "stream", _WEBRTC_TARGET, "--json"])
        assert result.exit_code == 64
        # Hint SHOULD reference FR-CAM-8 / §3.1.2.
        assert "offer-sdp" in result.stderr or "offer-sdp" in result.output


class TestCamStreamWebrtcOfferFromFile:
    @responses.activate
    def test_offer_sdp_from_file(self, fake_paths: dict[str, Path], tmp_path: Path) -> None:
        offer_path = tmp_path / "offer.sdp"
        offer_path.write_text("v=0\r\no=offerer\r\n", encoding="utf-8")
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{_WEBRTC_TARGET}",
            json=_doorbell_payload(),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{_WEBRTC_TARGET}:executeCommand",
            json={
                "results": {
                    "answerSdp": "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n",
                    "expiresAt": "2026-05-03T01:00:00Z",
                    "mediaSessionId": "ms-1234",
                }
            },
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            [
                "cam",
                "stream",
                _WEBRTC_TARGET,
                "--offer-sdp",
                str(offer_path),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output + result.stderr
        payload = json.loads(result.output)
        assert payload["protocol"] == "webrtc"
        assert payload["answer_sdp"].startswith("v=0")
        assert payload["media_session_id"] == "ms-1234"
        assert payload["url"] is None
        # Confirm offer SDP made it into the request body.
        post_calls = [c for c in responses.calls if c.request.method == "POST"]
        body = post_calls[0].request.body
        body_text = body.decode("utf-8") if isinstance(body, bytes) else body
        assert "offerer" in body_text


class TestCamStreamWebrtcOfferFromStdin:
    @responses.activate
    def test_offer_sdp_from_stdin(self, fake_paths: dict[str, Path]) -> None:
        # FR-CAM-10: --offer-sdp - reads from stdin.
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{_WEBRTC_TARGET}",
            json=_doorbell_payload(),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{_WEBRTC_TARGET}:executeCommand",
            json={
                "results": {
                    "answerSdp": "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n",
                    "expiresAt": "2026-05-03T01:00:00Z",
                    "mediaSessionId": "ms-1234",
                }
            },
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "stream", _WEBRTC_TARGET, "--offer-sdp", "-", "--json"],
            input="v=0\r\no=stdin-offer\r\n",
        )
        assert result.exit_code == 0, result.output + result.stderr
        post_calls = [c for c in responses.calls if c.request.method == "POST"]
        body = post_calls[0].request.body
        body_text = body.decode("utf-8") if isinstance(body, bytes) else body
        assert "stdin-offer" in body_text


# ---------------------------------------------------------------------------
# --quiet (FR-CAM-12)
# ---------------------------------------------------------------------------


class TestCamStreamQuiet:
    @responses.activate
    def test_quiet_suppresses_stdout_but_keeps_exit_code(self, fake_paths: dict[str, Path]) -> None:
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{_RTSP_TARGET}",
            json=_indoor_payload(),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{_RTSP_TARGET}:executeCommand",
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
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "stream", _RTSP_TARGET, "--quiet"])
        assert result.exit_code == 0
        assert result.output == ""


# ---------------------------------------------------------------------------
# C2 reviewer feedback: protocol-detection failure modes
# ---------------------------------------------------------------------------


def _bare_cam_payload(name: str) -> dict:
    """A camera with NO CameraLiveStream trait at all."""
    return {
        "name": name,
        "type": "sdm.devices.types.CAMERA",
        "traits": {
            "sdm.devices.traits.Info": {"customName": "bare"},
        },
    }


def _live_stream_unknown_protos_payload(name: str) -> dict:
    """A camera whose CameraLiveStream trait carries protocols we don't grok."""
    return {
        "name": name,
        "type": "sdm.devices.types.CAMERA",
        "traits": {
            "sdm.devices.traits.Info": {"customName": "weird"},
            "sdm.devices.traits.CameraLiveStream": {
                "videoCodecs": ["H264"],
                "audioCodecs": ["AAC"],
                "supportedProtocols": ["FUTURE_PROTO"],
            },
        },
    }


class TestCamStreamProtocolDetectFailures:
    """Reviewer feedback (C2): trait-absent vs trait-malformed.

    Prior behaviour silently defaulted to ``webrtc`` when the trait was
    missing OR when ``supportedProtocols`` was empty/unrecognized, and
    then exited 64 with a misleading "missing --offer-sdp" hint. The
    fix distinguishes the two cases:

    - trait absent → exit 5 (UNSUPPORTED_FEATURE)
    - trait present but protocols unrecognized → exit 1 (DEVICE_ERROR)
    """

    @responses.activate
    def test_no_camera_live_stream_trait_exits_5(self, fake_paths: dict[str, Path]) -> None:
        target = "enterprises/proj/devices/bare-cam"
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{target}",
            json=_bare_cam_payload(target),
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "stream", target, "--json"])
        assert result.exit_code == 5
        envelope = json.loads(result.stderr)
        assert envelope["error"] == "unsupported_feature"
        assert "CameraLiveStream" in envelope["message"]

    @responses.activate
    def test_unrecognized_supported_protocols_exits_1(self, fake_paths: dict[str, Path]) -> None:
        target = "enterprises/proj/devices/weird-cam"
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{target}",
            json=_live_stream_unknown_protos_payload(target),
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "stream", target, "--json"])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["error"] == "device_error"
        # Trait list surfaced in details for the bug report.
        traits = envelope["details"]["traits"]
        assert "sdm.devices.traits.CameraLiveStream" in traits

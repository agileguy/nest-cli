"""Tests for ``cam stream-extend`` (FR-CAM-13).

Refresh an active session via SDM ``ExtendRtspStream`` and re-emit the
updated §10.2 Stream record. Missing ``--extension-token`` exits 64.
SDM 4xx exits 1 (device error).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

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


_TARGET = "enterprises/proj/devices/indoor-1"


def _indoor_payload() -> dict:
    raw = (
        Path(__file__).parent.parent / "fixtures" / "sdm" / "samples" / "sample_indoor_cam.json"
    ).read_text()
    payload = json.loads(raw)
    payload["name"] = _TARGET
    return payload


class TestStreamExtendHappyPath:
    @responses.activate
    def test_emits_updated_stream_record(self, fake_paths: dict[str, Path]) -> None:
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{_TARGET}",
            json=_indoor_payload(),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{_TARGET}:executeCommand",
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
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            [
                "cam",
                "stream-extend",
                _TARGET,
                "--extension-token",
                "ext-tok",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output + result.stderr
        payload = json.loads(result.output)
        assert payload["protocol"] == "rtsp"
        assert payload["extension_token"] == "ext-tok-2"
        assert payload["expires_at"] == "2026-05-03T01:05:00Z"


class TestStreamExtendMissingToken:
    def test_missing_extension_token_exits_64(self, fake_paths: dict[str, Path]) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "stream-extend", _TARGET, "--json"],
        )
        assert result.exit_code == 64


class TestStreamExtend4xx:
    @responses.activate
    def test_sdm_4xx_exits_1(self, fake_paths: dict[str, Path]) -> None:
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{_TARGET}",
            json=_indoor_payload(),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{_TARGET}:executeCommand",
            json={"error": {"code": 400, "message": "INVALID_ARGUMENT"}},
            status=400,
        )
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            [
                "cam",
                "stream-extend",
                _TARGET,
                "--extension-token",
                "stale-token",
                "--json",
            ],
        )
        assert result.exit_code == 1

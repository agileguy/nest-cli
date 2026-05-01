"""Tests for ``nest_cli.cli.cam_cmd`` — cam list/info/capabilities.

Covers FR-CAM-1, FR-CAM-2, FR-CAM-28. HTTP mocked via ``responses``;
credentials and config paths redirected to a tmp dir.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

# Import submodules explicitly so monkeypatch can resolve string paths
# like "nest_cli.cli.cam_cmd.default_config_path". The package init
# re-exports the Click command object under the same name, which would
# otherwise shadow the submodule.
import nest_cli.cli.cam_cmd  # noqa: F401
import nest_cli.cli.config_cmd  # noqa: F401
import nest_cli.cli.list_cmd  # noqa: F401
from nest_cli.auth.types import CamCredentials
from nest_cli.cli import cli as cli_root
from nest_cli.sdm.client import SDM_API_ROOT


def _write_creds(path: Path) -> CamCredentials:
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
    return creds


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    config_path = tmp_path / "config.toml"
    credentials_path = tmp_path / "credentials-cam.json"

    def _fake_config_path() -> Path:
        return config_path

    def _fake_creds_path() -> Path:
        return credentials_path

    # Patch every import-time binding of ``default_config_path`` and
    # ``default_credentials_path`` so each verb module sees the tmp paths.
    # The canonical home for the credentials path is now
    # ``nest_cli.cli._shared`` (the shared helper used by every verb).
    monkeypatch.setattr("nest_cli.config.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli.list_cmd.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli.cam_cmd.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli._shared.default_credentials_path", _fake_creds_path)

    def _fake_refresh(creds: CamCredentials, path: Path, *, force: bool = False) -> CamCredentials:
        return creds

    monkeypatch.setattr("nest_cli.cli._shared.refresh_access_token_if_needed", _fake_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.refresh_access_token_if_needed", _fake_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.save_credentials", lambda p, c: None)

    return {"config": config_path, "credentials": credentials_path}


def _doorbell_payload() -> dict:
    raw = (
        Path(__file__).parent / "fixtures" / "sdm" / "samples" / "sample_battery_doorbell.json"
    ).read_text()
    payload = json.loads(raw)
    payload["name"] = "enterprises/proj/devices/doorbell-1"
    return payload


class TestCamInfoHappyPath:
    @responses.activate
    def test_resolves_alias_and_emits_camera_record(self, fake_paths: dict[str, Path]) -> None:
        _write_creds(fake_paths["credentials"])
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/doorbell-1"\n',
            encoding="utf-8",
        )
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/proj/devices/doorbell-1",
            json=_doorbell_payload(),
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "info", "front-door", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["target_id"] == "enterprises/proj/devices/doorbell-1"
        assert payload["type"] == "sdm.devices.types.DOORBELL"
        # The trait list should have at least Info + DoorbellChime.
        trait_names = {t["name"] for t in payload["traits"]}
        assert "sdm.devices.traits.Info" in trait_names
        assert "sdm.devices.traits.DoorbellChime" in trait_names

    @responses.activate
    def test_passes_through_literal_target_id(self, fake_paths: dict[str, Path]) -> None:
        # Literal SDM path (not in aliases) should resolve verbatim.
        _write_creds(fake_paths["credentials"])
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/proj/devices/doorbell-1",
            json=_doorbell_payload(),
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            [
                "cam",
                "info",
                "enterprises/proj/devices/doorbell-1",
                "--json",
            ],
        )
        assert result.exit_code == 0


class TestCamInfoNotFound:
    @responses.activate
    def test_unknown_device_returns_exit_4(self, fake_paths: dict[str, Path]) -> None:
        _write_creds(fake_paths["credentials"])
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/proj/devices/missing",
            json={"error": "not found"},
            status=404,
        )
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            [
                "cam",
                "info",
                "enterprises/proj/devices/missing",
                "--json",
            ],
        )
        assert result.exit_code == 4


class TestCamCapabilities:
    @responses.activate
    def test_emits_traits_and_supported_verbs(self, fake_paths: dict[str, Path]) -> None:
        _write_creds(fake_paths["credentials"])
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/proj/devices/doorbell-1",
            json=_doorbell_payload(),
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            [
                "cam",
                "capabilities",
                "enterprises/proj/devices/doorbell-1",
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["target_id"] == "enterprises/proj/devices/doorbell-1"
        # v0.1.0 universal verbs always present.
        assert "info" in payload["supported_verbs"]
        assert "capabilities" in payload["supported_verbs"]
        assert isinstance(payload["traits"], list)
        # Trait names are surfaced.
        names = {t["name"] for t in payload["traits"]}
        assert "sdm.devices.traits.DoorbellChime" in names


class TestCamList:
    def test_synonym_for_list_family_cam(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n'
            'office-mesh = "wifi:groups/g1"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        # Only the cam alias surfaces.
        assert len(payload) == 1
        assert payload[0]["name"] == "front-door"

"""End-to-end tests for ``cam info @group`` fan-out (FR-6, FR-8, FR-9a).

These tests prove the integration of:

- ``resolve_target_or_group`` (FR-5/FR-6)
- ``fan_out_verb`` (FR-7/FR-8a/FR-8e/FR-9a)
- ``cam info`` per-verb wiring

The SDM client is mocked at the boundary so CI never hits Google.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

import nest_cli.cli.cam_cmd  # noqa: F401 - ensure import-time bindings
from nest_cli.auth.types import CamCredentials
from nest_cli.cli import cli as cli_root
from nest_cli.errors import EXIT_DEVICE_ERROR, StructuredError
from nest_cli.sdm.types import Camera, CameraTrait


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Same harness as the cam test conftest; redirect paths and stub refresh."""
    config_path = tmp_path / "config.toml"
    credentials_path = tmp_path / "credentials-cam.json"

    monkeypatch.setattr("nest_cli.config.default_config_path", lambda: config_path)
    monkeypatch.setattr("nest_cli.cli.cam_cmd.default_config_path", lambda: config_path)
    monkeypatch.setattr("nest_cli.cli._shared.default_credentials_path", lambda: credentials_path)

    def _no_refresh(creds: CamCredentials, path: Path, *, force: bool = False) -> CamCredentials:
        return creds

    monkeypatch.setattr("nest_cli.cli._shared.refresh_access_token_if_needed", _no_refresh)

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
    credentials_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials_path.write_text(creds.model_dump_json(), encoding="utf-8")
    credentials_path.chmod(0o600)
    return {"config": config_path, "credentials": credentials_path}


def _make_camera(target_id: str) -> Camera:
    return Camera(
        target_id=target_id,
        type="sdm.devices.types.CAMERA",
        traits=[CameraTrait(name="sdm.devices.traits.CameraImage")],
    )


@pytest.fixture
def write_config(fake_paths: dict[str, Path]) -> dict[str, Path]:
    fake_paths["config"].write_text(
        "[aliases]\n"
        'front = "enterprises/proj/devices/dF"\n'
        'back = "enterprises/proj/devices/dB"\n'
        'office = "wifi:groups/g1"\n'
        "\n"
        "[groups]\n"
        'home-cams = ["front", "back"]\n'
        'mixed = ["front", "office"]\n',
        encoding="utf-8",
    )
    return fake_paths


class TestCamInfoGroup:
    def test_cam_info_at_group_emits_two_envelopes(
        self, write_config: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`cam info @home-cams` → 2 FR-9a envelopes, both ok, exit 0."""
        cameras = {
            "enterprises/proj/devices/dF": _make_camera("enterprises/proj/devices/dF"),
            "enterprises/proj/devices/dB": _make_camera("enterprises/proj/devices/dB"),
        }

        def _get_device(self: object, target_id: str) -> Camera:  # noqa: ARG001
            return cameras[target_id]

        monkeypatch.setattr("nest_cli.sdm.client.SdmClient.get_device", _get_device)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "info", "@home-cams", "--jsonl"])
        assert result.exit_code == 0, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2
        names = [json.loads(ln)["target"] for ln in lines]
        assert names == ["front", "back"]
        for ln in lines:
            envelope = json.loads(ln)
            assert envelope["status"] == "ok"
            assert envelope["exit_code"] == 0
            assert "result" in envelope
            assert "error" not in envelope

    def test_cam_info_at_group_partial_failure_exits_7(
        self, write_config: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One camera 404s → mixed → exit 7; both envelopes still emitted."""

        def _get_device(self: object, target_id: str) -> Camera:  # noqa: ARG001
            if target_id == "enterprises/proj/devices/dB":
                raise StructuredError(
                    code=EXIT_DEVICE_ERROR,
                    message="device not found upstream",
                )
            return _make_camera(target_id)

        monkeypatch.setattr("nest_cli.sdm.client.SdmClient.get_device", _get_device)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "info", "@home-cams", "--jsonl"])
        assert result.exit_code == 7, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["status"] == "ok"
        assert second["status"] == "error"
        assert second["exit_code"] == 1

    def test_cam_info_mixed_group_emits_exit_5_for_wifi_member(
        self, write_config: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`cam info @mixed` (cam + wifi) → cam OK + wifi exit-5, exit 7."""

        def _get_device(self: object, target_id: str) -> Camera:  # noqa: ARG001
            return _make_camera(target_id)

        monkeypatch.setattr("nest_cli.sdm.client.SdmClient.get_device", _get_device)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "info", "@mixed", "--jsonl"])
        assert result.exit_code == 7, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["target"] == "front"
        assert first["status"] == "ok"
        assert second["target"] == "office"
        assert second["status"] == "error"
        assert second["exit_code"] == 5
        assert second["error"]["code"] == "unsupported_feature"

    def test_cam_info_concurrency_flag_accepted(
        self, write_config: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--concurrency 1`` accepted on a fan-out call; exit 0 still."""

        def _get_device(self: object, target_id: str) -> Camera:  # noqa: ARG001
            return _make_camera(target_id)

        monkeypatch.setattr("nest_cli.sdm.client.SdmClient.get_device", _get_device)

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "info", "@home-cams", "--concurrency", "1", "--jsonl"],
        )
        assert result.exit_code == 0, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2

"""Shared fixtures for the Phase 2 cam-verb test modules.

The four verbs (snapshot, chime, battery, signal) share a common harness:

- ``fake_paths`` — redirects ``default_config_path`` and
  ``default_credentials_path`` to a tmp-dir, plus stubs out the OAuth
  refresh-and-save calls so the SDM client doesn't try to rotate tokens
  against Google.
- ``write_creds`` — writes a far-future-expiry CamCredentials file at the
  patched credentials path so the verb's load-and-refresh sequence
  succeeds.
- ``cam_payload`` factories that emit per-trait-set fixtures (battery
  doorbell vs indoor cam vs custom) for the SDM ``devices.get`` mock.

Tests live alongside this conftest so the layered patching applies
consistently to every verb's CliRunner invocation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# Import the verb modules so monkeypatch can resolve the import-time
# ``default_config_path`` / ``default_credentials_path`` bindings.
import nest_cli.cli.cam_cmd  # noqa: F401
from nest_cli.auth.types import CamCredentials


def _doorbell_payload(name: str = "enterprises/proj/devices/doorbell-1") -> dict[str, Any]:
    """Battery doorbell fixture — DoorbellChime + CameraEventImage, no CameraImage."""
    raw = (
        Path(__file__).parent.parent
        / "fixtures"
        / "sdm"
        / "samples"
        / "sample_battery_doorbell.json"
    ).read_text()
    payload = json.loads(raw)
    payload["name"] = name
    return payload


def _indoor_payload(name: str = "enterprises/proj/devices/indoor-1") -> dict[str, Any]:
    """Indoor cam fixture — CameraImage + CameraEventImage, no DoorbellChime."""
    raw = (
        Path(__file__).parent.parent / "fixtures" / "sdm" / "samples" / "sample_indoor_cam.json"
    ).read_text()
    payload = json.loads(raw)
    payload["name"] = name
    return payload


@pytest.fixture
def doorbell_payload() -> dict[str, Any]:
    """Battery doorbell: DoorbellChime + CameraEventImage; no CameraImage."""
    return _doorbell_payload()


@pytest.fixture
def indoor_payload() -> dict[str, Any]:
    """Indoor cam: CameraImage + CameraEventImage; no DoorbellChime."""
    return _indoor_payload()


@pytest.fixture
def write_creds() -> Any:
    """Return a function that writes a fresh CamCredentials JSON to a path."""

    def _write(path: Path) -> CamCredentials:
        creds = CamCredentials(
            version=1,
            type="oauth",
            google_cloud_project_id="proj",
            oauth_client_id="client-id-12345678",
            oauth_client_secret="client-secret",  # noqa: S106 - fixture
            refresh_token="refresh-tok",  # noqa: S106
            access_token="access-tok",  # noqa: S106
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(creds.model_dump_json(), encoding="utf-8")
        path.chmod(0o600)
        return creds

    return _write


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect config + credentials paths to a tmp dir and stub OAuth refresh.

    Mirrors ``tests/test_cli_cam.py::fake_paths`` so the four Phase 2
    verb-test modules don't each redefine the patching surface.
    """
    config_path = tmp_path / "config.toml"
    credentials_path = tmp_path / "credentials-cam.json"

    def _fake_config_path() -> Path:
        return config_path

    def _fake_creds_path() -> Path:
        return credentials_path

    monkeypatch.setattr("nest_cli.config.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli.cam_cmd.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli.list_cmd.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli._shared.default_credentials_path", _fake_creds_path)

    def _fake_refresh(creds: CamCredentials, path: Path, *, force: bool = False) -> CamCredentials:
        return creds

    monkeypatch.setattr("nest_cli.cli._shared.refresh_access_token_if_needed", _fake_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.refresh_access_token_if_needed", _fake_refresh)
    monkeypatch.setattr("nest_cli.sdm.client.save_credentials", lambda p, c: None)

    return {"config": config_path, "credentials": credentials_path}

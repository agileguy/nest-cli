"""Tests for ``nest_cli.sdm.client`` — SDM REST wrapper.

Mocks all HTTP via the ``responses`` library; no real network. Covers:

- ``list_devices`` happy path with multi-device response.
- ``list_devices`` empty inventory returns ``[]`` (FR-3).
- ``get_device`` happy path returns parsed Camera record.
- 401 → token refresh → retry → success.
- 401 → token refresh → second 401 → exit 2.
- 404 → exit 4.
- 5xx → exit 3.
- Connection error → exit 3.
- ``execute_command`` POSTs to ``:executeCommand`` and parses the result.
- ``execute_command`` 401 → refresh → retry → success.
- ``execute_command`` 401 twice → exit 2.
- ``execute_command`` 404 / 5xx / connection-error mappings.

Token-refresh integration is mocked via monkeypatch on
``refresh_access_token_if_needed`` so we don't need to stub Google's
OAuth endpoint here (Engineer A's tests cover that path).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses

from nest_cli.auth.types import CamCredentials
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    StructuredError,
)
from nest_cli.sdm.client import SDM_API_ROOT, SdmClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fixture_path(name: str) -> Path:
    """Return the on-disk path for a sample SDM device fixture."""
    return Path(__file__).parent.parent / "fixtures" / "sdm" / "samples" / name


@pytest.fixture
def fresh_credentials(tmp_path: Path) -> CamCredentials:
    """A CamCredentials with a far-future expiry — no refresh trigger."""
    return CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id="dan-nest-1234",
        oauth_client_id="client-id-12345678",
        oauth_client_secret="client-secret",  # noqa: S106 - fixture, not real.
        refresh_token="refresh-tok",  # noqa: S106
        access_token="access-tok-current",  # noqa: S106
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def credentials_path(tmp_path: Path) -> Path:
    return tmp_path / "credentials-cam.json"


@pytest.fixture
def patch_refresh(monkeypatch: pytest.MonkeyPatch) -> list[CamCredentials]:
    """Patch ``refresh_access_token_if_needed`` to be a deterministic no-op.

    Records every call into a list returned to the test for assertions.
    The patched function rotates the access token to ``"access-tok-rotated"``
    when ``force=True`` is passed; otherwise returns the input unchanged.
    """
    calls: list[CamCredentials] = []

    def _fake_refresh(
        creds: CamCredentials,
        path: Path,
        *,
        force: bool = False,
    ) -> CamCredentials:
        calls.append(creds)
        if force:
            return creds.model_copy(update={"access_token": "access-tok-rotated"})
        return creds

    # Patch in both the auth.credentials home AND the sdm.client import binding.
    monkeypatch.setattr(
        "nest_cli.auth.credentials.refresh_access_token_if_needed",
        _fake_refresh,
    )
    monkeypatch.setattr(
        "nest_cli.sdm.client.refresh_access_token_if_needed",
        _fake_refresh,
    )
    return calls


@pytest.fixture
def patch_save(monkeypatch: pytest.MonkeyPatch) -> list[CamCredentials]:
    """Patch ``save_credentials`` to capture writes without touching disk."""
    saved: list[CamCredentials] = []

    def _fake_save(path: Path, creds: CamCredentials) -> None:
        saved.append(creds)

    monkeypatch.setattr("nest_cli.sdm.client.save_credentials", _fake_save)
    return saved


# ---------------------------------------------------------------------------
# list_devices
# ---------------------------------------------------------------------------


class TestListDevices:
    @responses.activate
    def test_returns_parsed_cameras_for_multi_device_response(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        doorbell_payload = json.loads(_fixture_path("sample_battery_doorbell.json").read_text())
        indoor_payload = json.loads(_fixture_path("sample_indoor_cam.json").read_text())
        # Make sure the two fixtures have distinct device paths so the
        # parsed Camera list has two distinct ``target_id`` values.
        doorbell_payload["name"] = "enterprises/dan-nest-1234/devices/doorbell-1"
        indoor_payload["name"] = "enterprises/dan-nest-1234/devices/indoor-1"

        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/dan-nest-1234/devices",
            json={"devices": [doorbell_payload, indoor_payload]},
            status=200,
        )

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        cameras = client.list_devices("dan-nest-1234")

        assert len(cameras) == 2
        target_ids = {c.target_id for c in cameras}
        assert target_ids == {
            "enterprises/dan-nest-1234/devices/doorbell-1",
            "enterprises/dan-nest-1234/devices/indoor-1",
        }
        # Doorbell carries the DoorbellChime trait.
        doorbell = next(c for c in cameras if c.target_id.endswith("/doorbell-1"))
        assert doorbell.has_trait("sdm.devices.traits.DoorbellChime")
        # Indoor cam does NOT carry DoorbellChime.
        indoor = next(c for c in cameras if c.target_id.endswith("/indoor-1"))
        assert not indoor.has_trait("sdm.devices.traits.DoorbellChime")

    @responses.activate
    def test_empty_inventory_returns_empty_list(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        # FR-3: zero-result with no error returns []; exit 0 is the
        # caller's responsibility.
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/empty-proj/devices",
            json={"devices": []},
            status=200,
        )

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        cameras = client.list_devices("empty-proj")
        assert cameras == []

    @responses.activate
    def test_missing_devices_field_treated_as_empty(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        # SDM returns ``{}`` (no devices key) when the project has no
        # devices and no list-key. Treat as empty — not an error.
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/missing-key/devices",
            json={},
            status=200,
        )

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        assert client.list_devices("missing-key") == []


# ---------------------------------------------------------------------------
# get_device
# ---------------------------------------------------------------------------


class TestGetDevice:
    @responses.activate
    def test_returns_parsed_camera_record(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        payload = json.loads(_fixture_path("sample_battery_doorbell.json").read_text())
        payload["name"] = "enterprises/dan-nest-1234/devices/doorbell-1"
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/dan-nest-1234/devices/doorbell-1",
            json=payload,
            status=200,
        )

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        camera = client.get_device("enterprises/dan-nest-1234/devices/doorbell-1")
        assert camera.target_id == "enterprises/dan-nest-1234/devices/doorbell-1"
        assert camera.type == "sdm.devices.types.DOORBELL"
        assert camera.has_trait("sdm.devices.traits.Info")


# ---------------------------------------------------------------------------
# Auth retry path
# ---------------------------------------------------------------------------


class TestTokenRefreshRetry:
    @responses.activate
    def test_401_then_refresh_then_success(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
        patch_save: list[CamCredentials],
    ) -> None:
        # First call returns 401; second call (with new token) returns 200.
        url = f"{SDM_API_ROOT}/enterprises/proj/devices/d1"
        responses.add(responses.GET, url, json={"error": "unauth"}, status=401)
        payload = json.loads(_fixture_path("sample_indoor_cam.json").read_text())
        payload["name"] = "enterprises/proj/devices/d1"
        responses.add(responses.GET, url, json=payload, status=200)

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        camera = client.get_device("enterprises/proj/devices/d1")
        assert camera.target_id == "enterprises/proj/devices/d1"
        # Refresh was called twice (lazy + force).
        assert len(patch_refresh) == 2
        # save_credentials was invoked after the forced refresh.
        assert len(patch_save) == 1

    @responses.activate
    def test_401_twice_raises_auth_error(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
        patch_save: list[CamCredentials],
    ) -> None:
        url = f"{SDM_API_ROOT}/enterprises/proj/devices/d1"
        responses.add(responses.GET, url, json={"error": "unauth"}, status=401)
        responses.add(responses.GET, url, json={"error": "still unauth"}, status=401)

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.get_device("enterprises/proj/devices/d1")
        assert exc_info.value.code == EXIT_AUTH_ERROR


# ---------------------------------------------------------------------------
# HTTP failure mappings
# ---------------------------------------------------------------------------


class TestHttpFailureMappings:
    @responses.activate
    def test_404_raises_exit_4(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        url = f"{SDM_API_ROOT}/enterprises/proj/devices/missing"
        responses.add(responses.GET, url, json={"error": "not found"}, status=404)
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.get_device("enterprises/proj/devices/missing")
        assert exc_info.value.code == EXIT_NOT_FOUND

    @responses.activate
    def test_5xx_raises_exit_3(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        url = f"{SDM_API_ROOT}/enterprises/proj/devices/d1"
        responses.add(
            responses.GET,
            url,
            json={"error": "internal"},
            status=503,
        )
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.get_device("enterprises/proj/devices/d1")
        assert exc_info.value.code == EXIT_NETWORK_ERROR

    @responses.activate
    def test_other_4xx_raises_exit_1(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        url = f"{SDM_API_ROOT}/enterprises/proj/devices/d1"
        responses.add(responses.GET, url, json={"error": "bad request"}, status=400)
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.get_device("enterprises/proj/devices/d1")
        assert exc_info.value.code == EXIT_DEVICE_ERROR

    @responses.activate
    def test_connection_error_raises_exit_3(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        # No response registered for the URL → ``responses`` raises a
        # ConnectionError on any non-matching URL, which the SDM client
        # maps to exit 3 (network).
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.get_device("enterprises/proj/devices/d1")
        assert exc_info.value.code == EXIT_NETWORK_ERROR


# ---------------------------------------------------------------------------
# execute_command (POST)
# ---------------------------------------------------------------------------


class TestExecuteCommand:
    """SDM ``executeCommand`` POST surface (FR-CAM-3..27 use this)."""

    @responses.activate
    def test_posts_command_and_returns_parsed_result(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        device = "enterprises/proj/devices/doorbell-1"
        url = f"{SDM_API_ROOT}/{device}:executeCommand"
        responses.add(
            responses.POST,
            url,
            json={"results": {"answerSdp": "v=0\r\n..."}},
            status=200,
        )

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        result = client.execute_command(
            device,
            "sdm.devices.commands.CameraLiveStream.GenerateRtspStream",
            {},
        )
        assert result == {"results": {"answerSdp": "v=0\r\n..."}}
        # Body the client sent should be the SDM-shaped {command, params}.
        request = responses.calls[0].request
        assert request.headers["Authorization"] == "Bearer access-tok-current"
        assert request.headers["Content-Type"] == "application/json"
        body = json.loads(request.body or b"{}")
        assert body == {
            "command": "sdm.devices.commands.CameraLiveStream.GenerateRtspStream",
            "params": {},
        }

    @responses.activate
    def test_passes_params_through(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        device = "enterprises/proj/devices/cam-1"
        url = f"{SDM_API_ROOT}/{device}:executeCommand"
        responses.add(responses.POST, url, json={"results": {}}, status=200)

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        client.execute_command(
            device,
            "sdm.devices.commands.CameraEventImage.GenerateImage",
            {"eventId": "evt-abc-123"},
        )
        body = json.loads(responses.calls[0].request.body or b"{}")
        assert body == {
            "command": "sdm.devices.commands.CameraEventImage.GenerateImage",
            "params": {"eventId": "evt-abc-123"},
        }

    @responses.activate
    def test_401_then_refresh_then_success(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
        patch_save: list[CamCredentials],
    ) -> None:
        device = "enterprises/proj/devices/d1"
        url = f"{SDM_API_ROOT}/{device}:executeCommand"
        responses.add(responses.POST, url, json={"error": "unauth"}, status=401)
        responses.add(responses.POST, url, json={"results": {"ok": True}}, status=200)

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        result = client.execute_command(device, "sdm.devices.commands.X.Y", {})
        assert result == {"results": {"ok": True}}
        assert len(patch_refresh) == 2  # lazy + force
        assert len(patch_save) == 1

    @responses.activate
    def test_401_twice_raises_auth_error(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
        patch_save: list[CamCredentials],
    ) -> None:
        device = "enterprises/proj/devices/d1"
        url = f"{SDM_API_ROOT}/{device}:executeCommand"
        responses.add(responses.POST, url, json={"error": "unauth"}, status=401)
        responses.add(responses.POST, url, json={"error": "still unauth"}, status=401)

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.execute_command(device, "sdm.devices.commands.X.Y", {})
        assert exc_info.value.code == EXIT_AUTH_ERROR

    @responses.activate
    def test_404_raises_exit_4(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        device = "enterprises/proj/devices/missing"
        url = f"{SDM_API_ROOT}/{device}:executeCommand"
        responses.add(responses.POST, url, json={"error": "not found"}, status=404)

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.execute_command(device, "sdm.devices.commands.X.Y", {})
        assert exc_info.value.code == EXIT_NOT_FOUND

    @responses.activate
    def test_5xx_raises_exit_3(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        device = "enterprises/proj/devices/d1"
        url = f"{SDM_API_ROOT}/{device}:executeCommand"
        responses.add(responses.POST, url, json={"error": "internal"}, status=503)

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.execute_command(device, "sdm.devices.commands.X.Y", {})
        assert exc_info.value.code == EXIT_NETWORK_ERROR

    @responses.activate
    def test_other_4xx_raises_exit_1(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        device = "enterprises/proj/devices/d1"
        url = f"{SDM_API_ROOT}/{device}:executeCommand"
        responses.add(responses.POST, url, json={"error": "bad request"}, status=400)

        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.execute_command(device, "sdm.devices.commands.X.Y", {})
        assert exc_info.value.code == EXIT_DEVICE_ERROR

    @responses.activate
    def test_connection_error_raises_exit_3(
        self,
        fresh_credentials: CamCredentials,
        credentials_path: Path,
        patch_refresh: list[CamCredentials],
    ) -> None:
        # No response registered → ConnectionError → exit 3.
        client = SdmClient(fresh_credentials, credentials_path=credentials_path)
        with pytest.raises(StructuredError) as exc_info:
            client.execute_command(
                "enterprises/proj/devices/d1",
                "sdm.devices.commands.X.Y",
                {},
            )
        assert exc_info.value.code == EXIT_NETWORK_ERROR

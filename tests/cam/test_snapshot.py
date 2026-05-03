"""Tests for ``nest-cli cam snapshot`` (FR-CAM-3, 4, 4a, 4b, 4c, 5).

Coverage matrix:

- Tier 1 happy path: CameraImage trait → executeCommand → follow-up GET
  → JPEG bytes written to ``--output <path>``; ``mechanism`` reports
  ``camera_image`` in ``--json`` mode.
- Tier 2 happy path: CameraImage absent + CameraEventImage trait + recent
  eventId → executeCommand → follow-up GET → JPEG bytes;
  ``mechanism`` reports ``camera_event_image``.
- Auth-rejection at any tier: SDM 401 (after refresh) → exit 2 immediately,
  no fallback attempted (FR-CAM-4a).
- Neither trait + no event in window: exit 5 with hint pointing at the
  trait array (FR-CAM-4b).
- ``--output -`` writes JPEG to stdout (FR-CAM-5).
- ``--output -`` combined with ``--json`` exits 64 (FR-CAM-5).
- ``--output -`` combined with ``--jsonl`` exits 64 (FR-CAM-5).

Both tiers' executeCommand returns ``{"results": {"url", "token"}}``;
the verb does a follow-up GET of ``url`` with ``Authorization: Basic <token>``
(matching the SDM CameraImage / CameraEventImage docs) and writes the
returned bytes.

The tier-2 ``eventId`` source is a single seam patched per-test:
``nest_cli.cli.cam_cmd._fetch_recent_event_id``. v0.2.0 stubs it to
``None`` (Pub/Sub not wired yet); tests inject an eventId to exercise
tier 2.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import responses
from click.testing import CliRunner

from nest_cli.cli import cli as cli_root
from nest_cli.sdm.client import SDM_API_ROOT

# --- Synthetic JPEG bytes ---------------------------------------------------
_JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01"
_FAKE_JPEG = _JPEG_HEADER + b"\x00" * 256 + b"\xff\xd9"


# --- Helpers ----------------------------------------------------------------


def _patch_event_id(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Patch the verb's eventId source seam.

    The seam exists so v0.2.0 can ship the fallback control flow without
    Pub/Sub plumbing — tests inject an eventId to exercise tier 2;
    real-world default returns None until Pub/Sub provisioning lands.
    """
    monkeypatch.setattr(
        "nest_cli.cli.cam_cmd._fetch_recent_event_id",
        lambda _client, _camera: value,
    )


# --- Tier 1: CameraImage ----------------------------------------------------


class TestSnapshotTier1:
    @responses.activate
    def test_writes_jpeg_to_output_path_via_camera_image(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"results": {"url": "https://nest-fixture.example/img/abc", "token": "tok-1"}},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://nest-fixture.example/img/abc",
            body=_FAKE_JPEG,
            status=200,
            content_type="image/jpeg",
        )
        _patch_event_id(monkeypatch, None)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "snapshot", device, "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.read_bytes() == _FAKE_JPEG

    @responses.activate
    def test_json_output_reports_camera_image_mechanism(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"results": {"url": "https://nest-fixture.example/img/abc", "token": "tok-1"}},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://nest-fixture.example/img/abc",
            body=_FAKE_JPEG,
            status=200,
            content_type="image/jpeg",
        )
        _patch_event_id(monkeypatch, None)

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", str(out), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["mechanism"] == "camera_image"
        assert payload["target"] == device
        assert payload["bytes"] == len(_FAKE_JPEG)


# --- Tier 2: CameraEventImage fallback ---------------------------------------


class TestSnapshotTier2:
    @responses.activate
    def test_falls_back_to_camera_event_image_when_camera_image_absent(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        doorbell_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doorbell has CameraEventImage but NOT CameraImage → tier 2 used."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/doorbell-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=doorbell_payload, status=200)
        # The verb skips tier 1 entirely (no CameraImage trait) and goes straight to tier 2.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={
                "results": {
                    "url": "https://nest-fixture.example/event-img/xyz",
                    "token": "tok-2",
                }
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://nest-fixture.example/event-img/xyz",
            body=_FAKE_JPEG,
            status=200,
            content_type="image/jpeg",
        )
        _patch_event_id(monkeypatch, "evt-recent-1")

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", str(out), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["mechanism"] == "camera_event_image"
        assert out.read_bytes() == _FAKE_JPEG

        # The POST body should carry the eventId param.
        post_body = json.loads(responses.calls[1].request.body or b"{}")
        assert post_body == {
            "command": "sdm.devices.commands.CameraEventImage.GenerateImage",
            "params": {"eventId": "evt-recent-1"},
        }


# --- FR-CAM-4a: auth-rejection short-circuit --------------------------------


class TestSnapshotAuthShortCircuit:
    @responses.activate
    def test_401_at_tier_1_exits_2_no_fallback(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FR-CAM-4a: auth-rejection at any tier exits 2 immediately."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        # Tier 1 POST returns 401 twice → SDM client raises auth error.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"error": "unauth"},
            status=401,
        )
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"error": "still unauth"},
            status=401,
        )
        # If the verb were to fall back to tier 2, it would attempt another POST.
        # We don't register one — any extra call would be a ConnectionError.
        _patch_event_id(monkeypatch, "evt-recent-1")

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "snapshot", device, "--output", str(out)])
        assert result.exit_code == 2

        # Exactly two POST attempts (the 401-then-retry inside the SDM client),
        # NOT three (a tier-2 fallback would be a third POST).
        post_calls = [c for c in responses.calls if c.request.method == "POST"]
        assert len(post_calls) == 2


# --- FR-CAM-4b: neither trait + no event ------------------------------------


class TestSnapshotNoSupportedMechanism:
    @responses.activate
    def test_no_camera_image_and_no_event_id_exits_5(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        doorbell_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doorbell has CameraEventImage but no recent eventId → tier 2 unavailable.

        With no CameraImage trait either, the verb cannot snapshot and
        SHALL exit 5 (FR-CAM-4b).
        """
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/doorbell-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=doorbell_payload, status=200)
        _patch_event_id(monkeypatch, None)  # no recent event in 60s window

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "snapshot", device, "--output", str(out)])
        assert result.exit_code == 5
        assert not out.exists()

    @responses.activate
    def test_camera_with_no_image_traits_at_all_exits_5(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A camera with NEITHER CameraImage NOR CameraEventImage → exit 5."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/bare-cam"
        bare_payload = {
            "name": device,
            "type": "sdm.devices.types.CAMERA",
            "traits": {
                "sdm.devices.traits.Info": {"customName": "bare"},
                # No CameraImage, no CameraEventImage.
            },
        }
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=bare_payload, status=200)
        _patch_event_id(monkeypatch, None)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "snapshot", device, "--output", str(out)])
        assert result.exit_code == 5


# --- FR-CAM-5: --output - exclusivity ---------------------------------------


class TestSnapshotStdoutOutput:
    @responses.activate
    def test_output_dash_writes_jpeg_to_stdout(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"results": {"url": "https://nest-fixture.example/img/abc", "token": "tok-1"}},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://nest-fixture.example/img/abc",
            body=_FAKE_JPEG,
            status=200,
            content_type="image/jpeg",
        )
        _patch_event_id(monkeypatch, None)

        # CliRunner captures stdout as bytes when mix_stderr=False; we use
        # the default behavior (str output) and verify the JPEG header
        # markers survive the write — Click's text mode preserves binary
        # bytes verbatim when echoed via stdout buffer.
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "snapshot", device, "--output", "-"])
        assert result.exit_code == 0
        # The JPEG SOI marker should be present in stdout output.
        assert b"\xff\xd8\xff" in result.stdout_bytes

    def test_output_dash_with_json_exits_64(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
    ) -> None:
        """FR-CAM-5: --output - and --json are mutually exclusive."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", "-", "--json"],
        )
        assert result.exit_code == 64

    def test_output_dash_with_jsonl_exits_64(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
    ) -> None:
        """FR-CAM-5: --output - and --jsonl are mutually exclusive."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", "-", "--jsonl"],
        )
        assert result.exit_code == 64

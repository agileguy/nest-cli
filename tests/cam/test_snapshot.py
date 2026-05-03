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

    def test_output_dash_with_quiet_exits_64(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
    ) -> None:
        """Reviewer feedback (C5): --output - + --quiet is undefined.

        FR-14 says --quiet suppresses ALL stdout; pairing it with
        --output - would silently consume the snapshot. Reject the
        combination explicitly with exit 64.
        """
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", "-", "--quiet"],
        )
        assert result.exit_code == 64
        assert "--quiet" in result.stderr or "--quiet" in result.output


# --- C6 reviewer feedback: FR-CAM-4 advance-on-failure ----------------------


class TestSnapshotTierAdvanceOnFailure:
    """Reviewer feedback (C6): SRD FR-CAM-4 says "advance on failure".

    Prior implementation only advanced to tier 2 when the camera lacked
    the CameraImage trait — a tier-1 5xx, malformed body, or connection
    error never triggered fallback. The fix wraps tier 1 in
    try/except StructuredError; non-auth failures advance to tier 2.
    EXIT_AUTH_ERROR from any tier still short-circuits per FR-CAM-4a.
    """

    @responses.activate
    def test_tier1_503_falls_back_to_tier2_success(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tier 1 returns 503 → tier 2 attempted → tier 2 succeeds."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        # Tier 1 GenerateImage POST → 503 (mapped to EXIT_NETWORK_ERROR).
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"error": "service unavailable"},
            status=503,
        )
        # Tier 2 GenerateImage POST → 200 with a valid result.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={
                "results": {
                    "url": "https://nest-fixture.example/event-img/xyz",
                    "token": "tok-tier2",
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
        assert result.exit_code == 0, result.output + result.stderr
        payload = json.loads(result.output)
        assert payload["mechanism"] == "camera_event_image"
        assert out.read_bytes() == _FAKE_JPEG

    @responses.activate
    def test_tier1_malformed_body_falls_back_to_tier2(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tier 1 returns 200 with no 'results' object → fall back to tier 2."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        # Tier 1: malformed (no 'results' key).
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"unexpected": "shape"},
            status=200,
        )
        # Tier 2: well-formed.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={
                "results": {
                    "url": "https://nest-fixture.example/event-img/zzz",
                    "token": "tok-tier2",
                }
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://nest-fixture.example/event-img/zzz",
            body=_FAKE_JPEG,
            status=200,
            content_type="image/jpeg",
        )
        _patch_event_id(monkeypatch, "evt-recent-2")

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", str(out), "--json"],
        )
        assert result.exit_code == 0, result.output + result.stderr
        payload = json.loads(result.output)
        assert payload["mechanism"] == "camera_event_image"

    @responses.activate
    def test_tier1_and_tier2_both_fail_surfaces_tier2_error(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tier 1 503 → tier 2 also returns malformed body → exit 1 with tier-2 error."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        # Tier 1: 503.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"error": "service unavailable"},
            status=503,
        )
        # Tier 2: malformed body → EXIT_DEVICE_ERROR.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"unexpected": "shape"},
            status=200,
        )
        _patch_event_id(monkeypatch, "evt-recent-3")

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", str(out), "--json"],
        )
        assert result.exit_code == 1, result.output + result.stderr
        envelope = json.loads(result.stderr)
        # The error message should reference camera_event_image (tier 2),
        # not camera_image (tier 1) — proving tier-2 error is the one
        # that surfaces.
        assert "camera_event_image" in envelope["message"]

    @responses.activate
    def test_tier2_401_exits_2_per_fr_cam_4a(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FR-CAM-4a: auth-rejection at any tier exits 2 immediately.

        Reviewer (Gemini) flagged this case as missing: tier 1 fails
        with a non-auth error, tier 2 attempted, tier 2 returns 401.
        Even though tier 2 is the fallback, FR-CAM-4a still applies —
        no further fallback after auth.
        """
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        out = tmp_path / "snap.jpg"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        # Tier 1: 503 → advance to tier 2.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"error": "service unavailable"},
            status=503,
        )
        # Tier 2 first POST: 401 → SDM client force-refreshes and retries.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"error": "unauth"},
            status=401,
        )
        # Tier 2 retry POST: still 401 → SDM client raises EXIT_AUTH_ERROR.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"error": "still unauth"},
            status=401,
        )
        _patch_event_id(monkeypatch, "evt-recent-4")

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", str(out)],
        )
        assert result.exit_code == 2


# --- C4 reviewer feedback: token leakage in error details -------------------


class TestSnapshotErrorTokenRedaction:
    """Reviewer feedback (C4): never put SDM tokens in error details.

    SDM's GenerateImage response carries a short-lived auth token, and
    the issued image URL sometimes encodes the token in the query string
    (``?auth=<token>``). Putting the raw response dict OR the raw URL in
    StructuredError.details exposes those tokens via stderr where they
    can end up in pasted bug reports.
    """

    @responses.activate
    def test_tier1_malformed_body_does_not_leak_token(
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
        leaky_token = "leaky-tok-do-not-leak"  # noqa: S105 - test fixture token
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        # Tier-1 GenerateImage returns a result that *has* a token but
        # *no* url — triggering the missing-'url' branch. Prior code
        # surfaced the raw inner dict (token included) in details.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"results": {"token": leaky_token}},
            status=200,
        )
        # No CameraEventImage trait on indoor cam beyond what fixture
        # provides; tier-2 may attempt a follow-up POST. Provide a
        # fallback that also fails so the verb returns the tier-1
        # error (or any error). We assert the redaction property on
        # whichever error envelope is emitted.
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"results": {"token": leaky_token}},
            status=200,
        )
        _patch_event_id(monkeypatch, "evt-recent-2")

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", str(out), "--json"],
        )
        # Whatever the tier outcome, the token MUST NOT appear in
        # stderr or stdout anywhere.
        combined = (result.stderr or "") + (result.output or "")
        assert leaky_token not in combined, (
            "SDM token leaked into error output — see C4 reviewer feedback"
        )

    @responses.activate
    def test_image_url_with_query_token_redacted_on_http_error(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If image URL carries ?auth=<token>, error must redact the query."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        out = tmp_path / "snap.jpg"
        leaky_query_token = "queryparam-token-leak"  # noqa: S105 - test fixture
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={
                "results": {
                    "url": f"https://nest-fixture.example/img/abc?auth={leaky_query_token}",
                    "token": "header-tok",
                }
            },
            status=200,
        )
        # Image GET returns 503 — exercise the HTTP-non-200 branch where
        # the URL was previously echoed verbatim into the error message.
        responses.add(
            responses.GET,
            f"https://nest-fixture.example/img/abc?auth={leaky_query_token}",
            status=503,
        )
        _patch_event_id(monkeypatch, None)

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "snapshot", device, "--output", str(out), "--json"],
        )
        combined = (result.stderr or "") + (result.output or "")
        assert leaky_query_token not in combined
        # Redacted form should appear in the message.
        assert "<redacted>" in combined

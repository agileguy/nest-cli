"""Tests for ``nest-cli cam signal`` (FR-CAM-27).

Coverage:

- Happy path: camera with non-null ``signal_strength`` → emit RSSI dBm,
  exit 0.
- Camera without signal_strength → exit 5.
- Last-online timestamp surfaced via ``last_event_ts`` when present.

Like battery, signal-strength is gated on the parsed ``Camera.signal_strength``
field rather than an SDM trait — SDM does not expose a documented trait
for RSSI today.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import responses
from click.testing import CliRunner

from nest_cli.cli import cli as cli_root
from nest_cli.sdm.client import SDM_API_ROOT


class TestSignalHappyPath:
    @responses.activate
    def test_emits_signal_strength_for_camera_with_rssi(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        doorbell_payload: dict[str, Any],
    ) -> None:
        """A camera with non-null signal_strength emits its value at exit 0."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/doorbell-1"
        doorbell_payload["signal_strength"] = -54
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{device}",
            json=doorbell_payload,
            status=200,
        )

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "signal", device, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["target"] == device
        assert payload["signal_strength_dbm"] == -54

    @responses.activate
    def test_includes_last_event_ts_when_present(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        doorbell_payload: dict[str, Any],
    ) -> None:
        """The signal payload SHALL include a last-online timestamp when available.

        The SRD's data model (§10.1) carries ``last_event_ts`` as the
        camera's last-known activity timestamp. When the camera record
        carries it, the verb surfaces it as ``last_online_ts`` in RFC
        3339 UTC ``Z`` form.
        """
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/doorbell-1"
        doorbell_payload["signal_strength"] = -67
        doorbell_payload["last_event_ts"] = "2026-04-30T12:34:56Z"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=doorbell_payload, status=200)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "signal", device, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["last_online_ts"] == "2026-04-30T12:34:56Z"


class TestSignalUnsupported:
    @responses.activate
    def test_no_signal_strength_exits_5(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
    ) -> None:
        """A camera without signal_strength exits 5 (FR-CAM-27)."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "signal", device, "--json"])
        assert result.exit_code == 5

        # Structured-error envelope on stderr (mixed into output by CliRunner).
        env: dict[str, Any] | None = None
        for line in result.output.splitlines():
            line = line.strip()
            if line.startswith("{") and "exit_code" in line:
                env = json.loads(line)
                break
        assert env is not None
        assert env["exit_code"] == 5
        assert env["error"] == "unsupported_feature"

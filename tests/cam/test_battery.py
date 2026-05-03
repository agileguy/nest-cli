"""Tests for ``nest-cli cam battery`` (FR-CAM-26).

Coverage:

- Happy path: camera with non-null ``battery_pct`` → emit the value, exit 0.
- Non-battery camera (``battery_pct`` is null) → exit 5 with
  ``is_battery_powered: false`` and the target name in the structured
  error details.

The ``Camera`` record reads ``battery_pct`` from the top-level SDM
payload. Tests mutate the in-memory fixture dict to inject (or omit)
the field — SDM doesn't currently expose battery state via a documented
trait, so v0.2.0's contract is tested directly against the parsed-record
predicate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import responses
from click.testing import CliRunner

from nest_cli.cli import cli as cli_root
from nest_cli.sdm.client import SDM_API_ROOT


class TestBatteryHappyPath:
    @responses.activate
    def test_emits_battery_pct_for_battery_camera(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        doorbell_payload: dict[str, Any],
    ) -> None:
        """A camera with non-null battery_pct emits its value at exit 0."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/doorbell-1"
        # Inject battery_pct into the SDM response — Camera.from_sdm_response
        # reads it from the top-level payload key.
        doorbell_payload["battery_pct"] = 87
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{device}",
            json=doorbell_payload,
            status=200,
        )

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "battery", device, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["target"] == device
        assert payload["battery_pct"] == 87
        assert payload["is_battery_powered"] is True


class TestBatteryUnsupported:
    @responses.activate
    def test_non_battery_camera_exits_5(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
    ) -> None:
        """A camera without battery_pct exits 5 (FR-CAM-26)."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        # No battery_pct on indoor cam — Camera.battery_pct will be None.
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "battery", device, "--json"])
        assert result.exit_code == 5

    @responses.activate
    def test_exit_5_envelope_carries_is_battery_powered_false(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
    ) -> None:
        """FR-CAM-26: exit-5 details include `is_battery_powered: false` and target."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/indoor-1"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "battery", device, "--json"])
        assert result.exit_code == 5

        # Click's CliRunner mixes stderr into output by default; parse the
        # structured-error envelope from the combined stream.
        combined = result.output
        # Find the JSON object in the combined stream — error envelope is one line.
        env: dict[str, Any] | None = None
        for line in combined.splitlines():
            line = line.strip()
            if line.startswith("{") and "exit_code" in line:
                env = json.loads(line)
                break
        assert env is not None, f"no structured error envelope found in output: {combined!r}"
        assert env["exit_code"] == 5
        assert env["error"] == "unsupported_feature"
        assert env["details"]["target"] == device
        assert env["details"]["is_battery_powered"] is False

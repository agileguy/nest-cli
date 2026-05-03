"""Tests for ``nest-cli cam chime`` (FR-CAM-15, 16).

Coverage:

- Happy path: doorbell with ``DoorbellChime`` trait → executeCommand POST → exit 0.
- Non-doorbell camera (no ``DoorbellChime`` trait) → exit 5 with hint.
- Exit-5 hint lists doorbell-capable aliases from the operator's config.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import responses
from click.testing import CliRunner

from nest_cli.cli import cli as cli_root
from nest_cli.sdm.client import SDM_API_ROOT


class TestChimeHappyPath:
    @responses.activate
    def test_invokes_doorbell_chime_command(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        doorbell_payload: dict[str, Any],
    ) -> None:
        """A camera with the DoorbellChime trait → POST :executeCommand → exit 0."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/doorbell-1"
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/{device}",
            json=doorbell_payload,
            status=200,
        )
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"results": {}},
            status=200,
        )

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "chime", device, "--json"])
        assert result.exit_code == 0, result.output

        # Two HTTP calls: GET devices.get + POST executeCommand.
        assert len(responses.calls) == 2
        post_body = json.loads(responses.calls[1].request.body or b"{}")
        assert post_body == {
            "command": "sdm.devices.commands.DoorbellChime.Chime",
            "params": {},
        }

    @responses.activate
    def test_emits_target_in_json_output(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        doorbell_payload: dict[str, Any],
    ) -> None:
        """The success payload SHALL identify the target so logs can correlate."""
        write_creds(fake_paths["credentials"])
        device = "enterprises/proj/devices/doorbell-1"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=doorbell_payload, status=200)
        responses.add(
            responses.POST,
            f"{SDM_API_ROOT}/{device}:executeCommand",
            json={"results": {}},
            status=200,
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "chime", device, "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["target"] == device


class TestChimeUnsupported:
    @responses.activate
    def test_non_doorbell_camera_exits_5(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
    ) -> None:
        """A camera missing DoorbellChime SHALL exit 5 (FR-CAM-16)."""
        write_creds(fake_paths["credentials"])
        fake_paths["config"].write_text("", encoding="utf-8")
        device = "enterprises/proj/devices/indoor-1"
        responses.add(responses.GET, f"{SDM_API_ROOT}/{device}", json=indoor_payload, status=200)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "chime", device, "--json"])
        assert result.exit_code == 5

        # No POST ever issued — bail before SDM call.
        assert all(call.request.method != "POST" for call in responses.calls)

    @responses.activate
    def test_exit_5_hint_lists_doorbell_aliases(
        self,
        fake_paths: dict[str, Path],
        write_creds: Any,
        indoor_payload: dict[str, Any],
        doorbell_payload: dict[str, Any],
    ) -> None:
        """FR-CAM-16: hint lists cameras in the operator's config that DO support chime.

        Implementation: the verb walks the ``[aliases]`` table, calls
        ``devices.get`` for each, and the hint enumerates the alias names
        whose camera carries DoorbellChime. To keep this test fast and
        free of N+1 SDM calls, we mock both alias lookups.
        """
        write_creds(fake_paths["credentials"])
        fake_paths["config"].write_text(
            "[aliases]\n"
            'front-door = "enterprises/proj/devices/doorbell-1"\n'
            'living-room = "enterprises/proj/devices/indoor-1"\n',
            encoding="utf-8",
        )
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/proj/devices/indoor-1",
            json=indoor_payload,
            status=200,
        )
        # The hint walk needs to inspect each alias.
        responses.add(
            responses.GET,
            f"{SDM_API_ROOT}/enterprises/proj/devices/doorbell-1",
            json=doorbell_payload,
            status=200,
        )

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "chime", "living-room", "--json"],
        )
        assert result.exit_code == 5
        # Structured error envelope on stderr.
        stderr = result.stderr if hasattr(result, "stderr") else ""
        # Click's CliRunner mixes stderr into ``output`` by default; the
        # structured error shows up there. We check that the hint mentions
        # the doorbell-capable alias.
        combined = result.output + stderr
        assert "front-door" in combined

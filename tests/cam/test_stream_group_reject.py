"""``cam stream`` SHALL refuse group targets and exit 64 (FR-8c).

Streaming a group fan-out would multiplex multiple SDM stream sessions
through one stdout, which is a footgun: the operator's downstream
consumer (ffmpeg, mpv) cannot demux JSON envelopes from raw SDM stream
metadata. Refuse early, name the FR explicitly in the hint.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from nest_cli.cli import cli as cli_root


class TestStreamRefusesAtPrefixGroup:
    def test_at_group_exits_64(self) -> None:
        """``cam stream @home-cams`` exits 64 with usage_error."""
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "stream", "@home-cams", "--json"])
        assert result.exit_code == 64, result.output + result.stderr
        envelope = json.loads(result.stderr)
        assert envelope["error"] == "usage_error"
        assert envelope["exit_code"] == 64
        # Hint mentions the FR or per-camera invocation rationale.
        assert "stream" in envelope.get("message", "") or "stream" in envelope.get("hint", "")

    def test_at_group_with_offer_sdp_still_exits_64(self) -> None:
        """The group-rejection check fires BEFORE the offer-sdp validation."""
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "stream", "@home-cams", "--offer-sdp", "-", "--json"],
            input="v=0\n",
        )
        assert result.exit_code == 64
        envelope = json.loads(result.stderr)
        assert envelope["exit_code"] == 64

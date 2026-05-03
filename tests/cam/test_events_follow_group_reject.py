"""``cam events --follow`` SHALL refuse group targets and exit 64 (FR-8d).

One Pub/Sub subscription per stdout. ``cam events`` *without* ``--follow``
MAY accept a group target and fan out (the one-shot drain has bounded
output that fan-out can demultiplex), but the streaming form is exit-64.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from nest_cli.cli import cli as cli_root


class TestFollowRefusesAtPrefixGroup:
    def test_at_group_with_follow_exits_64(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "events", "@home-cams", "--follow", "--json"],
        )
        assert result.exit_code == 64, result.output + result.stderr
        envelope = json.loads(result.stderr)
        assert envelope["error"] == "usage_error"
        assert envelope["exit_code"] == 64
        # The hint references the FR or the one-subscription-per-stdout
        # rationale.
        text = envelope.get("message", "") + envelope.get("hint", "")
        assert "follow" in text or "subscription" in text or "group" in text


class TestEventsOneShotAcceptsGroup:
    """Per FR-8d: ``cam events`` WITHOUT ``--follow`` MAY accept a group.

    We don't fully wire one-shot fan-out for ``cam events`` in v0.4.0
    (the verb already handles a single target filter) — but at minimum
    the verb must NOT reject ``@group`` with exit 64. A successful
    fan-out (or a non-64 failure path) is acceptable.
    """

    def test_at_group_without_follow_does_not_exit_64(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "events", "@home-cams", "--json"],
        )
        # NOT exit 64 — could be 6 (no subscription configured), 4
        # (unknown group), 2 (no creds), or 0 with empty drain. All
        # acceptable; the test is the absence of the FR-8c-style 64.
        assert result.exit_code != 64, result.output + result.stderr

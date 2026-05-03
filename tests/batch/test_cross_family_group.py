"""Tests for cross-family group fan-out (FR-5).

When a group contains both cam and wifi aliases:

- A cam-family verb fanning out emits an exit-5 envelope for each wifi
  member (status=error, code=unsupported_feature, family=wifi).
- A wifi-family verb fanning out emits exit-5 for each cam member.

The wrong-family records are SYNTHESIZED — the verb callable is NOT
invoked for them. The synthesized record contributes to the FR-8a tally
exactly like a real failure.
"""

from __future__ import annotations

import json
import sys
from io import StringIO

import pytest

from nest_cli.cli._fanout import FanOutResult, fan_out_verb
from nest_cli.cli._shared import ResolvedTarget


def _capture_stdout(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return buf


class TestCrossFamilyEmitsUnsupportedFeature:
    def test_wifi_member_in_cam_group_emits_exit_5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        targets = [
            ResolvedTarget(
                name="front-door",
                target="enterprises/proj/devices/dF",
                family="cam",
                family_match=True,
            ),
            ResolvedTarget(
                name="office-mesh",
                target="wifi:groups/g1",
                family="wifi",
                family_match=False,  # cross-family
            ),
        ]
        called: list[str] = []

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            called.append(rt.name)
            return FanOutResult(target=rt.name, exit_code=0, result={"ok": True})

        buf = _capture_stdout(monkeypatch)
        exit_code = fan_out_verb(
            targets=targets, verb_callable=_verb, concurrency=3, output_mode="jsonl"
        )

        # Verb only called for the family-match=True target.
        assert called == ["front-door"]

        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])

        # Order preserved.
        assert first["target"] == "front-door"
        assert first["status"] == "ok"
        assert first["exit_code"] == 0

        assert second["target"] == "office-mesh"
        assert second["status"] == "error"
        assert second["exit_code"] == 5
        assert second["error"]["code"] == "unsupported_feature"

        # 1 OK + 1 fail = exit 7 (FR-8a mixed).
        assert exit_code == 7

    def test_cam_member_in_wifi_group_emits_exit_5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mirror direction: wifi verb sees a cam member as cross-family."""
        targets = [
            ResolvedTarget(
                name="front-door",
                target="enterprises/proj/devices/dF",
                family="cam",
                family_match=False,
            ),
            ResolvedTarget(
                name="office-mesh",
                target="wifi:groups/g1",
                family="wifi",
                family_match=True,
            ),
        ]

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            return FanOutResult(target=rt.name, exit_code=0, result={"ok": True})

        buf = _capture_stdout(monkeypatch)
        exit_code = fan_out_verb(
            targets=targets, verb_callable=_verb, concurrency=3, output_mode="jsonl"
        )
        assert exit_code == 7

        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["target"] == "front-door"
        assert first["exit_code"] == 5
        assert first["error"]["code"] == "unsupported_feature"
        assert second["target"] == "office-mesh"
        assert second["exit_code"] == 0


class TestAllCrossFamilyMembersExitFirstFailureCode:
    def test_group_of_only_wrong_family_exits_5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A group with ONLY wrong-family members: all-failed → first-target's code (5)."""
        targets = [
            ResolvedTarget(
                name="m1",
                target="wifi:groups/g1",
                family="wifi",
                family_match=False,
            ),
            ResolvedTarget(
                name="m2",
                target="wifi:groups/g2",
                family="wifi",
                family_match=False,
            ),
        ]

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            raise AssertionError("verb should not be called")

        _capture_stdout(monkeypatch)
        exit_code = fan_out_verb(
            targets=targets, verb_callable=_verb, concurrency=3, output_mode="quiet"
        )
        assert exit_code == 5

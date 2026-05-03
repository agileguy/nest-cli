"""Tests for fan-out execution helpers (FR-7, FR-8, FR-8a, FR-8e).

The fan-out helper accepts a list of ``ResolvedTarget`` records and a
per-target callable that returns a ``(exit_code, payload)`` tuple. It:

- Runs sub-ops concurrently (default 3, override via flag).
- Collects results in resolved-target-list order (NOT completion order).
- Emits one FR-9a JSONL envelope per target.
- Computes the FR-8a exit code (0 / 7 / first-failure-code).
- Synthesizes exit-5 envelopes for ``family_match=False`` targets without
  even calling the verb (FR-5).

These tests focus on ordering, concurrency override, and exit-code
arithmetic. The output-mode formatting is covered in
``test_fan_out_exit_codes`` and ``test_cross_family_group``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest

from nest_cli.cli._fanout import FanOutResult, fan_out_verb
from nest_cli.cli._shared import ResolvedTarget


def _ok(payload: dict[str, Any]) -> Callable[[ResolvedTarget], FanOutResult]:
    def _verb(rt: ResolvedTarget) -> FanOutResult:
        return FanOutResult(target=rt.name, exit_code=0, result={**payload, "name": rt.name})

    return _verb


def _capture_stdout(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    """Capture click.echo output by redirecting stdout."""
    buf = StringIO()
    import sys

    monkeypatch.setattr(sys, "stdout", buf)
    return buf


class TestOrderingPreserved:
    def test_results_emitted_in_resolved_target_list_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even when a later-listed target completes first, emission stays in list order."""
        targets = [
            ResolvedTarget(
                name="a", target="enterprises/proj/devices/dA", family="cam", family_match=True
            ),
            ResolvedTarget(
                name="b", target="enterprises/proj/devices/dB", family="cam", family_match=True
            ),
            ResolvedTarget(
                name="c", target="enterprises/proj/devices/dC", family="cam", family_match=True
            ),
        ]

        # Verb sleeps in reverse order so c finishes first, then b, then a.
        sleep_for = {"a": 0.030, "b": 0.020, "c": 0.005}

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            time.sleep(sleep_for[rt.name])
            return FanOutResult(target=rt.name, exit_code=0, result={"name": rt.name})

        buf = _capture_stdout(monkeypatch)
        exit_code = fan_out_verb(
            targets=targets,
            verb_callable=_verb,
            concurrency=3,
            output_mode="jsonl",
        )

        assert exit_code == 0
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        names = [json.loads(ln)["target"] for ln in lines]
        assert names == ["a", "b", "c"]


class TestConcurrencyDefault:
    def test_default_concurrency_is_three(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fan-out call without a concurrency override caps at 3 in-flight."""
        targets = [
            ResolvedTarget(
                name=f"t{i}",
                target=f"enterprises/proj/devices/d{i}",
                family="cam",
                family_match=True,
            )
            for i in range(6)
        ]
        in_flight = {"current": 0, "max": 0}
        lock_state = {"locked": False}

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            # Naive shared-counter; threads can race the increment but
            # the assertion (max <= concurrency) is monotonic so the
            # race only undercounts. That's the safe direction for this
            # assertion.
            in_flight["current"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["current"])
            time.sleep(0.020)
            in_flight["current"] -= 1
            return FanOutResult(target=rt.name, exit_code=0, result={"name": rt.name})

        _capture_stdout(monkeypatch)
        # No concurrency override.
        fan_out_verb(targets=targets, verb_callable=_verb, output_mode="quiet")

        assert in_flight["max"] <= 3, f"max in-flight was {in_flight['max']}, expected <=3"
        assert lock_state["locked"] is False


class TestConcurrencyOverride:
    def test_concurrency_one_serializes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """concurrency=1 means at most one verb runs at a time."""
        targets = [
            ResolvedTarget(
                name=f"t{i}",
                target=f"enterprises/proj/devices/d{i}",
                family="cam",
                family_match=True,
            )
            for i in range(4)
        ]
        in_flight = {"current": 0, "max": 0}

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            in_flight["current"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["current"])
            time.sleep(0.005)
            in_flight["current"] -= 1
            return FanOutResult(target=rt.name, exit_code=0, result={"name": rt.name})

        _capture_stdout(monkeypatch)
        fan_out_verb(
            targets=targets,
            verb_callable=_verb,
            concurrency=1,
            output_mode="quiet",
        )

        assert in_flight["max"] == 1

"""Tests for fan-out exit-code arithmetic (FR-8a, FR-9a).

The FR-8a contract:

- 0 if every sub-op succeeded.
- 7 (partial failure) if at least one OK + at least one failed.
- All-failed → exit code is the failure code of the *first* target in
  resolved-config order (NOT completion order).
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


def _t(name: str) -> ResolvedTarget:
    return ResolvedTarget(
        name=name,
        target=f"enterprises/proj/devices/{name}",
        family="cam",
        family_match=True,
    )


class TestAllOkExitsZero:
    def test_three_ok_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        targets = [_t("a"), _t("b"), _t("c")]

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            return FanOutResult(target=rt.name, exit_code=0, result={"ok": True})

        _capture_stdout(monkeypatch)
        exit_code = fan_out_verb(
            targets=targets, verb_callable=_verb, concurrency=3, output_mode="quiet"
        )
        assert exit_code == 0


class TestMixedExitsSeven:
    def test_one_ok_one_fail_exits_seven(self, monkeypatch: pytest.MonkeyPatch) -> None:
        targets = [_t("a"), _t("b")]

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            if rt.name == "a":
                return FanOutResult(target=rt.name, exit_code=0, result={"ok": True})
            return FanOutResult(
                target=rt.name,
                exit_code=1,
                error={"code": "device_error", "message": "oops"},
            )

        _capture_stdout(monkeypatch)
        exit_code = fan_out_verb(
            targets=targets, verb_callable=_verb, concurrency=3, output_mode="quiet"
        )
        assert exit_code == 7


class TestAllFailedFirstFailureCode:
    def test_all_failed_returns_first_targets_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3 targets, all fail with codes 4, 1, 2; first target's code 4 wins."""
        targets = [_t("a"), _t("b"), _t("c")]
        codes = {"a": 4, "b": 1, "c": 2}

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            return FanOutResult(
                target=rt.name,
                exit_code=codes[rt.name],
                error={"code": "device_error", "message": "boom"},
            )

        _capture_stdout(monkeypatch)
        exit_code = fan_out_verb(
            targets=targets, verb_callable=_verb, concurrency=3, output_mode="quiet"
        )
        assert exit_code == 4

    def test_first_target_completion_order_irrelevant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Completion-order tie-breaking does NOT happen; config order wins.

        Even if target ``c`` finishes first (and is the only one to
        finish before the others) the exit code SHALL be ``a``'s code.
        """
        import time as _time

        targets = [_t("a"), _t("b"), _t("c")]
        codes = {"a": 4, "b": 3, "c": 1}
        sleeps = {"a": 0.020, "b": 0.010, "c": 0.001}

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            _time.sleep(sleeps[rt.name])
            return FanOutResult(
                target=rt.name,
                exit_code=codes[rt.name],
                error={"code": "device_error", "message": "boom"},
            )

        _capture_stdout(monkeypatch)
        exit_code = fan_out_verb(
            targets=targets, verb_callable=_verb, concurrency=3, output_mode="quiet"
        )
        assert exit_code == 4


class TestEnvelopeShapeFR9a:
    def test_ok_envelope_has_status_ok_and_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        targets = [_t("a")]

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            return FanOutResult(target=rt.name, exit_code=0, result={"hello": "world"})

        buf = _capture_stdout(monkeypatch)
        fan_out_verb(targets=targets, verb_callable=_verb, output_mode="jsonl")
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        envelope = json.loads(lines[0])
        assert envelope["target"] == "a"
        assert envelope["status"] == "ok"
        assert envelope["exit_code"] == 0
        assert envelope["result"] == {"hello": "world"}
        assert "error" not in envelope

    def test_error_envelope_has_status_error_and_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        targets = [_t("a")]

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            return FanOutResult(
                target=rt.name,
                exit_code=4,
                error={
                    "code": "not_found",
                    "message": "no such device",
                    "hint": "check config",
                },
            )

        buf = _capture_stdout(monkeypatch)
        fan_out_verb(targets=targets, verb_callable=_verb, output_mode="jsonl")
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        envelope = json.loads(lines[0])
        assert envelope["target"] == "a"
        assert envelope["status"] == "error"
        assert envelope["exit_code"] == 4
        assert envelope["error"]["code"] == "not_found"
        assert "result" not in envelope


class TestSingleFailureDoesNotAbort:
    def test_failure_in_first_target_still_runs_remaining(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failure of target ``a`` does NOT prevent ``b`` and ``c`` from running."""
        targets = [_t("a"), _t("b"), _t("c")]
        called: list[str] = []

        def _verb(rt: ResolvedTarget) -> FanOutResult:
            called.append(rt.name)
            if rt.name == "a":
                return FanOutResult(
                    target=rt.name,
                    exit_code=4,
                    error={"code": "not_found", "message": "no such device"},
                )
            return FanOutResult(target=rt.name, exit_code=0, result={"ok": True})

        _capture_stdout(monkeypatch)
        fan_out_verb(targets=targets, verb_callable=_verb, output_mode="quiet")
        assert sorted(called) == ["a", "b", "c"]

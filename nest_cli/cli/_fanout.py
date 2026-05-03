"""Fan-out execution helper for group-target verbs (Phase 4).

Implements the per-verb mechanics that satisfy:

- **FR-7** — concurrent execution of group sub-ops up to a configurable
  cap (default 3).
- **FR-8** — per-device success/failure independence.
- **FR-8a** — group exit code arithmetic (0 / 7 / first-failure-code).
- **FR-8e** — emission order matches the resolved-alias-list, NOT the
  completion order.
- **FR-9a** — JSONL envelope shape: ``{target, status, exit_code,
  result?, error?}``.
- **FR-5** — cross-family members (``family_match=False`` on the
  ``ResolvedTarget``) get a synthesized exit-5 envelope without the
  per-target callable being invoked.

The helper is intentionally agnostic about how a verb maps to a
``FanOutResult`` — the verb body or its CliRunner-driven dispatcher
constructs and returns the result; this module only orchestrates.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import click

from nest_cli.cli._shared import ResolvedTarget
from nest_cli.errors import EXIT_OK, EXIT_PARTIAL_FAILURE, EXIT_UNSUPPORTED_FEATURE
from nest_cli.output import OutputMode

# FR-7 default concurrency. Lower than tapo-cli's 5 because Google's
# APIs (SDM + Foyer) rate-limit harder than the local-LAN tapo path.
DEFAULT_CONCURRENCY = 3


@dataclass
class FanOutResult:
    """One sub-op's result, ready for FR-9a envelope emission.

    Either ``result`` or ``error`` is set; both must not be set
    simultaneously. ``status`` is derived in
    :func:`_to_envelope` from the presence of ``error``.
    """

    target: str
    exit_code: int
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = field(default=None)


def _to_envelope(result: FanOutResult) -> dict[str, Any]:
    """Render a ``FanOutResult`` to the FR-9a JSONL envelope.

    Shape (matches SRD §5.7 and tapo-cli FR-44a):

    - ``target`` — alias name or literal target string.
    - ``status`` — ``"ok"`` if ``error`` is None, ``"error"`` otherwise.
    - ``exit_code`` — the integer exit code.
    - ``result`` — present iff ``status == "ok"``.
    - ``error`` — present iff ``status == "error"``; the ``code`` /
      ``message`` / ``hint`` triple from §11.2.
    """
    envelope: dict[str, Any] = {
        "target": result.target,
        "status": "ok" if result.error is None else "error",
        "exit_code": result.exit_code,
    }
    if result.error is None:
        envelope["result"] = result.result if result.result is not None else {}
    else:
        envelope["error"] = result.error
    return envelope


def _synthesize_cross_family_result(rt: ResolvedTarget) -> FanOutResult:
    """Build the FR-5 exit-5 envelope for a wrong-family group member.

    The verb body is NOT invoked for cross-family members. Instead the
    fan-out helper synthesizes an ``unsupported_feature`` record so
    operators see a clean per-target trace without the verb having to
    plumb a family-mismatch branch in every implementation.
    """
    return FanOutResult(
        target=rt.name,
        exit_code=EXIT_UNSUPPORTED_FEATURE,
        error={
            "code": "unsupported_feature",
            "message": (
                f"target {rt.name!r} is family {rt.family!r}; "
                "this verb operates on the other family"
            ),
            "hint": (
                "Cross-family group memberships are allowed (FR-5) but each "
                "verb only operates on its own family. Split the group, or "
                "invoke the matching verb against the other family's members."
            ),
        },
    )


def _aggregate_exit_code(results: list[FanOutResult]) -> int:
    """Compute the FR-8a group exit code from per-target results.

    - 0 if every result has ``exit_code == 0``.
    - 7 if at least one OK + at least one failed.
    - All-failed → first target's exit code (config-file order).
    """
    if not results:
        return EXIT_OK
    ok_count = sum(1 for r in results if r.exit_code == EXIT_OK)
    if ok_count == len(results):
        return EXIT_OK
    if ok_count == 0:
        # All failed — return the exit code of the first target in the
        # resolved-target-list order, NOT the completion order.
        return results[0].exit_code
    return EXIT_PARTIAL_FAILURE


def _emit_envelopes(envelopes: list[dict[str, Any]], output_mode: OutputMode) -> None:
    """Emit the FR-9a envelopes per ``output_mode``.

    Behavior:

    - ``quiet`` — emit nothing (FR-14 mirror).
    - ``json``  — emit a single JSON array containing every envelope.
    - ``jsonl`` (default) — one JSON object per line.
    - ``text``  — one ``key: value`` block per envelope. Operators who
      ask for text mode on a group target get a degraded but still
      readable rendering; programmatic consumers should pass
      ``--jsonl``.
    """
    if output_mode == "quiet":
        return
    if output_mode == "json":
        click.echo(json.dumps(envelopes, indent=2, sort_keys=True))
        return
    # jsonl AND text both emit one object per line; text adds key:value
    # rendering for operators on a tty.
    for env in envelopes:
        if output_mode == "text":
            for k, v in env.items():
                click.echo(f"{k}: {v}")
            click.echo("")
        else:
            click.echo(json.dumps(env, sort_keys=True))


def fan_out_verb(
    *,
    targets: list[ResolvedTarget],
    verb_callable: Callable[[ResolvedTarget], FanOutResult],
    output_mode: OutputMode,
    concurrency: int | None = None,
) -> int:
    """Execute ``verb_callable`` against every ``ResolvedTarget``.

    Returns the FR-8a aggregate exit code. Emits one FR-9a JSONL
    envelope per target to stdout (in resolved-target-list order
    regardless of completion order).

    Cross-family targets (``family_match=False``) get a synthesized
    exit-5 envelope without ``verb_callable`` being invoked (FR-5).

    Parallelism is capped at ``concurrency`` (default
    :data:`DEFAULT_CONCURRENCY` = 3). Per-target callable failures
    that raise (rather than returning a ``FanOutResult``) are caught
    and converted to an internal device-error envelope so the rest of
    the group still runs (FR-8: a single failure does NOT abort).
    """
    cap = concurrency if concurrency is not None else DEFAULT_CONCURRENCY
    cap = max(1, cap)
    n = len(targets)

    # results indexed by position so we can re-emit in resolved order
    results: list[FanOutResult | None] = [None] * n

    def _run_one(idx: int, rt: ResolvedTarget) -> None:
        if not rt.family_match:
            results[idx] = _synthesize_cross_family_result(rt)
            return
        try:
            results[idx] = verb_callable(rt)
        except Exception as exc:  # noqa: BLE001 - convert any callable failure
            # Defensive: callables SHOULD return FanOutResult on every
            # path, but if one raises we don't want it to abort the
            # group. Wrap as a device error and keep going.
            results[idx] = FanOutResult(
                target=rt.name,
                exit_code=1,
                error={
                    "code": "device_error",
                    "message": f"verb raised {type(exc).__name__}: {exc}",
                },
            )

    if cap == 1 or n <= 1:
        for idx, rt in enumerate(targets):
            _run_one(idx, rt)
    else:
        with ThreadPoolExecutor(max_workers=cap) as pool:
            futures = [pool.submit(_run_one, idx, rt) for idx, rt in enumerate(targets)]
            for fut in futures:
                fut.result()

    # Materialize the per-target results in order.
    final: list[FanOutResult] = []
    for r in results:
        # Every slot filled by _run_one; the None cast is safety.
        assert r is not None  # noqa: S101 - logic invariant
        final.append(r)

    _emit_envelopes([_to_envelope(r) for r in final], output_mode)
    return _aggregate_exit_code(final)


__all__ = ["DEFAULT_CONCURRENCY", "FanOutResult", "fan_out_verb"]


# Silence unused-import warning when click.echo is the only click usage.
_ = sys

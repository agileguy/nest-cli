"""``nest-cli batch`` verb (Phase 4 — FR-9, FR-9a, FR-10, FR-10a..c).

Reads newline-delimited commands from a file (``--file <path>``) or
stdin (``--stdin``) and dispatches each as a fresh ``nest-cli`` verb
invocation, emitting one FR-9a envelope per executed command on stdout.

Each invocation is dispatched through Click's in-process ``CliRunner``
so SystemExit is captured per-line and the rest of the batch keeps
running. The dispatcher injects ``--jsonl`` into the parsed argv (if no
output flag is already present) so the inner verb's stdout is parseable
JSON suitable for the ``result`` field.

Exit code semantics (FR-10a, mirrors FR-8a):

- 0 if every line succeeded.
- 7 if at least one OK + at least one failed.
- All-failed → exit code of the *first* line in the input order.

SIGINT/SIGTERM during batch (FR-10c):

1. Cease dispatching new sub-ops.
2. Wait up to 2 seconds for in-flight to complete and emit results.
3. Emit a final ``{"event":"interrupted","completed":N,"pending":M}``
   line on stdout.
4. Exit 130 (SIGINT) or 143 (SIGTERM).
"""

from __future__ import annotations

import json
import shlex
import signal
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import click
from click.testing import CliRunner

from nest_cli.cli._shared import exit_on_structured_error
from nest_cli.errors import (
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_USAGE_ERROR,
    StructuredError,
    error_enum_for_code,
)

# FR-10c: post-signal grace window for in-flight sub-ops.
_INTERRUPT_GRACE_SECONDS = 2.0

# Output flags that, when present in the parsed line, take precedence
# over the dispatcher's ``--jsonl`` injection. (We never want to mutate
# an operator's explicit choice.)
_OUTPUT_FLAGS = frozenset({"--json", "--jsonl", "--quiet", "--output", "-o"})


@click.command("batch")
@click.option(
    "--file",
    "file_path",
    type=str,
    default=None,
    help="Read commands from a file (mutually exclusive with --stdin).",
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help="Read commands from stdin (mutually exclusive with --file).",
)
def batch_cmd(file_path: str | None, from_stdin: bool) -> None:
    """Run a stream of nest-cli commands and emit one JSONL result per line.

    Implements FR-9 / FR-9a / FR-10 / FR-10a / FR-10b / FR-10c.

    Each input line is parsed with ``shlex.split`` and dispatched via
    Click's in-process ``CliRunner`` so each sub-op gets its own
    captured stdout/stderr. Blank lines and lines starting with ``#``
    are skipped. The output is one FR-9a envelope per executed line
    (in input order), and the exit code follows FR-10a (mirrors FR-8a).

    Group-target fan-out happens *inside* each invoked verb — `batch`
    treats every line as a single sub-op, regardless of whether the
    line targets a group or a single alias. The verb's own fan-out (if
    wired) emits its own per-target JSONL on its captured stdout, which
    becomes the ``result`` field of the batch envelope (a structured
    list rather than a single object).
    """
    # Mutual-exclusion gate.
    if from_stdin and file_path is not None:
        exit_on_structured_error(
            StructuredError(
                code=EXIT_USAGE_ERROR,
                message="--file and --stdin are mutually exclusive",
                hint="Pass exactly one of --file <path> or --stdin.",
            ),
            output_mode="text",
        )
    if not from_stdin and file_path is None:
        exit_on_structured_error(
            StructuredError(
                code=EXIT_USAGE_ERROR,
                message="batch requires either --file <path> or --stdin",
                hint=(
                    "Pass --file <path> to read commands from a file, or --stdin to pipe them in."
                ),
            ),
            output_mode="text",
        )

    # Source the command stream.
    if from_stdin:
        source_lines = sys.stdin.read().splitlines()
    else:
        assert file_path is not None  # noqa: S101 - mutex above guarantees this
        try:
            source_lines = Path(file_path).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            exit_on_structured_error(
                StructuredError(
                    code=EXIT_USAGE_ERROR,
                    message=f"could not read batch file {file_path}: {exc}",
                    hint="Check the path exists and is readable.",
                ),
                output_mode="text",
            )

    # FR-10b: empty input exits 0 with no stdout.
    parsed_lines = list(_filter_input_lines(source_lines))
    if not parsed_lines:
        sys.exit(EXIT_OK)

    # FR-10c: install SIGINT/SIGTERM handlers that set a flag. We don't
    # raise from the handler — that would bypass the summary-line
    # emission below. Save+restore previous handlers so a test process
    # invoking batch in-process doesn't leak handlers into the next
    # test.
    interrupted: dict[str, int | None] = {"signal": None}

    def _handle_signal(signum: int, _frame: Any) -> None:
        interrupted["signal"] = signum

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    results: list[dict[str, Any]] = []
    completed = 0
    pending_at_interrupt = 0
    interrupt_deadline: float | None = None

    try:
        for idx, raw_line in enumerate(parsed_lines):
            # Honor the FR-10c grace window: once interrupted, finish
            # any in-flight call (we're synchronous so there's at most
            # one in-flight at a time — the one we're about to dispatch
            # is dropped) and emit the summary.
            if interrupted["signal"] is not None:
                pending_at_interrupt = len(parsed_lines) - idx
                break

            envelope = _dispatch_line(raw_line)
            results.append(envelope)
            completed += 1
            click.echo(json.dumps(envelope, sort_keys=True))

            # Defensive: if a signal arrived during the dispatch, the
            # next loop-top check catches it. The "wait up to 2 seconds"
            # behavior bounds how long we stay in the loop draining
            # results AFTER an interrupt — effectively zero in this
            # synchronous implementation, but the deadline guards
            # against any future async expansion.
            if interrupted["signal"] is not None and interrupt_deadline is None:
                interrupt_deadline = time.monotonic() + _INTERRUPT_GRACE_SECONDS

        # FR-10c: if interrupted, emit the summary line.
        if interrupted["signal"] is not None:
            summary = {
                "event": "interrupted",
                "completed": completed,
                "pending": pending_at_interrupt,
            }
            click.echo(json.dumps(summary, sort_keys=True))
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)

    # FR-10c exit codes for signals; otherwise FR-10a aggregate.
    if interrupted["signal"] == signal.SIGTERM:
        sys.exit(EXIT_SIGTERM)
    if interrupted["signal"] == signal.SIGINT:
        sys.exit(EXIT_SIGINT)
    sys.exit(_aggregate_exit_code(results))


def _filter_input_lines(lines: Iterable[str]) -> Iterable[str]:
    """Yield non-blank, non-comment lines (FR-10b)."""
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        yield stripped


def _dispatch_line(raw_line: str) -> dict[str, Any]:
    """Parse and execute one batch line; return the FR-9a envelope.

    The dispatcher:

    1. Parses ``raw_line`` with ``shlex.split`` so quoted args survive.
    2. Injects ``--jsonl`` into the parsed argv if no output-format
       flag is already present, so the inner verb's stdout is parseable
       and stuffable into the ``result`` field.
    3. Invokes the root ``cli`` group via ``CliRunner.invoke`` which
       traps ``SystemExit`` and surfaces ``result.exit_code``.
    4. Builds the FR-9a envelope from the captured exit code + stdout.
    """
    # Lazy import to avoid a circular import — batch_cmd is registered
    # at the same layer as the cli root group.
    from nest_cli.cli import cli as cli_root

    try:
        parsed_argv = shlex.split(raw_line)
    except ValueError as exc:
        # Unbalanced quotes; treat as a per-line usage error.
        return {
            "command": raw_line,
            "status": "error",
            "exit_code": EXIT_USAGE_ERROR,
            "error": {
                "code": "usage_error",
                "message": f"could not parse batch line: {exc}",
                "hint": "Check for unbalanced quotes in the input line.",
            },
        }

    if not parsed_argv:
        # Defensive — shouldn't happen because _filter_input_lines
        # already strips blanks, but guard anyway.
        return {
            "command": raw_line,
            "status": "error",
            "exit_code": EXIT_USAGE_ERROR,
            "error": {
                "code": "usage_error",
                "message": "empty batch line after shlex parse",
            },
        }

    # Inject --jsonl if no output flag is set on the line.
    has_output_flag = any(
        token in _OUTPUT_FLAGS or token.startswith("--output=") for token in parsed_argv
    )
    invoke_argv = list(parsed_argv) if has_output_flag else [*parsed_argv, "--jsonl"]

    runner = CliRunner()
    result = runner.invoke(cli_root, invoke_argv, catch_exceptions=True)
    exit_code = result.exit_code

    envelope: dict[str, Any] = {
        "command": raw_line,
        "exit_code": exit_code,
    }
    if exit_code == 0:
        envelope["status"] = "ok"
        envelope["result"] = _parse_inner_stdout(result.output)
    else:
        envelope["status"] = "error"
        envelope["error"] = _parse_inner_error(result.stderr, exit_code)

    return envelope


def _parse_inner_stdout(output: str) -> Any:
    """Parse the inner verb's stdout into the FR-9a ``result`` field.

    The inner invocation always runs with ``--jsonl`` (or operator's
    explicit choice). Single-record verbs emit one JSON object on
    stdout; multi-record verbs emit one object per line. We try to
    parse as a single object first; on failure, parse line-by-line and
    return a list.
    """
    text = output.strip()
    if not text:
        return None
    # Try single-object first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try line-by-line.
    parsed: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            # Inner verb's stdout was not parseable JSON. Surface as a
            # raw-text fallback rather than corrupting the batch
            # envelope.
            return {"raw_stdout": output}
    return parsed if parsed else None


def _parse_inner_error(stderr: str, exit_code: int) -> dict[str, Any]:
    """Build an error sub-object from the inner verb's stderr.

    Ideal: the inner verb emitted a §11.3 structured error JSON object
    on stderr; we pluck ``code`` / ``message`` / ``hint`` from it. If
    parsing fails, fall back to a minimal envelope keyed off the exit
    code's enum mapping.
    """
    text = stderr.strip()
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            err: dict[str, Any] = {
                "code": str(parsed.get("error", error_enum_for_code(exit_code))),
                "message": str(parsed.get("message", "")),
            }
            hint = parsed.get("hint")
            if hint:
                err["hint"] = str(hint)
            return err
    return {
        "code": error_enum_for_code(exit_code),
        "message": text or f"sub-op failed with exit code {exit_code}",
    }


def _aggregate_exit_code(results: list[dict[str, Any]]) -> int:
    """FR-10a (mirrors FR-8a) aggregate exit-code arithmetic."""
    if not results:
        return EXIT_OK
    ok_count = sum(1 for r in results if r["exit_code"] == EXIT_OK)
    if ok_count == len(results):
        return EXIT_OK
    if ok_count == 0:
        # All-failed → first line's exit code (input order).
        first_failed = next(r for r in results if r["exit_code"] != EXIT_OK)
        return int(first_failed["exit_code"])
    return EXIT_PARTIAL_FAILURE


__all__ = ["batch_cmd"]

"""Shared output formatters for nest-cli (SRD §5.8 / FR-11..15).

The CLI supports four output modes:

- ``text`` — human-readable, one line per record (default on a tty).
- ``json`` — pretty JSON (single object or single array).
- ``jsonl`` — newline-delimited JSON (one object per line).
- ``quiet`` — no stdout output; exit code is the only signal.

The ``add_output_options`` decorator stacks four Click flags — ``--json``,
``--jsonl``, ``--quiet``, ``--output text|json|jsonl|quiet`` — onto the
target command and resolves them into a single ``output_mode`` keyword
argument passed to the command body. Any combination of flags that names
two different modes (e.g. ``--json --jsonl``) exits 64 with a structured
error per SRD §5.10 / §11.3.

The ``emit`` function is the dispatch point: command bodies build a
result (a Pydantic model, a dict, a list of either) and call
``emit(result, output_mode)``. Pydantic models serialize via
``model_dump()`` so the JSON payload is the canonical SRD §10.x record.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar, cast

import click
from pydantic import BaseModel

from nest_cli.errors import (
    EXIT_USAGE_ERROR,
    StructuredError,
    emit_structured_error_to_stderr,
)

OutputMode = Literal["text", "json", "jsonl", "quiet"]

_VALID_MODES: tuple[OutputMode, ...] = ("text", "json", "jsonl", "quiet")

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Click decorator factory
# ---------------------------------------------------------------------------


def add_output_options(cmd: F) -> F:
    """Stack ``--json``, ``--jsonl``, ``--quiet``, ``--output`` onto ``cmd``.

    The four flags resolve to a single ``output_mode: OutputMode`` keyword
    argument passed to the command body. The resolution rules are:

    - ``--output <mode>`` is the canonical knob. Default: ``text``.
    - ``--json`` / ``--jsonl`` / ``--quiet`` are convenience aliases that
      are mutually exclusive with each other AND with ``--output`` when
      ``--output`` names a different mode.
    - Two convenience flags together (e.g. ``--json --jsonl``) exit 64.
    - One convenience flag plus ``--output`` naming a different mode
      exits 64.
    - One convenience flag plus ``--output`` naming the same mode is fine.
    """

    @click.option(
        "--output",
        "output_explicit",
        type=click.Choice(_VALID_MODES),
        default=None,
        help="Output format: text, json, jsonl, or quiet.",
    )
    @click.option(
        "--json",
        "json_flag",
        is_flag=True,
        default=False,
        help="Force pretty-JSON output. Mutually exclusive with --jsonl/--quiet/--output.",
    )
    @click.option(
        "--jsonl",
        "jsonl_flag",
        is_flag=True,
        default=False,
        help="Force newline-delimited JSON. Mutually exclusive with --json/--quiet/--output.",
    )
    @click.option(
        "--quiet",
        "quiet_flag",
        is_flag=True,
        default=False,
        help="Suppress stdout; exit code is the only signal. "
        "Mutually exclusive with --json/--jsonl/--output.",
    )
    def wrapper(
        *args: Any,
        output_explicit: str | None,
        json_flag: bool,
        jsonl_flag: bool,
        quiet_flag: bool,
        **kwargs: Any,
    ) -> Any:
        try:
            output_mode = _resolve_output_mode(
                output_explicit=output_explicit,
                json_flag=json_flag,
                jsonl_flag=jsonl_flag,
                quiet_flag=quiet_flag,
            )
        except StructuredError as exc:
            # Resolve before any command-side I/O. Default to text-mode
            # error rendering so the operator sees the conflict on a tty.
            emit_structured_error_to_stderr(exc, output_mode="text")
            sys.exit(exc.code)
        return cmd(*args, output_mode=output_mode, **kwargs)

    # Click introspects ``__wrapped__`` for help text; preserve the chain.
    wrapper.__wrapped__ = cmd  # type: ignore[attr-defined]
    wrapper.__doc__ = cmd.__doc__
    wrapper.__name__ = cmd.__name__
    return cast(F, wrapper)


def _resolve_output_mode(
    *,
    output_explicit: str | None,
    json_flag: bool,
    jsonl_flag: bool,
    quiet_flag: bool,
) -> OutputMode:
    """Collapse the four-flag input to a single ``OutputMode``.

    Raises ``StructuredError(code=EXIT_USAGE_ERROR)`` if the flags name
    two different modes.
    """
    candidates: list[OutputMode] = []
    if json_flag:
        candidates.append("json")
    if jsonl_flag:
        candidates.append("jsonl")
    if quiet_flag:
        candidates.append("quiet")
    if output_explicit is not None:
        candidates.append(cast(OutputMode, output_explicit))

    if not candidates:
        return "text"

    distinct = set(candidates)
    if len(distinct) > 1:
        raise StructuredError(
            code=EXIT_USAGE_ERROR,
            message=(
                "Output flags conflict: " + ", ".join(sorted(distinct)) + " name different modes."
            ),
            hint=(
                "Pass at most one of --json, --jsonl, --quiet, or --output. "
                "Combining flags that name the same mode is allowed; "
                "combining different modes is not."
            ),
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# emit()
# ---------------------------------------------------------------------------


def _format_rfc3339_z(dt: datetime) -> str:
    """Render ``dt`` as RFC 3339 UTC with the literal ``Z`` suffix (FR-22).

    Pydantic v2's default JSON datetime serializer emits ``+00:00``; this
    helper canonicalizes to ``Z`` so every nest-cli stdout payload that
    carries a datetime (whether through a Pydantic model serializer or a
    raw dict passed to ``emit``) stays on the same wire format.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _pydantic_default(obj: Any) -> Any:
    """JSON encoder fallback for Pydantic models, datetimes, and paths."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime):
        return _format_rfc3339_z(obj)
    return str(obj)


def _to_jsonable(value: Any) -> Any:
    """Recursively normalize Pydantic models into plain JSON-able structures."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return _format_rfc3339_z(value)
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def emit(result: Any, output_mode: OutputMode) -> None:
    """Render ``result`` to stdout per ``output_mode``.

    Behavior matrix:

    - ``quiet`` — no output.
    - ``json`` — pretty JSON (indent=2, sort_keys=True for determinism per
      FR-25). Pydantic models are dumped via ``model_dump(mode="json")``.
    - ``jsonl`` — one JSON object per line. If ``result`` is a list, each
      element gets its own line; otherwise a single line.
    - ``text`` — best-effort human-readable rendering. Lists become one
      entry per line; dicts become ``key: value`` lines; Pydantic models
      become a multi-line block keyed by attribute name.
    """
    if output_mode == "quiet":
        return

    if output_mode == "json":
        click.echo(
            json.dumps(
                _to_jsonable(result),
                indent=2,
                sort_keys=True,
                default=_pydantic_default,
            )
        )
        return

    if output_mode == "jsonl":
        if isinstance(result, list):
            for item in result:
                click.echo(
                    json.dumps(
                        _to_jsonable(item),
                        sort_keys=True,
                        default=_pydantic_default,
                    )
                )
        else:
            click.echo(
                json.dumps(
                    _to_jsonable(result),
                    sort_keys=True,
                    default=_pydantic_default,
                )
            )
        return

    # text mode
    _emit_text(result)


def _emit_text(result: Any) -> None:
    """Render ``result`` as human-readable text.

    Lists become one line per item; dicts and Pydantic models become a
    block of ``key: value`` lines separated by a blank line between
    records. None becomes empty output (no stdout). Strings are echoed
    verbatim.
    """
    if result is None:
        return
    if isinstance(result, str):
        click.echo(result)
        return
    if isinstance(result, BaseModel):
        _emit_text_record(result.model_dump(mode="json"))
        return
    if isinstance(result, dict):
        _emit_text_record(result)
        return
    if isinstance(result, list):
        if not result:
            return
        for i, item in enumerate(result):
            if i:
                click.echo("")
            if isinstance(item, BaseModel):
                _emit_text_record(item.model_dump(mode="json"))
            elif isinstance(item, dict):
                _emit_text_record(item)
            else:
                click.echo(str(item))
        return
    click.echo(str(result))


def _emit_text_record(record: dict[str, Any]) -> None:
    """Emit a single dict as ``key: value`` lines.

    Bool values render lowercase (``true`` / ``false``) so text-mode output
    is consistent with the JSON-mode rendering and matches the SRD-style
    boolean serialization. Datetime values render as RFC 3339 UTC ``Z``
    per FR-22.
    """
    for key, value in record.items():
        if isinstance(value, bool):
            click.echo(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, datetime):
            click.echo(f"{key}: {_format_rfc3339_z(value)}")
        elif isinstance(value, dict | list):
            click.echo(f"{key}: {json.dumps(_to_jsonable(value), default=_pydantic_default)}")
        else:
            click.echo(f"{key}: {value}")

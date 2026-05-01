"""Exit codes and structured-error contract (SRD §11.1, §11.2).

This module is the single source of truth for nest-cli's exit codes and the
on-the-wire shape of structured errors emitted to stderr.

Exit codes
----------

The constants below mirror the SRD §11.1 table exactly. Other modules
SHOULD import these constants rather than redefining the integer values:

    from nest_cli.errors import EXIT_AUTH_ERROR

The constants are also reachable on a ``StructuredError`` instance via
``err.code``, so command bodies typically do::

    raise StructuredError(
        code=EXIT_NOT_FOUND,
        message=f"alias not found: {alias!r}",
        hint="Run `nest-cli list` to see configured aliases.",
    )

and the top-level CLI catches and emits via
``emit_structured_error_to_stderr`` then ``sys.exit(err.code)``.

Wire format
-----------

The stderr JSON envelope is locked by SRD §11.2:

    {"error": "<enum>", "exit_code": <int>, "message": "<str>",
     "hint": "<str|optional>", "details": {<obj|optional>}}

The ``error`` field is the closed enum from §11.2:
``device_error``, ``auth_failed``, ``network_error``, ``not_found``,
``unsupported_feature``, ``config_error``, ``partial_failure``,
``usage_error``, ``interrupted``. ``exit_code`` is the integer mirror.
Tooling MAY pattern-match either field; both are guaranteed to be present
and consistent.

Text-mode emission is a single human-readable line on stderr — the same
information, no JSON structure. Programmatic callers SHOULD pass
``--json`` to get the parseable envelope.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Exit codes (SRD §11.1)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_DEVICE_ERROR = 1
EXIT_AUTH_ERROR = 2
EXIT_NETWORK_ERROR = 3
EXIT_NOT_FOUND = 4
EXIT_UNSUPPORTED_FEATURE = 5
EXIT_CONFIG_ERROR = 6
EXIT_PARTIAL_FAILURE = 7
EXIT_USAGE_ERROR = 64
EXIT_SIGINT = 130
EXIT_SIGTERM = 143


# ---------------------------------------------------------------------------
# Error enum mapping (SRD §11.2)
# ---------------------------------------------------------------------------

# Closed mapping of exit-code → wire-format error enum. Tooling MAY pattern
# match on the string enum; both representations are guaranteed-consistent.
_EXIT_CODE_TO_ENUM: dict[int, str] = {
    EXIT_DEVICE_ERROR: "device_error",
    EXIT_AUTH_ERROR: "auth_failed",
    EXIT_NETWORK_ERROR: "network_error",
    EXIT_NOT_FOUND: "not_found",
    EXIT_UNSUPPORTED_FEATURE: "unsupported_feature",
    EXIT_CONFIG_ERROR: "config_error",
    EXIT_PARTIAL_FAILURE: "partial_failure",
    EXIT_USAGE_ERROR: "usage_error",
    EXIT_SIGINT: "interrupted",
    EXIT_SIGTERM: "interrupted",
}


def error_enum_for_code(code: int) -> str:
    """Return the SRD §11.2 ``error`` enum string for an exit code.

    Falls back to ``"device_error"`` for codes outside the closed mapping
    (defensive — should not happen if callers use the EXIT_* constants).
    """
    return _EXIT_CODE_TO_ENUM.get(code, "device_error")


# ---------------------------------------------------------------------------
# Structured error (raised by command bodies, caught by the CLI top level)
# ---------------------------------------------------------------------------


@dataclass
class StructuredError(Exception):
    """A CLI failure with an explicit exit code and operator-facing payload.

    Mirrors the wire envelope in SRD §11.2. The ``code`` field is the
    integer exit code (one of ``EXIT_*`` above). The ``message`` is the
    short human-readable summary. The ``hint`` is an optional actionable
    next step. ``details`` is an optional structured payload — used for
    things like ``{"target": "front-door", "credential": "oauth_refresh_token"}``
    which are surfaced in JSON output but not in text-mode stderr.
    """

    code: int
    message: str
    hint: str | None = None
    details: dict[str, Any] | None = field(default=None)

    def __str__(self) -> str:  # noqa: D401 - stdlib Exception protocol
        return self.message


# ---------------------------------------------------------------------------
# Stderr emission (dual-contract: JSON for programmatic, text for humans)
# ---------------------------------------------------------------------------


def emit_structured_error_to_stderr(err: StructuredError, output_mode: str) -> None:
    """Write ``err`` to stderr in a format chosen by ``output_mode``.

    Output-mode contract (SRD §5.8 + §11.2):

    - ``"json"`` / ``"jsonl"`` / ``"quiet"`` — emit a single JSON object on
      stderr with the SRD §11.2 envelope.
    - ``"text"`` (default tty) — emit a human-readable single-line summary
      on stderr. Programmatic callers should pass ``--json`` to get the
      parseable envelope.

    Note ``--quiet`` only suppresses *stdout* per FR-14; structured errors
    still go to stderr (otherwise the operator gets exit-5 with no
    diagnostic information at all).
    """
    if output_mode == "text":
        line = f"error: {err.message}"
        if err.hint:
            line += f"\nhint: {err.hint}"
        print(line, file=sys.stderr)
        return

    payload: dict[str, Any] = {
        "error": error_enum_for_code(err.code),
        "exit_code": err.code,
        "message": err.message,
    }
    if err.hint:
        payload["hint"] = err.hint
    if err.details:
        payload["details"] = err.details
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)

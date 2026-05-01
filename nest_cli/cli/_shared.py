"""Shared helpers used by multiple ``nest_cli.cli.*_cmd`` modules.

Each helper here exists to dedupe a pattern that appears in 2+ verb
modules. Keeping these in a separate module (rather than ``list_cmd``)
avoids the verb-imports-another-verb tangle that previously needed
deferred imports.

Public surface is private-by-convention (leading underscore) — these
are CLI-internals, not intended for external import.
"""

from __future__ import annotations

import sys

from nest_cli.auth.credentials import (
    CredentialError,
    default_credentials_path,
    load_credentials,
    refresh_access_token_if_needed,
)
from nest_cli.auth.types import CamCredentials
from nest_cli.errors import (
    StructuredError,
    emit_structured_error_to_stderr,
)
from nest_cli.output import OutputMode


def load_credentials_or_exit(output_mode: OutputMode) -> CamCredentials:
    """Load + auto-refresh the cam credentials, or exit with a structured error.

    Wraps the load → refresh sequence and the ``CredentialError`` →
    ``StructuredError`` conversion that every operational verb needs to
    do. Failure paths emit a structured error to stderr and ``sys.exit``
    with the SRD-mapped code (2 for auth, 6 for config, 3 for network on
    refresh timeout).
    """
    creds_path = default_credentials_path()
    try:
        creds = load_credentials(creds_path)
        return refresh_access_token_if_needed(creds, creds_path)
    except CredentialError as exc:
        err = StructuredError(
            code=exc.exit_code,
            message=str(exc),
            hint=exc.hint,
        )
        emit_structured_error_to_stderr(err, output_mode)
        sys.exit(err.code)


def exit_on_structured_error(exc: StructuredError, output_mode: OutputMode) -> None:
    """Emit a structured error to stderr and exit with the mapped code.

    The verb modules call this in ``except StructuredError`` handlers to
    keep the body of each command short. ``sys.exit`` raises
    ``SystemExit`` so the caller does not need an explicit ``return``.
    """
    emit_structured_error_to_stderr(exc, output_mode)
    sys.exit(exc.code)


def family_for_target(target: str) -> str:
    """Classify a target string as ``cam`` or ``wifi``.

    A target starting with ``wifi:`` is wifi; everything else is cam
    (the SDM ``enterprises/...`` path is the dominant cam form).
    """
    return "wifi" if target.startswith("wifi:") else "cam"


def filter_aliases_by_family(aliases: dict[str, str], family: str | None) -> dict[str, str]:
    """Return the subset of ``aliases`` whose targets match ``family``.

    ``None`` means no filter — returns a copy of the input dict.
    """
    if family is None:
        return dict(aliases)
    return {name: target for name, target in aliases.items() if family_for_target(target) == family}

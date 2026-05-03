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
from typing import NoReturn

from nest_cli.auth.credentials import (
    CredentialError,
    default_credentials_path,
    load_credentials,
    refresh_access_token_if_needed,
)
from nest_cli.auth.types import CamCredentials
from nest_cli.errors import (
    EXIT_USAGE_ERROR,
    StructuredError,
    emit_structured_error_to_stderr,
)
from nest_cli.output import OutputMode

# Hint pointing operators at the SRD section that explains the wifi
# experimental-flag posture. Used by every wifi verb (auth + list) so
# operators see consistent guidance regardless of which verb tripped
# the FR-WIFI-0 gate.
EXPERIMENTAL_WIFI_HINT = (
    "Pass --experimental-wifi to acknowledge SRD §3.2.3 — the wifi side "
    "wraps single-maintainer reverse-engineered libraries that break when "
    "Google rotates Foyer endpoints. The flag's friction is the feature."
)


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


def exit_on_structured_error(exc: StructuredError, output_mode: OutputMode) -> NoReturn:
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


def experimental_wifi_gate_or_exit(
    experimental_wifi: bool, output_mode: OutputMode, *, verb: str
) -> None:
    """Exit 64 with FR-WIFI-0 hint unless ``--experimental-wifi`` was passed.

    SRD §11.2 also names exit 5 for this case, but FR-WIFI-0 is the
    more specific requirement (says exit 64). We follow FR-WIFI-0;
    ARCHITECTURE.md notes the §11.2 vs FR-WIFI-0 resolution.

    ``verb`` is the operator-facing verb name (e.g. ``"wifi-setup"``,
    ``"list groups"``) used in the error message body.
    """
    if experimental_wifi:
        return
    exit_on_structured_error(
        StructuredError(
            code=EXIT_USAGE_ERROR,
            message=(
                f"`{verb}` requires --experimental-wifi (FR-WIFI-0). "
                "The wifi side wraps reverse-engineered single-maintainer "
                "libraries that break when Google rotates Foyer endpoints; "
                "every invocation must explicitly opt in."
            ),
            hint=EXPERIMENTAL_WIFI_HINT,
            family="wifi",
        ),
        output_mode,
    )

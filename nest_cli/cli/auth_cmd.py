"""Click subgroup ``auth`` — OAuth credentials management for the cam side.

Verbs (per SRD §5.5.1):

- ``auth setup`` — interactive OAuth Desktop flow (FR-CRED-1, FR-CRED-2).
- ``auth refresh`` — force-rotate the access token (FR-CRED-4).
- ``auth revoke`` — invalidate at Google + scrub local file (FR-CRED-5).
- ``auth status`` — read credentials and emit a redacted summary
  (FR-CRED-10 cam-only subset).

Public surface
--------------

``auth_group`` is a ``click.Group`` named ``auth``. The root CLI module
imports it directly::

    from nest_cli.cli.auth_cmd import auth_group
    main.add_command(auth_group)

This package does NOT re-export ``auth_group`` from ``nest_cli.auth`` to
avoid a circular import (``nest_cli.auth`` → ``nest_cli.cli.auth_cmd`` →
``nest_cli.auth``).

Output mode
-----------

Every verb stacks the standard ``add_output_options`` decorator from
``nest_cli.output``, so ``--json``, ``--jsonl``, ``--quiet``, and
``--output text|json|jsonl|quiet`` are all honored uniformly across the
CLI (FR-11..15). Failure paths are emitted via the SRD §11.3 envelope
through ``exit_on_structured_error``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import click

from nest_cli.auth.credentials import (
    CredentialError,
    default_credentials_path,
    load_credentials,
    refresh_access_token_if_needed,
    revoke_refresh_token,
    save_credentials,
)
from nest_cli.auth.oauth import run_oauth_flow
from nest_cli.cli._shared import exit_on_structured_error
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_USAGE_ERROR,
    StructuredError,
)
from nest_cli.output import OutputMode, add_output_options, emit


def _redact_client_id(client_id: str) -> str:
    """Redact all but the trailing 8 characters (auth status output only)."""
    if len(client_id) <= 8:
        return "***"
    return "***" + client_id[-8:]


def _credential_error_to_structured(exc: CredentialError) -> StructuredError:
    """Convert a CredentialError into the SRD §11.3 StructuredError envelope.

    The ``family`` discriminator does not belong in the §11.3 error
    envelope (the closed enum + exit_code carry the disambiguation). It
    surfaces in the ``auth status`` payload instead.
    """
    return StructuredError(
        code=exc.exit_code,
        message=str(exc),
        hint=exc.hint,
    )


# ---------------------------------------------------------------------------
# Click group + verbs
# ---------------------------------------------------------------------------


auth_group = click.Group(
    name="auth",
    help="OAuth credentials management for Nest cameras (SDM API).",
)


@auth_group.command("setup")
@click.option(
    "--callback-port",
    type=click.IntRange(min=1, max=65535),
    default=8765,
    show_default=True,
    help="Port for the local OAuth callback listener.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite an existing credentials file. Required if one already exists.",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help="Print the consent URL on stderr instead of trying to open a browser.",
)
@add_output_options
def cmd_setup(
    callback_port: int,
    overwrite: bool,
    no_browser: bool,
    output_mode: OutputMode,
) -> None:
    """Run the interactive OAuth Desktop flow and persist credentials.

    Implements FR-CRED-1 and FR-CRED-2. Prerequisites (out of band, per
    SRD §6.2): operator has created a Google Cloud project, enabled the
    Smart Device Management API, registered an application in the Device
    Access Console (paying $5 USD), and downloaded a Desktop OAuth client
    JSON from console.cloud.google.com/apis/credentials. The CLI prompts
    for the three identifiers, then runs the consent flow.
    """
    creds_path = default_credentials_path()
    if creds_path.exists() and not overwrite:
        # FR-CRED-2: refuse to clobber.
        exit_on_structured_error(
            StructuredError(
                code=EXIT_AUTH_ERROR,
                message=f"credentials already exist at {creds_path}",
                hint=(
                    "Pass --overwrite to replace them, or run `nest-cli auth revoke` "
                    "first to revoke at Google's end."
                ),
            ),
            output_mode,
        )

    # Interactive prompts. ``hide_input`` masks the secret; ``confirmation_prompt``
    # is off because the operator pasted from the console JSON and a typo would
    # be caught by Google immediately.
    project_id = click.prompt("Google Cloud project id", type=str)
    client_id = click.prompt("OAuth client id", type=str)
    client_secret = click.prompt("OAuth client secret", type=str, hide_input=True)

    click.echo(
        f"Starting local OAuth listener on 127.0.0.1:{callback_port}...",
        err=True,
    )
    try:
        creds = run_oauth_flow(
            client_id=client_id,
            client_secret=client_secret,
            project_id=project_id,
            callback_port=callback_port,
            open_browser=not no_browser,
        )
        save_credentials(creds_path, creds)
    except CredentialError as exc:
        exit_on_structured_error(_credential_error_to_structured(exc), output_mode)

    payload = {
        "status": "ok",
        "credentials_path": str(creds_path),
        "project_id": creds.google_cloud_project_id,
        "expires_at": creds.expires_at,
    }
    emit(payload, output_mode)


@auth_group.command("refresh")
@add_output_options
def cmd_refresh(output_mode: OutputMode) -> None:
    """Force-refresh the access token using the stored refresh token.

    Implements FR-CRED-4. Useful for testing, debugging, and recovering
    from a clock-skew-driven near-expiry condition without waiting for
    the next operational verb to fire the auto-refresh path.
    """
    creds_path = default_credentials_path()
    try:
        creds = load_credentials(creds_path)
        rotated = refresh_access_token_if_needed(creds, creds_path, force=True)
    except CredentialError as exc:
        exit_on_structured_error(_credential_error_to_structured(exc), output_mode)

    payload = {
        "status": "ok",
        "expires_at": rotated.expires_at,
    }
    emit(payload, output_mode)


@auth_group.command("revoke")
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt (required in non-tty contexts).",
)
@add_output_options
def cmd_revoke(yes: bool, output_mode: OutputMode) -> None:
    """Revoke the cam OAuth refresh token at Google and scrub the local file.

    Implements FR-CRED-5. After this verb succeeds, all ``cam`` verbs exit
    2 until ``auth setup`` re-runs.

    Local-file scrub strategy: atomic-rename an empty JSON object ``{}``
    over the credentials file. The file path remains, so ``auth status``
    can read it and report ``configured=false`` cleanly. (Deleting outright
    would leave ``status`` with a "missing file" branch that conflates
    "never set up" with "explicitly revoked" — the empty-stub stays
    distinguishable in v0.2 if we ever want to.)
    """
    creds_path = default_credentials_path()

    if not yes:
        # ``click.confirm`` reads from the configured input stream, which
        # CliRunner controls in tests. In production with no stdin attached
        # (e.g. a pipe or background invocation), Click raises
        # ``click.exceptions.Abort`` which we map to exit 64 — the operator
        # should pass ``--yes`` for non-interactive use. Mirrors
        # ``tapo-cli`` ``reboot`` semantics (SRD §11.1, exit 64).
        try:
            confirmed = click.confirm(
                "Revoke the cam OAuth refresh token at Google and scrub the local file?",
                default=False,
            )
        except click.exceptions.Abort:
            exit_on_structured_error(
                StructuredError(
                    code=EXIT_USAGE_ERROR,
                    message="auth revoke requires --yes when no stdin is attached",
                ),
                output_mode,
            )
        if not confirmed:
            click.echo("Aborted.", err=True)
            return

    try:
        creds = load_credentials(creds_path)
        revoke_refresh_token(creds)
    except CredentialError as exc:
        exit_on_structured_error(_credential_error_to_structured(exc), output_mode)

    # Scrub local file: atomic-replace with an empty stub. We bypass
    # ``save_credentials`` here because the stub deliberately fails the
    # CamCredentials schema (no fields).
    _scrub_credentials(creds_path)

    payload = {"status": "revoked", "credentials_path": str(creds_path)}
    emit(payload, output_mode)


def _scrub_credentials(path: Path) -> None:
    """Atomically replace the credentials file with an empty JSON stub.

    Mirrors the ``save_credentials`` write pattern (tempfile + fsync +
    rename + chmod 0600) so the post-revoke state is indistinguishable
    from any other write at the filesystem layer.
    """
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("{}")
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


@auth_group.command("status")
@add_output_options
def cmd_status(output_mode: OutputMode) -> None:
    """Print the cam credentials status with secrets redacted.

    Implements the cam-only subset of FR-CRED-10. Output is a JSON array
    in ``--json`` mode (FR-CRED-10): v0.1.0 emits one element (cam family);
    Phase 3 will add the wifi element to the same array.

    Redaction (FR-CRED-10, §6.7): the refresh token, access token, and
    OAuth client secret are NEVER emitted. ``oauth_client_id`` is
    truncated to its trailing 8 characters.
    """
    creds_path = default_credentials_path()
    if not creds_path.exists():
        emit(
            [
                {
                    "family": "cam",
                    "configured": False,
                    "credentials_path": str(creds_path),
                }
            ],
            output_mode,
        )
        return

    # Detect the post-revoke empty-stub state ahead of full validation so
    # the operator gets a clean "not configured" message rather than a
    # schema-violation crash.
    try:
        raw = creds_path.read_text(encoding="utf-8")
    except OSError as exc:
        exit_on_structured_error(
            StructuredError(
                code=EXIT_AUTH_ERROR,
                message=f"could not read {creds_path}: {exc}",
            ),
            output_mode,
        )

    if raw.strip() in ("{}", ""):
        emit(
            [
                {
                    "family": "cam",
                    "configured": False,
                    "credentials_path": str(creds_path),
                    "note": "credentials previously revoked",
                }
            ],
            output_mode,
        )
        return

    try:
        creds = load_credentials(creds_path)
    except CredentialError as exc:
        exit_on_structured_error(_credential_error_to_structured(exc), output_mode)

    # ``time_until_expiry_seconds`` is computed at emit time so the
    # operator sees a fresh delta even if the file was written long ago.
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    seconds_until_expiry = int((creds.expires_at.astimezone(UTC) - now).total_seconds())

    emit(
        [
            {
                "family": "cam",
                "configured": True,
                "credentials_path": str(creds_path),
                "google_cloud_project_id": creds.google_cloud_project_id,
                "oauth_client_id_redacted": _redact_client_id(creds.oauth_client_id),
                "expires_at": creds.expires_at,
                "time_until_expiry_seconds": seconds_until_expiry,
            }
        ],
        output_mode,
    )

"""Click subgroup ``auth`` — OAuth credentials management for the cam side.

Verbs (per SRD §5.5.1):

- ``auth setup`` — interactive OAuth Desktop flow (FR-CRED-1, FR-CRED-2).
- ``auth refresh`` — force-rotate the access token (FR-CRED-4).
- ``auth revoke`` — invalidate at Google + scrub local file (FR-CRED-5).
- ``auth status`` — read credentials and emit a redacted summary
  (FR-CRED-10 cam-only subset).

Public surface
--------------

``auth_group`` is a ``click.Group`` named ``auth``. Engineer B's root
CLI module imports it directly::

    from nest_cli.cli.auth_cmd import auth_group
    main.add_command(auth_group)

This package does NOT re-export ``auth_group`` from ``nest_cli.auth`` to
avoid a circular import (``nest_cli.auth`` → ``nest_cli.cli.auth_cmd`` →
``nest_cli.auth``).

Output mode
-----------

For v0.1.0 we honor ``--output text|json``. SRD FR-11..15 will eventually
unify this with ``--quiet`` and ``--jsonl`` across the whole CLI;
Engineer B's shared ``output.py`` is the canonical home for that logic.
The ``auth`` verbs use a minimal local helper for now and will rebase to
the shared formatter once it lands.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

# ---------------------------------------------------------------------------
# Output helpers (local for v0.1.0; will rebase to nest_cli.output)
# ---------------------------------------------------------------------------

_OUTPUT_TEXT = "text"
_OUTPUT_JSON = "json"
_OUTPUT_CHOICES = (_OUTPUT_TEXT, _OUTPUT_JSON)


def _emit(payload: dict[str, Any], *, output: str, text_lines: list[str]) -> None:
    """Render ``payload`` per ``--output`` mode.

    Text mode emits the human-readable lines passed in. JSON mode emits the
    raw payload as pretty JSON. The two modes share no string formatting —
    they are independent renderings of the same underlying record (FR-25).
    """
    if output == _OUTPUT_JSON:
        click.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        for line in text_lines:
            click.echo(line)


def _exit_with_credential_error(exc: CredentialError) -> None:
    """Print a structured error to stderr and exit with the SRD-mapped code."""
    error_record = {
        "error": "auth_failed" if exc.exit_code == 2 else "config_error",
        "exit_code": exc.exit_code,
        "family": "cam",
        "message": str(exc),
    }
    if exc.hint:
        error_record["hint"] = exc.hint
    click.echo(json.dumps(error_record), err=True)
    sys.exit(exc.exit_code)


def _redact_client_id(client_id: str) -> str:
    """Redact all but the trailing 8 characters (auth status output only)."""
    if len(client_id) <= 8:
        return "***"
    return "***" + client_id[-8:]


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
@click.option(
    "--output",
    type=click.Choice(_OUTPUT_CHOICES),
    default=_OUTPUT_TEXT,
    show_default=True,
    help="Output format.",
)
def cmd_setup(
    callback_port: int,
    overwrite: bool,
    no_browser: bool,
    output: str,
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
        click.echo(
            json.dumps(
                {
                    "error": "auth_failed",
                    "exit_code": 2,
                    "family": "cam",
                    "message": f"credentials already exist at {creds_path}",
                    "hint": (
                        "Pass --overwrite to replace them, or run `nest-cli auth revoke` "
                        "first to revoke at Google's end."
                    ),
                }
            ),
            err=True,
        )
        sys.exit(2)

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
        _exit_with_credential_error(exc)
        return  # unreachable; for type-checkers

    payload = {
        "status": "ok",
        "credentials_path": str(creds_path),
        "project_id": creds.google_cloud_project_id,
        "expires_at": creds.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    _emit(
        payload,
        output=output,
        text_lines=[
            f"OAuth setup complete. Credentials saved to {creds_path} (chmod 0600).",
            f"Access token expires at {payload['expires_at']}.",
        ],
    )


@auth_group.command("refresh")
@click.option(
    "--output",
    type=click.Choice(_OUTPUT_CHOICES),
    default=_OUTPUT_TEXT,
    show_default=True,
    help="Output format.",
)
def cmd_refresh(output: str) -> None:
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
        _exit_with_credential_error(exc)
        return  # unreachable

    payload = {
        "status": "ok",
        "expires_at": rotated.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    _emit(
        payload,
        output=output,
        text_lines=[f"Access token rotated. New expiry: {payload['expires_at']}"],
    )


@auth_group.command("revoke")
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt (required in non-tty contexts).",
)
@click.option(
    "--output",
    type=click.Choice(_OUTPUT_CHOICES),
    default=_OUTPUT_TEXT,
    show_default=True,
    help="Output format.",
)
def cmd_revoke(yes: bool, output: str) -> None:
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
            click.echo(
                json.dumps(
                    {
                        "error": "usage_error",
                        "exit_code": 64,
                        "family": "cam",
                        "message": "auth revoke requires --yes when no stdin is attached",
                    }
                ),
                err=True,
            )
            sys.exit(64)
        if not confirmed:
            click.echo("Aborted.", err=True)
            sys.exit(0)

    try:
        creds = load_credentials(creds_path)
        revoke_refresh_token(creds)
    except CredentialError as exc:
        _exit_with_credential_error(exc)
        return  # unreachable

    # Scrub local file: atomic-replace with an empty stub. We bypass
    # ``save_credentials`` here because the stub deliberately fails the
    # CamCredentials schema (no fields).
    _scrub_credentials(creds_path)

    payload = {"status": "revoked", "credentials_path": str(creds_path)}
    _emit(
        payload,
        output=output,
        text_lines=[
            "Cam OAuth refresh token revoked at Google.",
            f"Local credentials scrubbed at {creds_path}.",
            "Run `nest-cli auth setup` to re-authorize.",
        ],
    )


def _scrub_credentials(path: Path) -> None:
    """Atomically replace the credentials file with an empty JSON stub.

    Mirrors the ``save_credentials`` write pattern (tempfile + fsync +
    rename + chmod 0600) so the post-revoke state is indistinguishable
    from any other write at the filesystem layer.
    """
    import os
    import tempfile

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
@click.option(
    "--output",
    type=click.Choice(_OUTPUT_CHOICES),
    default=_OUTPUT_TEXT,
    show_default=True,
    help="Output format.",
)
def cmd_status(output: str) -> None:
    """Print the cam credentials status with secrets redacted.

    Implements the cam-only subset of FR-CRED-10. Engineer B's full
    ``auth status`` (with both cam and wifi rows) lives in a different
    module; this verb reports only the cam family.

    Redaction (FR-CRED-10, §6.7): the refresh token, access token, and
    OAuth client secret are NEVER emitted. ``oauth_client_id`` is
    truncated to its trailing 8 characters.
    """
    creds_path = default_credentials_path()
    if not creds_path.exists():
        payload = {
            "family": "cam",
            "configured": False,
            "credentials_path": str(creds_path),
        }
        _emit(
            payload,
            output=output,
            text_lines=[
                "family: cam",
                "configured: false",
                f"credentials_path: {creds_path}",
                "(run `nest-cli auth setup` to configure)",
            ],
        )
        return

    # Detect the post-revoke empty-stub state ahead of full validation so
    # the operator gets a clean "not configured" message rather than a
    # schema-violation crash.
    try:
        raw = creds_path.read_text(encoding="utf-8")
    except OSError as exc:
        click.echo(
            json.dumps(
                {
                    "error": "config_error",
                    "exit_code": 6,
                    "family": "cam",
                    "message": f"could not read {creds_path}: {exc}",
                }
            ),
            err=True,
        )
        sys.exit(6)

    if raw.strip() in ("{}", ""):
        payload = {
            "family": "cam",
            "configured": False,
            "credentials_path": str(creds_path),
            "note": "credentials previously revoked",
        }
        _emit(
            payload,
            output=output,
            text_lines=[
                "family: cam",
                "configured: false",
                f"credentials_path: {creds_path}",
                "note: credentials previously revoked",
            ],
        )
        return

    try:
        creds = load_credentials(creds_path)
    except CredentialError as exc:
        _exit_with_credential_error(exc)
        return  # unreachable

    expires_at_iso = creds.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    now = datetime.now(UTC)
    seconds_until_expiry = int((creds.expires_at.astimezone(UTC) - now).total_seconds())

    payload = {
        "family": "cam",
        "configured": True,
        "credentials_path": str(creds_path),
        "google_cloud_project_id": creds.google_cloud_project_id,
        "oauth_client_id_redacted": _redact_client_id(creds.oauth_client_id),
        "expires_at": expires_at_iso,
        "time_until_expiry_seconds": seconds_until_expiry,
    }
    _emit(
        payload,
        output=output,
        text_lines=[
            "family: cam",
            "configured: true",
            f"credentials_path: {creds_path}",
            f"google_cloud_project_id: {creds.google_cloud_project_id}",
            f"oauth_client_id (redacted): {_redact_client_id(creds.oauth_client_id)}",
            f"expires_at: {expires_at_iso}",
            f"time_until_expiry_seconds: {seconds_until_expiry}",
        ],
    )

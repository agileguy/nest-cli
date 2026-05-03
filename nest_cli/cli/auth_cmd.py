"""Click subgroup ``auth`` — credentials management for cam and wifi families.

Verbs (per SRD §5.5):

- ``auth setup``        — interactive OAuth Desktop flow (FR-CRED-1, FR-CRED-2).
- ``auth refresh``      — force-rotate the access token (FR-CRED-4).
- ``auth revoke``       — invalidate at Google + scrub local file (FR-CRED-5).
- ``auth wifi-setup``   — derive Foyer master token from Android master token
  (FR-CRED-7, FR-CRED-8). Requires ``--experimental-wifi``.
- ``auth wifi-revoke``  — atomically scrub local wifi credentials and emit a
  stderr reminder (FR-CRED-9). Requires ``--experimental-wifi``.
- ``auth status``       — read both credential families and emit redacted
  summary (FR-CRED-10 — array of two records: cam and wifi).

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
import re
import tempfile
from datetime import UTC, datetime
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
from nest_cli.auth.wifi_credentials import (
    WIFI_REVOCATION_REMINDER,
    WifiCredentialError,
    default_wifi_credentials_path,
    load_wifi_credentials,
    revoke_wifi_credentials,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli._shared import (
    exit_on_structured_error,
    experimental_wifi_gate_or_exit,
)
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_USAGE_ERROR,
    StructuredError,
)
from nest_cli.output import OutputMode, add_output_options, emit

# Environment variable that ``auth wifi-setup`` can read to receive an
# Android master token without the operator having to pipe stdin or pass
# a file. Documented in the verb's ``--help`` (FR-CRED-7).
_ENV_MASTER_TOKEN = "GOOGLE_ANDROID_MASTER_TOKEN"

# Phase B: the Foyer access-token mint path needs the same device's
# ``android_id`` (16-char hex from /data/data/com.google.android.gsf/
# databases/gservices.db). Mirrors the master-token UX: flag > env > prompt.
_ENV_ANDROID_ID = "GOOGLE_ANDROID_ID"
_ANDROID_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")

# Phase C: ``auth wifi-refresh-bootstrap`` accepts a Google OAuth refresh
# token via flag, env, or interactive stdin prompt. The token shape is
# the standard Google form ``1//<chars>`` — we validate the prefix +
# allowed character set so a paste error surfaces cleanly as exit 6
# rather than as an opaque OAuth failure later.
_ENV_REFRESH_TOKEN = "GOOGLE_REFRESH_TOKEN"
_REFRESH_TOKEN_PATTERN = re.compile(r"^1//[\w-]+$")


def _redact_client_id(client_id: str) -> str:
    """Redact all but the trailing 8 characters (auth status output only)."""
    if len(client_id) <= 8:
        return "***"
    return "***" + client_id[-8:]


def _credential_error_to_structured(exc: CredentialError) -> StructuredError:
    """Convert a cam-side CredentialError into a StructuredError envelope.

    Cam-side errors do NOT carry the ``family`` discriminator on the
    wire — the v0.1.0 / v0.2.x envelope shipped without it and we keep
    that bit-identical for back-compat (documented deviation in
    ARCHITECTURE.md).
    """
    return StructuredError(
        code=exc.exit_code,
        message=str(exc),
        hint=exc.hint,
    )


def _wifi_credential_error_to_structured(exc: WifiCredentialError) -> StructuredError:
    """Convert a wifi-side WifiCredentialError into a StructuredError envelope.

    Wifi-side errors DO carry ``family="wifi"`` on the SRD §11.3
    envelope (per the audit's recommendation that the wifi family ships
    SRD-aligned). Cam-side stays without ``family`` for back-compat.
    """
    return StructuredError(
        code=exc.exit_code,
        message=str(exc),
        hint=exc.hint,
        family="wifi",
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
    """Print cam + wifi credentials status with secrets redacted.

    Implements FR-CRED-10. Output is a JSON array in ``--json`` mode:
    Phase 3 emits TWO elements (one per family — cam first, then wifi)
    so operators have a single place to see "what is this CLI authorized
    to do" (Decision 22).

    Each family record includes ``configured`` and ``credentials_path``
    unconditionally. Configured records additionally carry the redacted
    metadata for that family (cam: project id, redacted client id,
    access-token expiry; wifi: account email, issued-at timestamp).
    Unconfigured records carry a ``note`` distinguishing
    "never set up" from "previously revoked".

    Redaction (§6.7): refresh token, access token, OAuth client secret,
    and Foyer master token are NEVER emitted. ``oauth_client_id`` is
    truncated to its trailing 8 characters.
    """
    cam_record = _build_cam_status_record(output_mode)
    wifi_record = _build_wifi_status_record(output_mode)
    emit([cam_record, wifi_record], output_mode)


def _build_cam_status_record(output_mode: OutputMode) -> dict[str, object]:
    """Compose the cam-side ``auth status`` record (mirrors v0.1.0/v0.2.x).

    Branches:

    1. File missing → ``configured=false``.
    2. File present, ``{}`` stub (post-revoke) → ``configured=false`` +
       ``note="credentials previously revoked"``.
    3. File present, schema-valid → ``configured=true`` + redacted fields.

    Schema-violation or chmod-violation paths surface via
    ``exit_on_structured_error`` (operator sees exit 2 / 6).
    """
    creds_path = default_credentials_path()
    if not creds_path.exists():
        return {
            "family": "cam",
            "configured": False,
            "credentials_path": str(creds_path),
        }

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
        return {
            "family": "cam",
            "configured": False,
            "credentials_path": str(creds_path),
            "note": "credentials previously revoked",
        }

    try:
        creds = load_credentials(creds_path)
    except CredentialError as exc:
        exit_on_structured_error(_credential_error_to_structured(exc), output_mode)

    now = datetime.now(UTC)
    seconds_until_expiry = int((creds.expires_at.astimezone(UTC) - now).total_seconds())

    return {
        "family": "cam",
        "configured": True,
        "credentials_path": str(creds_path),
        "google_cloud_project_id": creds.google_cloud_project_id,
        "oauth_client_id_redacted": _redact_client_id(creds.oauth_client_id),
        "expires_at": creds.expires_at,
        "time_until_expiry_seconds": seconds_until_expiry,
    }


def _build_wifi_status_record(output_mode: OutputMode) -> dict[str, object]:
    """Compose the wifi-side ``auth status`` record (FR-CRED-10).

    Same three-branch shape as the cam record. Configured records emit
    ``google_account_email`` and ``issued_at`` (per AuthRecord §10.12);
    the master token is NEVER surfaced (§6.7 redaction).
    """
    creds_path = default_wifi_credentials_path()
    if not creds_path.exists():
        return {
            "family": "wifi",
            "configured": False,
            "credentials_path": str(creds_path),
        }

    try:
        raw = creds_path.read_text(encoding="utf-8")
    except OSError as exc:
        exit_on_structured_error(
            StructuredError(
                code=EXIT_AUTH_ERROR,
                message=f"could not read {creds_path}: {exc}",
                family="wifi",
            ),
            output_mode,
        )

    if raw.strip() in ("{}", ""):
        return {
            "family": "wifi",
            "configured": False,
            "credentials_path": str(creds_path),
            "note": "credentials previously revoked",
        }

    try:
        wifi_creds = load_wifi_credentials(creds_path)
    except WifiCredentialError as exc:
        exit_on_structured_error(_wifi_credential_error_to_structured(exc), output_mode)

    return {
        "family": "wifi",
        "configured": True,
        "credentials_path": str(creds_path),
        "google_account_email": wifi_creds.google_account_email,
        "issued_at": wifi_creds.issued_at,
        "schema_version": wifi_creds.version,
        "refresh_token_present": wifi_creds.refresh_token is not None,
    }


# ---------------------------------------------------------------------------
# auth wifi-setup / auth wifi-revoke (Phase 3)
# ---------------------------------------------------------------------------


@auth_group.command("wifi-setup")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@click.option(
    "--master-token-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help=(
        "Read the Android master token from a file (one line). Mutually "
        "exclusive with stdin and the GOOGLE_ANDROID_MASTER_TOKEN env var "
        "(precedence: file > env > stdin)."
    ),
)
@click.option(
    "--google-account-email",
    type=str,
    default=None,
    help="Google account email that owns the Nest Wi-Fi mesh. Prompted if absent.",
)
@click.option(
    "--android-id",
    "android_id",
    type=str,
    default=None,
    help=(
        "16-char hex Android device id (from gservices.db on the paired Android "
        "device). Required for Foyer access-token minting; precedence: "
        "--android-id > GOOGLE_ANDROID_ID env > stdin prompt."
    ),
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite an existing wifi credentials file. Required if one already exists.",
)
@add_output_options
def cmd_wifi_setup(
    experimental_wifi: bool,
    master_token_file: Path | None,
    google_account_email: str | None,
    android_id: str | None,
    overwrite: bool,
    output_mode: OutputMode,
) -> None:
    """Persist a Foyer master token to credentials-wifi.json (FR-CRED-7).

    Bootstrap (out-of-band, per SRD §6.3): the operator first extracts
    an Android master token from a paired Android device using a
    community tool such as ``gpsoauth``, the Android ``Auth`` library
    impersonation, or the ``aiohomekit-google-companion`` script. The
    CLI does NOT perform this Android-side extraction.

    Token sources, in precedence order:

    1. ``--master-token-file <path>`` — read the token from a file.
    2. ``GOOGLE_ANDROID_MASTER_TOKEN`` env var — read the token from env.
    3. stdin — last resort; uses ``getpass`` to avoid shell history.

    The persisted JSON shape is FR-CRED-8 v2: ``{"version": 2, "type":
    "foyer", "google_account_email": ..., "master_token": ...,
    "android_id": <16-char hex>, "issued_at": <rfc3339>}``. File mode
    is chmod 0600. Phase B (2026-05-03) bumped the schema to v2 to
    persist the ``android_id`` needed by the gpsoauth → Foyer mint path.

    SRD §6.4 reminds operators that Foyer has no programmatic revoke
    endpoint; the only way to invalidate this token at Google's end is
    to log out the paired Android session at
    ``myaccount.google.com/permissions``. ``auth wifi-revoke`` only
    scrubs the local file.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="auth wifi-setup")

    creds_path = default_wifi_credentials_path()
    if creds_path.exists() and not overwrite:
        # FR-CRED-2 mirror: refuse to clobber. Empty stub from a prior
        # revoke also counts as "exists" — operator must pass --overwrite
        # to recreate the file with fresh credentials.
        raw = creds_path.read_text(encoding="utf-8").strip()
        if raw not in ("", "{}"):
            exit_on_structured_error(
                StructuredError(
                    code=EXIT_AUTH_ERROR,
                    message=f"wifi credentials already exist at {creds_path}",
                    hint=(
                        "Pass --overwrite to replace them, or run "
                        "`nest-cli auth wifi-revoke --experimental-wifi` first."
                    ),
                    family="wifi",
                ),
                output_mode,
            )

    if google_account_email is None:
        google_account_email = click.prompt("Google account email", type=str)

    master_token = _resolve_master_token(master_token_file, output_mode)
    resolved_android_id = _resolve_android_id(android_id, output_mode)

    creds = WifiCredentials(
        version=2,
        type="foyer",
        google_account_email=google_account_email,
        master_token=master_token,
        android_id=resolved_android_id,
        issued_at=datetime.now(UTC),
    )

    try:
        save_wifi_credentials(creds_path, creds)
    except WifiCredentialError as exc:
        exit_on_structured_error(_wifi_credential_error_to_structured(exc), output_mode)

    payload = {
        "status": "ok",
        "credentials_path": str(creds_path),
        "google_account_email": creds.google_account_email,
        "issued_at": creds.issued_at,
    }
    emit(payload, output_mode)


def _resolve_master_token(master_token_file: Path | None, output_mode: OutputMode) -> str:
    """Resolve a master token from file → env → stdin in precedence order.

    Returns the trimmed token string. Empty / whitespace-only sources
    surface as exit 6 (config_error, family=wifi) — the operator passed
    a real source but it was empty, which is a misconfigured input not
    an auth failure. The SRD-aligned way to say "I don't have a
    credential" is to omit the source entirely (then we exit 2 from
    load-time, not setup-time).
    """
    if master_token_file is not None:
        text = master_token_file.read_text(encoding="utf-8").strip()
        if not text:
            exit_on_structured_error(
                StructuredError(
                    code=EXIT_CONFIG_ERROR,
                    message=f"--master-token-file at {master_token_file} is empty",
                    hint="Re-extract the Android master token and re-write the file.",
                    family="wifi",
                ),
                output_mode,
            )
        return text

    env_value = os.environ.get(_ENV_MASTER_TOKEN, "").strip()
    if env_value:
        return env_value

    # stdin fallback. ``click.prompt(hide_input=True)`` runs ``getpass``
    # under the hood, so the token doesn't echo and doesn't end up in
    # shell history. CliRunner.invoke(input=...) populates this path
    # for tests.
    token = click.prompt(
        "Android master token (hidden)",
        type=str,
        hide_input=True,
        default="",
        show_default=False,
    )
    token = (token or "").strip()
    if not token:
        exit_on_structured_error(
            StructuredError(
                code=EXIT_CONFIG_ERROR,
                message="no master token supplied (file, env, or stdin all empty)",
                hint=(
                    f"Pipe the token into stdin, set the {_ENV_MASTER_TOKEN} env "
                    "var, or pass --master-token-file."
                ),
                family="wifi",
            ),
            output_mode,
        )
    return token


def _resolve_android_id(android_id_flag: str | None, output_mode: OutputMode) -> str:
    """Resolve a 16-char hex android_id from flag → env → prompt order.

    The Foyer access-token mint path needs the same Android device's
    ``android_id`` that was used to derive the master token; otherwise
    Google's auth backend rejects the OAuth call with an opaque
    "Authorization Error". The value is a 16-char lowercase hex string
    pulled from ``/data/data/com.google.android.gsf/databases/gservices.db``
    on the rooted Android device.

    Empty / non-hex / wrong-length sources surface as exit 6
    (config_error, family=wifi). Same posture as ``_resolve_master_token``:
    "I don't have a credential" means omit the source entirely (then we
    exit 2 from load-time, not setup-time); a present-but-bad source is
    a misconfigured input, not an auth failure.
    """
    if android_id_flag is not None:
        return _validate_android_id(
            android_id_flag.strip(), source="--android-id", output_mode=output_mode
        )

    env_value = os.environ.get(_ENV_ANDROID_ID, "").strip()
    if env_value:
        return _validate_android_id(env_value, source=_ENV_ANDROID_ID, output_mode=output_mode)

    prompted = click.prompt(
        "Android android_id (16-char hex from gservices.db)",
        type=str,
        default="",
        show_default=False,
    )
    prompted = (prompted or "").strip()
    if not prompted:
        exit_on_structured_error(
            StructuredError(
                code=EXIT_CONFIG_ERROR,
                message="no android_id supplied (flag, env, or stdin all empty)",
                hint=(
                    f"Pass --android-id <hex>, set the {_ENV_ANDROID_ID} env var, "
                    "or type the 16-char hex value at the prompt. Extract it from "
                    "/data/data/com.google.android.gsf/databases/gservices.db on "
                    "the paired (rooted) Android device."
                ),
                family="wifi",
            ),
            output_mode,
        )
    return _validate_android_id(prompted, source="stdin", output_mode=output_mode)


def _validate_android_id(value: str, *, source: str, output_mode: OutputMode) -> str:
    """Reject non-hex / wrong-length android_id with EXIT_CONFIG_ERROR."""
    if not _ANDROID_ID_PATTERN.fullmatch(value):
        exit_on_structured_error(
            StructuredError(
                code=EXIT_CONFIG_ERROR,
                message=(
                    f"android_id from {source} is not a 16-char lowercase hex string "
                    f"(got {len(value)} chars)"
                ),
                hint=(
                    "android_id must match ^[0-9a-f]{16}$. Re-extract from "
                    "/data/data/com.google.android.gsf/databases/gservices.db "
                    "on the paired (rooted) Android device."
                ),
                family="wifi",
            ),
            output_mode,
        )
    return value


@auth_group.command("wifi-revoke")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt (required in non-tty contexts).",
)
@add_output_options
def cmd_wifi_revoke(
    experimental_wifi: bool,
    yes: bool,
    output_mode: OutputMode,
) -> None:
    """Atomically scrub the wifi credentials file + remind operator (FR-CRED-9).

    Foyer has NO programmatic revoke endpoint. This verb only scrubs the
    local file (atomic empty-stub replacement) and emits a stderr
    reminder that the only true revocation path is the Google account
    security panel (``myaccount.google.com/permissions``) or a Google
    account password change (SRD §6.4). Operators are expected to weigh
    that blast radius before invoking either.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="auth wifi-revoke")

    creds_path = default_wifi_credentials_path()
    if not creds_path.exists():
        # Nothing to scrub. Exit 0 — idempotency is more useful than
        # erroring out on a no-op.
        click.echo(f"No wifi credentials at {creds_path}; nothing to revoke.", err=True)
        click.echo(WIFI_REVOCATION_REMINDER, err=True)
        emit({"status": "noop", "credentials_path": str(creds_path)}, output_mode)
        return

    if not yes:
        try:
            confirmed = click.confirm(
                "Scrub local wifi credentials? (Foyer has no programmatic revoke; "
                "this only removes the local file.)",
                default=False,
            )
        except click.exceptions.Abort:
            exit_on_structured_error(
                StructuredError(
                    code=EXIT_USAGE_ERROR,
                    message="auth wifi-revoke requires --yes when no stdin is attached",
                    family="wifi",
                ),
                output_mode,
            )
        if not confirmed:
            click.echo("Aborted.", err=True)
            return

    try:
        revoke_wifi_credentials(creds_path)
    except WifiCredentialError as exc:
        exit_on_structured_error(_wifi_credential_error_to_structured(exc), output_mode)

    # FR-CRED-9: emit the stderr reminder. The reminder is operator
    # guidance, not a structured-error path — it lands as a plain stderr
    # line so it's visible in any output mode.
    click.echo(WIFI_REVOCATION_REMINDER, err=True)

    payload = {"status": "revoked", "credentials_path": str(creds_path)}
    emit(payload, output_mode)


# ---------------------------------------------------------------------------
# auth wifi-refresh-bootstrap (Phase C)
# ---------------------------------------------------------------------------


def _stdin_is_tty() -> bool:
    """Return ``True`` if stdin reports as a tty.

    Indirected through this helper so tests can monkeypatch the value
    deterministically (CliRunner replaces stdin with an in-memory stream
    whose ``isatty()`` is always False).
    """
    import sys

    isatty = getattr(sys.stdin, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _resolve_refresh_token(
    flag_value: str | None,
    output_mode: OutputMode,
) -> str:
    """Resolve a refresh token from flag → env → stdin in precedence order.

    Returns the trimmed, format-validated token. Empty / non-matching
    sources surface as exit 6 (config_error, family=wifi). Stdin is
    only consulted when stdin is a tty (CliRunner controls this for
    tests via the ``input=`` parameter, which makes the input non-tty
    but still readable; click.prompt then reads from the buffer).
    """
    if flag_value is not None:
        return _validate_refresh_token(
            flag_value.strip(), source="--refresh-token", output_mode=output_mode
        )

    env_value = os.environ.get(_ENV_REFRESH_TOKEN, "").strip()
    if env_value:
        return _validate_refresh_token(
            env_value, source=_ENV_REFRESH_TOKEN, output_mode=output_mode
        )

    # Last resort: prompt on stdin. ``hide_input=True`` masks the value
    # so it doesn't echo and doesn't end up in shell history.
    prompted = click.prompt(
        "Google OAuth refresh token (1//...)",
        type=str,
        hide_input=True,
        default="",
        show_default=False,
    )
    prompted = (prompted or "").strip()
    if not prompted:
        exit_on_structured_error(
            StructuredError(
                code=EXIT_CONFIG_ERROR,
                message="no refresh token supplied (flag, env, or stdin all empty)",
                hint=(
                    f"Pass --refresh-token <1//...>, set the {_ENV_REFRESH_TOKEN} env var, "
                    "or paste the value at the prompt. See `auth wifi-refresh-bootstrap "
                    "--help` for how to obtain one (web OAuth or AngeloD2022/onhubauthhelper)."
                ),
                family="wifi",
            ),
            output_mode,
        )
    return _validate_refresh_token(prompted, source="stdin", output_mode=output_mode)


def _validate_refresh_token(value: str, *, source: str, output_mode: OutputMode) -> str:
    """Reject a refresh token that doesn't match ``^1//[\\w-]+$``."""
    if not _REFRESH_TOKEN_PATTERN.fullmatch(value):
        exit_on_structured_error(
            StructuredError(
                code=EXIT_CONFIG_ERROR,
                message=(
                    f"refresh_token from {source} is not a Google OAuth refresh token "
                    f"(expected '1//...', got {len(value)} chars starting "
                    f"{value[:5]!r})"
                ),
                hint=(
                    "Refresh tokens look like '1//09abc-DEF_xyz...'. Re-extract from "
                    "your OAuth flow or AngeloD2022/onhubauthhelper output and retry."
                ),
                family="wifi",
            ),
            output_mode,
        )
    return value


@auth_group.command("wifi-refresh-bootstrap")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@click.option(
    "--refresh-token",
    "refresh_token",
    type=str,
    default=None,
    help=(
        "Google OAuth refresh token (1//...). Mutually exclusive with the "
        "GOOGLE_REFRESH_TOKEN env var and the interactive prompt; precedence: "
        "flag > env > stdin. Obtain via the AngeloD2022/onhubauthhelper "
        "tool or by running a manual OAuth flow against the Google Wifi web "
        "client_id (936475272427.apps.googleusercontent.com)."
    ),
)
@add_output_options
def cmd_wifi_refresh_bootstrap(
    experimental_wifi: bool,
    refresh_token: str | None,
    output_mode: OutputMode,
) -> None:
    """Persist an OAuth refresh token alongside an existing v2 wifi credential.

    Phase C action verbs (pause, prioritize, speedtest, reboot, ...) hit
    Foyer REST endpoints at ``/v2/groups/...`` that reject the
    gpsoauth-minted access token used by the Phase B gRPC read path.
    They require an OnHub-scoped access token derived through a two-step
    OAuth chain rooted in a standard Google OAuth refresh token
    (``1//...``). This verb persists that refresh token alongside the
    operator's existing v2 master_token / android_id, upgrading the file
    to schema v3 in place.

    Token sources, in precedence order:

    1. ``--refresh-token <1//...>`` — pass the value as a flag.
    2. ``GOOGLE_REFRESH_TOKEN`` env var — read the token from env.
    3. Interactive stdin prompt — last resort; uses ``getpass`` so the
       value doesn't echo and doesn't end up in shell history.

    The verb requires a pre-existing v2 credentials file (created by
    ``auth wifi-setup``). If no v2 credentials exist, the verb exits 6
    with a hint pointing at ``auth wifi-setup``. Existing v3 records
    have their ``refresh_token`` field overwritten in place; the
    ``master_token``, ``android_id``, ``google_account_email``, and
    ``issued_at`` fields are preserved.

    To obtain a refresh token: run a one-time OAuth web flow against the
    Google Wifi web client_id ``936475272427.apps.googleusercontent.com``
    (see Google's OAuth playground or the AngeloD2022/onhubauthhelper
    tool on GitHub for a ready-made implementation).
    """
    experimental_wifi_gate_or_exit(
        experimental_wifi, output_mode, verb="auth wifi-refresh-bootstrap"
    )

    creds_path = default_wifi_credentials_path()
    if not creds_path.exists():
        exit_on_structured_error(
            StructuredError(
                code=EXIT_CONFIG_ERROR,
                message=f"no wifi credentials at {creds_path}",
                hint=(
                    "Run `nest-cli auth wifi-setup --experimental-wifi` first to "
                    "create a v2 credentials file, then re-run this command to "
                    "upgrade it to v3."
                ),
                family="wifi",
            ),
            output_mode,
        )

    try:
        existing = load_wifi_credentials(creds_path)
    except WifiCredentialError as exc:
        exit_on_structured_error(_wifi_credential_error_to_structured(exc), output_mode)

    token = _resolve_refresh_token(refresh_token, output_mode)

    upgraded = WifiCredentials(
        version=3,
        type=existing.type,
        google_account_email=existing.google_account_email,
        master_token=existing.master_token,
        android_id=existing.android_id,
        issued_at=existing.issued_at,
        refresh_token=token,
    )

    try:
        save_wifi_credentials(creds_path, upgraded)
    except WifiCredentialError as exc:
        exit_on_structured_error(_wifi_credential_error_to_structured(exc), output_mode)

    payload = {
        "status": "ok",
        "credentials_path": str(creds_path),
        "version": upgraded.version,
        "google_account_email": upgraded.google_account_email,
        "refresh_token_present": True,
    }
    emit(payload, output_mode)

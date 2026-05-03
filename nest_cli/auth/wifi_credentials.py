"""On-disk credentials I/O for the wifi side (Foyer master token).

This module mirrors the cam-side ``nest_cli.auth.credentials`` flow with
a different on-disk schema (FR-CRED-8) and different revocation semantics
(FR-CRED-9 — Foyer has no programmatic revoke endpoint, so we scrub the
local file and remind the operator to revoke at Google's end).

What it owns:

- Default path under ``$XDG_CONFIG_HOME`` / ``~/.config/nest-cli``
  (``credentials-wifi.json``) — separate file from the cam-side OAuth
  credentials per SRD Decision 8 / §6.1.
- Atomic write of ``credentials-wifi.json`` (tmpfile + ``fsync`` +
  ``rename``, then ``chmod 0o600``) — FR-CRED-7 / FR-CRED-8.
- ``chmod 0o600`` enforcement on read — FR-CRED-12.
- Per-file ``flock`` serialization for concurrent writes — FR-CRED-13.
- Atomic empty-stub replacement on revoke — FR-CRED-9.

We deliberately reuse the cam-side ``_ensure_parent_dir``, ``_file_lock``,
and ``enforce_credentials_chmod`` private helpers rather than duplicating
their flock + symlink-hardening invariants. They live in the same
``nest_cli.auth`` package; the leading underscore is convention only.

Threat-model note (SRD §4.7): the Foyer master token has a HIGHER blast
radius than the OAuth refresh token (Google-account-wide, not just
sdm.service). The chmod 0600 + atomic-rename + flock invariants are the
SAME as the cam side; the difference is FR-CRED-9 — Foyer has no
revoke endpoint, so the only programmatic action is local-file scrub
plus a stderr reminder pointing at ``myaccount.google.com/permissions``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from nest_cli.auth.credentials import (
    CredentialError,
    _ensure_parent_dir,
    _file_lock,
    enforce_credentials_chmod,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
)

__all__ = [
    "WIFI_REVOCATION_REMINDER",
    "WifiCredentialError",
    "default_wifi_credentials_path",
    "load_wifi_credentials",
    "revoke_wifi_credentials",
    "save_wifi_credentials",
]


# ---------------------------------------------------------------------------
# Operator-facing reminder for FR-CRED-9
# ---------------------------------------------------------------------------

WIFI_REVOCATION_REMINDER = (
    "Foyer has no programmatic revoke endpoint. To fully revoke this "
    "credential at Google's end, visit https://myaccount.google.com/permissions "
    "and remove the paired Android app session that derived this master token. "
    "Alternatively, change your Google account password."
)


# ---------------------------------------------------------------------------
# CredentialError mirror
# ---------------------------------------------------------------------------


class WifiCredentialError(Exception):
    """A wifi-credentials failure with an explicit CLI exit code.

    Mirrors ``nest_cli.auth.credentials.CredentialError`` shape so the
    CLI layer can convert either family's exception into a
    ``StructuredError`` with one helper. ``family`` is hardcoded to
    ``"wifi"`` so the structured-error envelope (SRD §11.3) carries
    the right discriminator.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.hint = hint
        self.family = "wifi"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _config_home() -> Path:
    """Return the resolved nest-cli config directory.

    Mirrors ``nest_cli.auth.credentials._config_home`` exactly so the cam
    and wifi credential files land in the same parent dir
    (``~/.config/nest-cli/``).
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "nest-cli"
    return Path.home() / ".config" / "nest-cli"


def default_wifi_credentials_path() -> Path:
    """Return ``~/.config/nest-cli/credentials-wifi.json`` (FR-CRED-8 path)."""
    return _config_home() / "credentials-wifi.json"


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def load_wifi_credentials(path: Path) -> WifiCredentials:
    """Read + validate ``credentials-wifi.json``.

    Maps failure modes to SRD exit codes (FR-CRED-7 / FR-CRED-8 / FR-CRED-12):

    - File missing → exit 2 (auth, with hint pointing at ``auth wifi-setup``).
    - Mode too permissive → exit 2 (FR-CRED-12).
    - JSON decode error → exit 6 (config error).
    - Schema violation (extra keys, missing keys, wrong type) → exit 6.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WifiCredentialError(
            f"wifi credentials not found at {path}",
            exit_code=EXIT_AUTH_ERROR,
            hint="Run `nest-cli auth wifi-setup --experimental-wifi` to create them.",
        ) from exc
    # ``enforce_credentials_chmod`` raises ``CredentialError`` (cam-side
    # type). Re-wrap as ``WifiCredentialError`` so the wifi family flag
    # propagates and the CLI handler uses the right error path.
    try:
        enforce_credentials_chmod(path)
    except CredentialError as exc:
        raise WifiCredentialError(
            str(exc),
            exit_code=exc.exit_code,
            hint=exc.hint,
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WifiCredentialError(
            f"wifi credentials at {path} are not valid JSON: {exc.msg}",
            exit_code=EXIT_CONFIG_ERROR,
            hint="Re-run `nest-cli auth wifi-setup --experimental-wifi --overwrite`.",
        ) from exc
    try:
        return WifiCredentials.model_validate(data)
    except ValidationError as exc:
        raise WifiCredentialError(
            f"wifi credentials at {path} failed schema validation: {exc.errors()[0]['msg']}",
            exit_code=EXIT_CONFIG_ERROR,
            hint="Re-run `nest-cli auth wifi-setup --experimental-wifi --overwrite`.",
        ) from exc


def save_wifi_credentials(path: Path, creds: WifiCredentials) -> None:
    """Atomically write ``creds`` to ``path`` with mode 0o600.

    Sequence (FR-CRED-7 / FR-CRED-8 / FR-CRED-13):

    1. Acquire ``flock`` on ``<path>.lock`` (per FR-CRED-13 — same lock
       pattern as cam side, just a different lock path).
    2. Ensure parent dir exists with mode 0o700.
    3. Write JSON to a sibling tempfile in the same dir.
    4. ``fsync`` the tempfile so the bytes hit disk before we publish.
    5. ``os.rename`` the tempfile over ``path``.
    6. ``chmod 0o600`` on the published file (deterministic regardless
       of the operator's umask).
    """
    _ensure_parent_dir(path)
    payload = creds.model_dump_json(indent=2)
    with _file_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.rename(tmp_path, path)
            os.chmod(path, 0o600)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise


def revoke_wifi_credentials(path: Path) -> None:
    """Atomically replace the wifi credentials file with an empty stub (FR-CRED-9).

    Foyer has no programmatic revoke endpoint, so this verb only does the
    local-file scrub. The CLI layer is responsible for emitting
    ``WIFI_REVOCATION_REMINDER`` on stderr (SRD §6.4) so the operator
    knows where to revoke at Google's end.

    Bypasses ``WifiCredentials`` schema validation: the empty stub
    deliberately fails the schema (no required fields), which is what
    ``auth status --json`` keys on to render ``configured=false`` per
    the SRD §6.7 redaction rules.
    """
    _ensure_parent_dir(path)
    with _file_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("{}")
                fh.flush()
                os.fsync(fh.fileno())
            os.rename(tmp_path, path)
            os.chmod(path, 0o600)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise

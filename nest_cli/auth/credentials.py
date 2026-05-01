"""On-disk credentials I/O for the cam side (SDM OAuth).

This module owns:

- Default paths under ``$XDG_CONFIG_HOME`` / ``~/.config/nest-cli``.
- Atomic write of ``credentials-cam.json`` (tmpfile + ``fsync`` + ``rename``,
  then ``chmod 0o600``) — FR-CRED-1 / FR-CRED-2.
- ``chmod 0o600`` enforcement on read — FR-CRED-12.
- Per-file ``flock`` serialization for concurrent writes — FR-CRED-13.
- Lazy access-token refresh against ``oauth2.googleapis.com/token`` —
  FR-CRED-6.
- Refresh-token revocation against ``oauth2.googleapis.com/revoke`` —
  FR-CRED-5.

External-dep policy
-------------------

We deliberately use stdlib ``urllib.request`` for the two HTTPS calls
(``token`` refresh + ``revoke``). ``requests`` is a transitive dep via
``google-auth-oauthlib``, but pulling it in here would couple our module
graph to a transitive — fragile across upstream pins. ``httpx`` is not
already in the tree and adding it would touch ``pyproject.toml``, which is
out of Engineer A's scope this phase. Two POST calls do not justify a new
dependency.

Exit-code coupling
------------------

SRD §11.1 names exit codes 2 (auth), 3 (network), and 6 (config). The
canonical home for these constants is ``nest_cli/errors.py``; we import
the SRD-numbered constants from there and attach them to each
``CredentialError`` via the ``.exit_code`` member.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import random
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from nest_cli.auth.types import CamCredentials
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_USAGE_ERROR,
)

__all__ = [
    "EXIT_AUTH_ERROR",
    "EXIT_CONFIG_ERROR",
    "EXIT_NETWORK_ERROR",
    "EXIT_USAGE_ERROR",
    "GOOGLE_OAUTH_REVOKE_URL",
    "GOOGLE_OAUTH_TOKEN_URL",
    "CredentialError",
    "default_credentials_path",
    "default_token_cache_dir",
    "enforce_credentials_chmod",
    "load_credentials",
    "refresh_access_token_if_needed",
    "revoke_refresh_token",
    "save_credentials",
]

# Refresh window: re-mint the access token if it expires within this many
# seconds (FR-CRED-6 says 60s).
ACCESS_TOKEN_REFRESH_WINDOW_S = 60

# Google OAuth endpoints (public, well-known — not configurable).
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# How long to allow a single HTTPS call to hang before declaring the network
# unreachable. SRD FR-CRED-13 sets the lock-acquisition default to 30s for the
# OAuth path; we mirror that for the underlying HTTP call.
HTTP_TIMEOUT_S = 30


class CredentialError(Exception):
    """A credentials-layer failure with an explicit CLI exit code.

    ``exit_code`` is the SRD §11.1 number the caller propagates to
    ``sys.exit``. ``hint`` is the operator-facing actionable next step
    (FR-CRED-2 mandates that the "credentials missing" message names
    ``auth setup``).
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


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _config_home() -> Path:
    """Return the resolved nest-cli config directory.

    Honors ``XDG_CONFIG_HOME`` (Linux convention) and falls back to
    ``~/.config`` on macOS or unset. The trailing ``nest-cli/`` segment is
    appended unconditionally.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "nest-cli"
    return Path.home() / ".config" / "nest-cli"


def default_credentials_path() -> Path:
    """Return ``~/.config/nest-cli/credentials-cam.json`` (FR-CRED-3 path)."""
    return _config_home() / "credentials-cam.json"


def default_token_cache_dir() -> Path:
    """Return ``~/.config/nest-cli/.tokens/`` (FR-CRED-9 mirror, chmod 0700)."""
    return _config_home() / ".tokens"


# ---------------------------------------------------------------------------
# Filesystem primitives
# ---------------------------------------------------------------------------


def _ensure_parent_dir(path: Path) -> None:
    """Create the parent dir with ``chmod 0o700`` if it does not exist.

    The mode is FR-CRED-9-aligned (the token cache dir uses 0700, and the
    credentials dir is the same parent). We do not relax the mode if the dir
    already exists with a tighter setting.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's ``mode`` is masked by umask, so re-apply explicitly.
    os.chmod(parent, 0o700)


@contextlib.contextmanager
def _file_lock(path: Path, *, timeout_s: int = HTTP_TIMEOUT_S) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``<path>.lock`` for the duration.

    The lock file is a sidecar — we never lock the credentials file itself,
    because atomic-rename would invalidate any fd held against the live
    file. POSIX ``flock`` is fine for v0.1.0 (Linux + macOS only per SRD
    §13.2). On lock timeout we raise ``CredentialError`` mapped to exit 3
    (network — FR-CRED-13 explicitly maps lock-acquisition timeout to 3).

    Symlink-race hardening: we open the lock file with ``O_CREAT|O_EXCL|
    O_NOFOLLOW`` first. If the lock file already exists from a prior run
    we re-open with ``O_NOFOLLOW`` only — both paths refuse to follow a
    symlink at the lock path. An attacker who pre-creates a symlink at
    ``<path>.lock`` pointing elsewhere would cause us to lock the wrong
    file (or worse, write to it on a future schema change). Refusing to
    follow makes that race a hard error mapped to exit 2 (auth) — the
    operator sees a clean failure, not a silent compromise.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    _ensure_parent_dir(lock_path)

    flags_create = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW
    flags_open = os.O_RDWR | os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags_create, 0o600)
    except FileExistsError:
        try:
            fd = os.open(lock_path, flags_open)
        except OSError as exc:
            # ``O_NOFOLLOW`` returns ELOOP on a symlink, which surfaces
            # as ``OSError`` with errno 62 on Linux / 62 on macOS. Map
            # any open failure here to a structured auth error rather
            # than letting the operator see an opaque OSError.
            raise CredentialError(
                f"refusing to lock credentials at {lock_path}: "
                "lock file is a symlink, unreadable, or otherwise unsafe",
                exit_code=EXIT_AUTH_ERROR,
                hint=(
                    f"Inspect {lock_path}; if you trust the path, remove it and retry. "
                    "A symlink at this path can indicate a credential-substitution attack."
                ),
            ) from exc

    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CredentialError(
                        f"Timed out waiting {timeout_s}s for credentials lock at {lock_path}",
                        exit_code=EXIT_NETWORK_ERROR,
                        hint="Another nest-cli process is refreshing credentials; retry shortly.",
                    ) from None
                # Add jitter to prevent thundering herd under high contention.
                # Base sleep 50-150ms (100ms ± 50ms jitter).
                time.sleep(0.05 + random.uniform(0, 0.1))
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def enforce_credentials_chmod(path: Path) -> None:
    """Raise if ``path`` mode is more permissive than 0o600 (FR-CRED-12).

    "More permissive" means any bit set in group or other (``0o077``).
    Owner-side bits beyond rw (e.g. exec) are not strictly forbidden by the
    SRD but we don't write them either; we tolerate them on read.
    """
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise CredentialError(
            f"credentials file mode is too permissive (0o{mode:03o}); expected 0o600",
            exit_code=EXIT_AUTH_ERROR,
            hint=f"Run `chmod 600 {path}` to lock the file down.",
        )


def load_credentials(path: Path) -> CamCredentials:
    """Read + validate ``credentials-cam.json``.

    Maps failure modes to SRD exit codes:

    - File missing → exit 2 (FR-CRED-1 hint message: "run ``auth setup``").
    - Mode too permissive → exit 2 (FR-CRED-12, via
      ``enforce_credentials_chmod``).
    - JSON decode error → exit 6 (config error).
    - Schema violation (extra keys, missing keys, wrong type) → exit 6
      (FR-CRED-3).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CredentialError(
            f"cam credentials not found at {path}",
            exit_code=EXIT_AUTH_ERROR,
            hint="Run `nest-cli auth setup` to create them.",
        ) from exc
    enforce_credentials_chmod(path)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialError(
            f"cam credentials at {path} are not valid JSON: {exc.msg}",
            exit_code=EXIT_CONFIG_ERROR,
            hint="Re-run `nest-cli auth setup --overwrite`.",
        ) from exc
    try:
        return CamCredentials.model_validate(data)
    except ValidationError as exc:
        raise CredentialError(
            f"cam credentials at {path} failed schema validation: {exc.errors()[0]['msg']}",
            exit_code=EXIT_CONFIG_ERROR,
            hint="Re-run `nest-cli auth setup --overwrite`.",
        ) from exc


def save_credentials(path: Path, creds: CamCredentials) -> None:
    """Atomically write ``creds`` to ``path`` with mode 0o600.

    Sequence (FR-CRED-1, FR-CRED-2, FR-CRED-6):

    1. Acquire ``flock`` on ``<path>.lock`` (FR-CRED-13).
    2. Ensure parent dir exists with mode 0o700.
    3. Write JSON to a sibling tempfile in the same dir (atomic-rename
       requires same filesystem).
    4. ``fsync`` the tempfile so the bytes hit disk before we publish.
    5. ``os.rename`` the tempfile over ``path``.
    6. ``chmod 0o600`` on the published file.

    The mode is set after the rename rather than relying on the tempfile's
    mode because ``mkstemp`` honors umask and we want a deterministic
    end-state regardless of the operator's umask.
    """
    _ensure_parent_dir(path)
    payload = creds.model_dump_json(indent=2)
    with _file_lock(path):
        # ``mkstemp`` returns (fd, path); we own closing both.
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
            # Clean up the tempfile on any failure path so we don't litter
            # the credentials dir.
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise


# ---------------------------------------------------------------------------
# Token refresh and revoke
# ---------------------------------------------------------------------------


def _post_form(url: str, params: dict[str, str]) -> tuple[int, bytes]:
    """POST a form-urlencoded body to ``url``. Returns (status, body).

    Network-layer failures (DNS, TLS, connection refused) raise
    ``CredentialError`` mapped to exit 3. HTTP responses that the caller
    needs to inspect (4xx/5xx) come back as a normal tuple — the caller
    decides whether to raise.
    """
    body = urllib.parse.urlencode(params).encode("ascii")
    req = urllib.request.Request(  # noqa: S310 - URL is a hardcoded https constant.
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        # HTTP 4xx/5xx: surface to caller, don't treat as a network outage.
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise CredentialError(
            f"network error contacting {url}: {exc.reason}",
            exit_code=EXIT_NETWORK_ERROR,
            hint="Check your internet connection and retry.",
        ) from exc
    except TimeoutError as exc:
        raise CredentialError(
            f"timed out contacting {url} after {HTTP_TIMEOUT_S}s",
            exit_code=EXIT_NETWORK_ERROR,
            hint="Google's OAuth endpoint is slow or unreachable; retry shortly.",
        ) from exc


def refresh_access_token_if_needed(
    creds: CamCredentials,
    path: Path,
    *,
    force: bool = False,
) -> CamCredentials:
    """Refresh + persist if the access token is near or past expiry.

    Returns the (possibly-rotated) ``CamCredentials``. If no refresh was
    needed and ``force`` is False, returns the input unchanged. On refresh
    success, the new credentials are persisted via ``save_credentials``
    (FR-CRED-6). On refresh failure with HTTP 4xx, raises
    ``CredentialError`` mapped to exit 2 — Google rejected the refresh
    token (FR-CRED-5 / FR-18).
    """
    now = datetime.now(UTC)
    expires_at = creds.expires_at
    if expires_at.tzinfo is None:
        # Pydantic v2 parses naive RFC 3339 as naive datetimes; treat as
        # UTC since SRD FR-22 mandates UTC ``Z`` everywhere.
        expires_at = expires_at.replace(tzinfo=UTC)
    needs_refresh = force or (expires_at - now) < timedelta(seconds=ACCESS_TOKEN_REFRESH_WINDOW_S)
    if not needs_refresh:
        return creds

    status, body = _post_form(
        GOOGLE_OAUTH_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": creds.refresh_token,
            "client_id": creds.oauth_client_id,
            "client_secret": creds.oauth_client_secret,
        },
    )
    if status != 200:
        # 4xx here typically means the refresh token was revoked or the
        # client_id/secret pair is stale — auth, not network.
        raise CredentialError(
            f"OAuth token refresh failed (HTTP {status})",
            exit_code=EXIT_AUTH_ERROR,
            hint=(
                "The refresh token may have been revoked. "
                "Run `nest-cli auth setup --overwrite` to re-authorize."
            ),
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CredentialError(
            f"OAuth token endpoint returned non-JSON body: {exc.msg}",
            exit_code=EXIT_NETWORK_ERROR,
        ) from exc

    new_access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(new_access_token, str) or not isinstance(expires_in, int):
        raise CredentialError(
            "OAuth token endpoint response missing access_token or expires_in",
            exit_code=EXIT_NETWORK_ERROR,
        )

    new_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    rotated = creds.model_copy(
        update={
            "access_token": new_access_token,
            "expires_at": new_expires_at,
        }
    )
    save_credentials(path, rotated)
    return rotated


def revoke_refresh_token(creds: CamCredentials) -> None:
    """Call Google's revoke endpoint against the stored refresh token.

    Per SRD FR-CRED-5, status 200 (revoked just now) and status 400 (already
    revoked / unknown token) are both acceptable terminal states — the
    operator's intent ("invalidate this token at Google's end") is honored
    in both cases. Any other status raises ``CredentialError`` mapped to
    exit 3.
    """
    status, _ = _post_form(GOOGLE_OAUTH_REVOKE_URL, {"token": creds.refresh_token})
    if status not in (200, 400):
        raise CredentialError(
            f"OAuth token revocation failed (HTTP {status})",
            exit_code=EXIT_NETWORK_ERROR,
            hint="Retry, or revoke manually at https://myaccount.google.com/permissions.",
        )

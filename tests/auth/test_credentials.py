"""Unit tests for ``nest_cli.auth.credentials``.

Coverage map (FR → test):

- FR-CRED-1 (atomic write, chmod 0600): test_save_then_load_roundtrip,
  test_save_sets_chmod_0600, test_save_atomic_replace_no_tempfile_left.
- FR-CRED-3 (extra=forbid + missing-key validation): test_load_rejects_extra_keys,
  test_load_rejects_missing_required_keys.
- FR-CRED-5 (revoke 200 / 400 OK): test_revoke_accepts_200,
  test_revoke_accepts_400, test_revoke_rejects_500.
- FR-CRED-6 (refresh-on-expiry): test_refresh_skipped_when_far_future,
  test_refresh_fires_when_expired, test_refresh_persists_new_token,
  test_refresh_force_overrides_window.
- FR-CRED-12 (chmod-0600 enforcement on read): test_load_rejects_loose_mode,
  test_enforce_chmod_passes_0600, test_enforce_chmod_rejects_0644.
- FR-CRED-13 (flock serialization): test_lock_blocks_second_writer.

We also exercise the not-found path (exit 2 mapping) and the
``XDG_CONFIG_HOME`` resolver.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nest_cli.auth import credentials as cred_mod
from nest_cli.auth.credentials import (
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_NETWORK_ERROR,
    GOOGLE_OAUTH_REVOKE_URL,
    GOOGLE_OAUTH_TOKEN_URL,
    CredentialError,
    _file_lock,
    default_credentials_path,
    default_token_cache_dir,
    enforce_credentials_chmod,
    load_credentials,
    refresh_access_token_if_needed,
    revoke_refresh_token,
    save_credentials,
)
from nest_cli.auth.types import CamCredentials

# ---------------------------------------------------------------------------
# HTTP mocking helper
# ---------------------------------------------------------------------------
#
# Production code uses stdlib ``urllib.request`` for the two POSTs (token
# refresh + revoke) — the ``responses`` library only patches ``requests``,
# so we patch our internal ``_post_form`` seam instead. Each test installs
# a stub that returns a canned (status, body) tuple per URL.


def _install_post_stub(
    monkeypatch: pytest.MonkeyPatch, responses_by_url: dict[str, tuple[int, bytes]]
) -> list[tuple[str, dict[str, str]]]:
    """Replace ``credentials._post_form`` with a deterministic stub.

    Returns a list that the test can inspect post-call to assert which
    URLs were hit and with which form params.
    """
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_post(url: str, params: dict[str, str]) -> tuple[int, bytes]:
        calls.append((url, params))
        if url not in responses_by_url:
            raise AssertionError(f"unexpected POST to {url}")
        return responses_by_url[url]

    monkeypatch.setattr(cred_mod, "_post_form", fake_post)
    return calls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_creds(
    *,
    expires_at: datetime | None = None,
    access_token: str = "test-access-token",
    refresh_token: str = "test-refresh-token",
) -> CamCredentials:
    """Build a valid ``CamCredentials`` instance for tests."""
    return CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id="test-project-123",
        oauth_client_id="test-client-id.apps.googleusercontent.com",
        oauth_client_secret="test-client-secret",
        refresh_token=refresh_token,
        access_token=access_token,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=1)),
    )


@pytest.fixture
def creds_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a writable credentials path under an isolated XDG home."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return default_credentials_path()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_default_credentials_path_honors_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``XDG_CONFIG_HOME`` overrides the default location."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom-xdg"))
    assert (
        default_credentials_path() == tmp_path / "custom-xdg" / "nest-cli" / "credentials-cam.json"
    )


def test_default_credentials_path_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``XDG_CONFIG_HOME`` we fall back to ``~/.config``."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_credentials_path() == tmp_path / ".config" / "nest-cli" / "credentials-cam.json"


def test_default_token_cache_dir_under_same_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``.tokens/`` lives next to the credentials file (FR-CRED-9)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cache = default_token_cache_dir()
    assert cache.name == ".tokens"
    assert cache.parent == default_credentials_path().parent


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_roundtrip(creds_path: Path) -> None:
    """A save followed by a load returns an equal model (FR-CRED-3)."""
    original = _make_creds()
    save_credentials(creds_path, original)
    loaded = load_credentials(creds_path)
    assert loaded.model_dump(mode="json") == original.model_dump(mode="json")


def test_save_sets_chmod_0600(creds_path: Path) -> None:
    """The saved file MUST be 0o600 regardless of the operator's umask (FR-CRED-1)."""
    save_credentials(creds_path, _make_creds())
    mode = stat.S_IMODE(creds_path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:03o}"


def test_save_creates_parent_dir_with_chmod_0700(creds_path: Path) -> None:
    """The parent dir holds chmod 0o700 (FR-CRED-9 mirror for the cred dir)."""
    save_credentials(creds_path, _make_creds())
    parent_mode = stat.S_IMODE(creds_path.parent.stat().st_mode)
    assert parent_mode == 0o700, f"expected 0o700 on parent, got 0o{parent_mode:03o}"


def test_save_atomic_replace_no_tempfile_left(creds_path: Path) -> None:
    """After a successful save, no ``.tmp`` sibling lingers in the dir."""
    save_credentials(creds_path, _make_creds())
    save_credentials(creds_path, _make_creds(access_token="rotated"))
    leftovers = [p for p in creds_path.parent.iterdir() if p.suffix == ".tmp" or ".tmp" in p.name]
    assert leftovers == [], f"unexpected tempfile residue: {leftovers}"


def test_save_overwrite_replaces_payload(creds_path: Path) -> None:
    """A second save fully replaces the first payload."""
    save_credentials(creds_path, _make_creds(access_token="first"))
    save_credentials(creds_path, _make_creds(access_token="second"))
    on_disk = json.loads(creds_path.read_text())
    assert on_disk["access_token"] == "second"


# ---------------------------------------------------------------------------
# load_credentials failure modes
# ---------------------------------------------------------------------------


def test_load_missing_file_exits_2(creds_path: Path) -> None:
    """File-not-found is FR-CRED-1 territory: exit 2 with auth-setup hint."""
    with pytest.raises(CredentialError) as exc:
        load_credentials(creds_path)
    assert exc.value.exit_code == EXIT_AUTH_ERROR
    assert "auth setup" in (exc.value.hint or "")


def test_load_rejects_extra_keys(creds_path: Path) -> None:
    """FR-CRED-3: unknown additional keys exit 6."""
    creds_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload: dict[str, Any] = _make_creds().model_dump(mode="json")
    payload["unexpected_extra_key"] = "boom"
    creds_path.write_text(json.dumps(payload))
    os.chmod(creds_path, 0o600)
    with pytest.raises(CredentialError) as exc:
        load_credentials(creds_path)
    assert exc.value.exit_code == EXIT_CONFIG_ERROR


def test_load_rejects_missing_required_keys(creds_path: Path) -> None:
    """FR-CRED-3: missing required field exits 6."""
    creds_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = _make_creds().model_dump(mode="json")
    del payload["refresh_token"]
    creds_path.write_text(json.dumps(payload))
    os.chmod(creds_path, 0o600)
    with pytest.raises(CredentialError) as exc:
        load_credentials(creds_path)
    assert exc.value.exit_code == EXIT_CONFIG_ERROR


def test_load_rejects_invalid_json(creds_path: Path) -> None:
    """Garbage JSON exits 6 (config) — distinct from missing-file exit 2."""
    creds_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    creds_path.write_text("{not valid json")
    os.chmod(creds_path, 0o600)
    with pytest.raises(CredentialError) as exc:
        load_credentials(creds_path)
    assert exc.value.exit_code == EXIT_CONFIG_ERROR


def test_load_rejects_loose_mode(creds_path: Path) -> None:
    """FR-CRED-12: a 0o644 file refuses to load and exits 2."""
    save_credentials(creds_path, _make_creds())
    os.chmod(creds_path, 0o644)
    with pytest.raises(CredentialError) as exc:
        load_credentials(creds_path)
    assert exc.value.exit_code == EXIT_AUTH_ERROR
    assert "permissive" in str(exc.value)


# ---------------------------------------------------------------------------
# enforce_credentials_chmod
# ---------------------------------------------------------------------------


def test_enforce_chmod_passes_0600(creds_path: Path) -> None:
    """0o600 is the canonical state and must not raise."""
    save_credentials(creds_path, _make_creds())
    enforce_credentials_chmod(creds_path)  # no raise


def test_enforce_chmod_rejects_0644(creds_path: Path) -> None:
    """0o644 leaks to group + other; FR-CRED-12 says exit 2."""
    save_credentials(creds_path, _make_creds())
    os.chmod(creds_path, 0o644)
    with pytest.raises(CredentialError) as exc:
        enforce_credentials_chmod(creds_path)
    assert exc.value.exit_code == EXIT_AUTH_ERROR


def test_enforce_chmod_rejects_0640(creds_path: Path) -> None:
    """0o640 leaks to group only — still rejected."""
    save_credentials(creds_path, _make_creds())
    os.chmod(creds_path, 0o640)
    with pytest.raises(CredentialError):
        enforce_credentials_chmod(creds_path)


# ---------------------------------------------------------------------------
# Refresh-on-expiry (FR-CRED-6)
# ---------------------------------------------------------------------------


def test_refresh_skipped_when_far_future(creds_path: Path) -> None:
    """If expires_at is far enough in the future, no HTTP call fires."""
    creds = _make_creds(expires_at=datetime.now(UTC) + timedelta(hours=1))
    save_credentials(creds_path, creds)
    # ``responses`` not activated — any HTTP call would NetworkError on us.
    same = refresh_access_token_if_needed(creds, creds_path)
    assert same.access_token == creds.access_token


def test_refresh_fires_when_expired(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If expires_at is past, we POST to Google's token endpoint and rotate."""
    expired = _make_creds(
        expires_at=datetime.now(UTC) - timedelta(seconds=10),
        access_token="old-access-token",
    )
    save_credentials(creds_path, expired)
    body = json.dumps(
        {"access_token": "new-access-token", "expires_in": 3600, "token_type": "Bearer"}
    ).encode()
    calls = _install_post_stub(monkeypatch, {GOOGLE_OAUTH_TOKEN_URL: (200, body)})
    rotated = refresh_access_token_if_needed(expired, creds_path)
    assert rotated.access_token == "new-access-token"
    assert rotated.refresh_token == expired.refresh_token  # refresh token unchanged
    assert calls[0][0] == GOOGLE_OAUTH_TOKEN_URL
    assert calls[0][1]["grant_type"] == "refresh_token"
    assert calls[0][1]["refresh_token"] == expired.refresh_token


def test_refresh_persists_new_token(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rotated token is written back to disk (FR-CRED-6 atomic write)."""
    expired = _make_creds(
        expires_at=datetime.now(UTC) - timedelta(seconds=10),
        access_token="old",
    )
    save_credentials(creds_path, expired)
    body = json.dumps(
        {"access_token": "fresh", "expires_in": 1800, "token_type": "Bearer"}
    ).encode()
    _install_post_stub(monkeypatch, {GOOGLE_OAUTH_TOKEN_URL: (200, body)})
    refresh_access_token_if_needed(expired, creds_path)
    on_disk = json.loads(creds_path.read_text())
    assert on_disk["access_token"] == "fresh"


def test_refresh_force_overrides_window(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`force=True` rotates even when the token is far from expiry (FR-CRED-4)."""
    fresh = _make_creds(expires_at=datetime.now(UTC) + timedelta(hours=1))
    save_credentials(creds_path, fresh)
    body = json.dumps(
        {"access_token": "forcibly-rotated", "expires_in": 3600, "token_type": "Bearer"}
    ).encode()
    _install_post_stub(monkeypatch, {GOOGLE_OAUTH_TOKEN_URL: (200, body)})
    rotated = refresh_access_token_if_needed(fresh, creds_path, force=True)
    assert rotated.access_token == "forcibly-rotated"


def test_refresh_4xx_exits_2(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Google rejecting the refresh token is FR-18 territory: exit 2."""
    expired = _make_creds(expires_at=datetime.now(UTC) - timedelta(seconds=10))
    save_credentials(creds_path, expired)
    body = json.dumps({"error": "invalid_grant"}).encode()
    _install_post_stub(monkeypatch, {GOOGLE_OAUTH_TOKEN_URL: (400, body)})
    with pytest.raises(CredentialError) as exc:
        refresh_access_token_if_needed(expired, creds_path)
    assert exc.value.exit_code == EXIT_AUTH_ERROR


def test_refresh_malformed_response_exits_3(
    creds_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 with missing fields is a network-shape failure (exit 3)."""
    expired = _make_creds(expires_at=datetime.now(UTC) - timedelta(seconds=10))
    save_credentials(creds_path, expired)
    body = json.dumps({"unexpected": "shape"}).encode()
    _install_post_stub(monkeypatch, {GOOGLE_OAUTH_TOKEN_URL: (200, body)})
    with pytest.raises(CredentialError) as exc:
        refresh_access_token_if_needed(expired, creds_path)
    assert exc.value.exit_code == EXIT_NETWORK_ERROR


# ---------------------------------------------------------------------------
# Revoke (FR-CRED-5)
# ---------------------------------------------------------------------------


def test_revoke_accepts_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status 200 is the canonical 'just revoked' response."""
    _install_post_stub(monkeypatch, {GOOGLE_OAUTH_REVOKE_URL: (200, b"")})
    revoke_refresh_token(_make_creds())  # no raise


def test_revoke_accepts_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status 400 (already revoked / unknown token) is also a terminal-OK state."""
    body = json.dumps({"error": "invalid_token"}).encode()
    _install_post_stub(monkeypatch, {GOOGLE_OAUTH_REVOKE_URL: (400, body)})
    revoke_refresh_token(_make_creds())  # no raise


def test_revoke_rejects_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server errors raise; the operator can retry."""
    _install_post_stub(monkeypatch, {GOOGLE_OAUTH_REVOKE_URL: (500, b"")})
    with pytest.raises(CredentialError) as exc:
        revoke_refresh_token(_make_creds())
    assert exc.value.exit_code == EXIT_NETWORK_ERROR


# ---------------------------------------------------------------------------
# flock serialization (FR-CRED-13)
# ---------------------------------------------------------------------------


def test_lock_blocks_second_writer(creds_path: Path) -> None:
    """A second ``_file_lock`` cannot acquire while the first holds it."""
    creds_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    second_acquired = threading.Event()
    first_release = threading.Event()

    def hold_lock_first() -> None:
        with _file_lock(creds_path, timeout_s=5):
            first_release.wait(timeout=2)

    def try_acquire_second() -> None:
        # Tiny timeout so this returns quickly when blocked.
        try:
            with _file_lock(creds_path, timeout_s=1):
                second_acquired.set()
        except CredentialError:
            pass  # expected — first holder has the lock

    t1 = threading.Thread(target=hold_lock_first)
    t1.start()
    time.sleep(0.1)  # let t1 acquire
    t2 = threading.Thread(target=try_acquire_second)
    t2.start()
    t2.join(timeout=3)
    # While t1 still holds, t2 must NOT have set its flag.
    assert not second_acquired.is_set(), "second writer acquired despite first lock"
    first_release.set()
    t1.join(timeout=2)


def test_lock_timeout_maps_to_exit_3(creds_path: Path) -> None:
    """FR-CRED-13: lock-acquisition timeout is a network-class failure (exit 3)."""
    creds_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    holder_release = threading.Event()
    holder_started = threading.Event()

    def holder() -> None:
        with _file_lock(creds_path, timeout_s=5):
            holder_started.set()
            holder_release.wait(timeout=3)

    t = threading.Thread(target=holder)
    t.start()
    holder_started.wait(timeout=2)
    try:
        with pytest.raises(CredentialError) as exc, _file_lock(creds_path, timeout_s=1):
            pass
        assert exc.value.exit_code == EXIT_NETWORK_ERROR
    finally:
        holder_release.set()
        t.join(timeout=2)


def test_lock_refuses_symlink_at_lock_path(creds_path: Path, tmp_path: Path) -> None:
    """A pre-existing symlink at ``<path>.lock`` is a credential-substitution
    attack vector and must be rejected with a structured auth error.

    The hardened ``_file_lock`` opens with ``O_NOFOLLOW`` on both the
    create and the existing-file paths so an attacker cannot redirect
    the lock to a file they control.
    """
    creds_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = creds_path.with_suffix(creds_path.suffix + ".lock")
    decoy = tmp_path / "decoy"
    decoy.write_text("attacker-controlled")
    lock_path.symlink_to(decoy)

    with pytest.raises(CredentialError) as exc_info, _file_lock(creds_path, timeout_s=1):
        pass
    assert exc_info.value.exit_code == EXIT_AUTH_ERROR
    # Decoy must remain untouched — we refused before ever opening it.
    assert decoy.read_text() == "attacker-controlled"

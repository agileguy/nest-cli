"""Tests for ``nest_cli.auth.wifi_credentials``.

Coverage map (SRD FR → test):

- FR-CRED-7 (atomic write, chmod 0600):    test_save_then_load_roundtrip,
  test_save_sets_chmod_0600, test_save_atomic_replace_no_tempfile_left.
- FR-CRED-8 (extra=forbid + missing keys): test_load_rejects_extra_keys,
  test_load_rejects_missing_required_keys.
- FR-CRED-9 (revoke = atomic empty-stub):  test_revoke_replaces_with_empty_stub,
  test_revoke_preserves_chmod_0600.
- FR-CRED-12 (chmod-0600 enforcement):     test_load_rejects_loose_mode.
- FR-CRED-13 (flock serialization):        test_save_uses_lock_file.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nest_cli.auth.wifi_credentials import (
    WIFI_REVOCATION_REMINDER,
    WifiCredentialError,
    default_wifi_credentials_path,
    load_wifi_credentials,
    revoke_wifi_credentials,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.errors import EXIT_AUTH_ERROR, EXIT_CONFIG_ERROR


def _make_creds(
    *,
    email: str = "operator@example.com",
    master_token: str = "android-master-token-xyz",
) -> WifiCredentials:
    return WifiCredentials(
        version=1,
        type="foyer",
        google_account_email=email,
        master_token=master_token,
        issued_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Path resolver
# ---------------------------------------------------------------------------


class TestDefaultPath:
    def test_honors_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        path = default_wifi_credentials_path()
        assert path == tmp_path / "xdg" / "nest-cli" / "credentials-wifi.json"

    def test_falls_back_to_dotconfig(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type,return-value]
        path = default_wifi_credentials_path()
        assert path == tmp_path / ".config" / "nest-cli" / "credentials-wifi.json"


# ---------------------------------------------------------------------------
# Save / load roundtrip + chmod
# ---------------------------------------------------------------------------


class TestSaveLoadRoundtrip:
    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        creds = _make_creds()
        save_wifi_credentials(path, creds)
        loaded = load_wifi_credentials(path)
        assert loaded == creds

    def test_save_sets_chmod_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        save_wifi_credentials(path, _make_creds())
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_save_atomic_replace_no_tempfile_left(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        save_wifi_credentials(path, _make_creds())
        # The dir should contain the credentials file plus its lock — but
        # no tempfile (atomic-rename cleared it).
        contents = sorted(p.name for p in tmp_path.iterdir())
        assert path.name in contents
        for name in contents:
            assert not name.endswith(".tmp")

    def test_save_replaces_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        save_wifi_credentials(path, _make_creds(email="old@example.com"))
        save_wifi_credentials(path, _make_creds(email="new@example.com"))
        loaded = load_wifi_credentials(path)
        assert loaded.google_account_email == "new@example.com"


# ---------------------------------------------------------------------------
# chmod enforcement (FR-CRED-12)
# ---------------------------------------------------------------------------


class TestChmodEnforcement:
    def test_load_rejects_loose_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        save_wifi_credentials(path, _make_creds())
        os.chmod(path, 0o644)  # group + other readable
        with pytest.raises(WifiCredentialError) as exc_info:
            load_wifi_credentials(path)
        assert exc_info.value.exit_code == EXIT_AUTH_ERROR


# ---------------------------------------------------------------------------
# Schema validation (FR-CRED-8 — extra=forbid + missing keys)
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_load_rejects_extra_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "type": "foyer",
                    "google_account_email": "x@y.com",
                    "master_token": "t",
                    "issued_at": "2026-05-03T12:00:00Z",
                    "surprise_field": "boom",
                }
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        with pytest.raises(WifiCredentialError) as exc_info:
            load_wifi_credentials(path)
        assert exc_info.value.exit_code == EXIT_CONFIG_ERROR

    def test_load_rejects_missing_required_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        path.write_text(
            json.dumps({"version": 1, "type": "foyer"}),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        with pytest.raises(WifiCredentialError) as exc_info:
            load_wifi_credentials(path)
        assert exc_info.value.exit_code == EXIT_CONFIG_ERROR

    def test_load_rejects_wrong_type_field(self, tmp_path: Path) -> None:
        # FR-CRED-8: ``type`` is pinned to ``foyer`` (regex anchor).
        path = tmp_path / "creds-wifi.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "type": "oauth",
                    "google_account_email": "x@y.com",
                    "master_token": "t",
                    "issued_at": "2026-05-03T12:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        with pytest.raises(WifiCredentialError) as exc_info:
            load_wifi_credentials(path)
        assert exc_info.value.exit_code == EXIT_CONFIG_ERROR


# ---------------------------------------------------------------------------
# Missing-file path (FR-CRED-7 hint)
# ---------------------------------------------------------------------------


class TestMissingFile:
    def test_load_missing_file_raises_with_setup_hint(self, tmp_path: Path) -> None:
        with pytest.raises(WifiCredentialError) as exc_info:
            load_wifi_credentials(tmp_path / "nonexistent.json")
        err = exc_info.value
        assert err.exit_code == EXIT_AUTH_ERROR
        assert "auth wifi-setup" in (err.hint or "")


# ---------------------------------------------------------------------------
# Revocation (FR-CRED-9)
# ---------------------------------------------------------------------------


class TestRevoke:
    def test_revoke_replaces_with_empty_stub(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        save_wifi_credentials(path, _make_creds())
        revoke_wifi_credentials(path)
        # The empty stub deliberately fails schema validation; we read
        # the raw JSON to confirm.
        raw = path.read_text(encoding="utf-8").strip()
        assert raw == "{}"

    def test_revoke_preserves_chmod_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "creds-wifi.json"
        save_wifi_credentials(path, _make_creds())
        revoke_wifi_credentials(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_revocation_reminder_mentions_permissions_url(self) -> None:
        # The CLI layer is responsible for emitting this on stderr; the
        # constant is defined here so the verb pulls it directly.
        assert "myaccount.google.com/permissions" in WIFI_REVOCATION_REMINDER


# ---------------------------------------------------------------------------
# flock serialization (FR-CRED-13) — sidecar exists during write
# ---------------------------------------------------------------------------


class TestLocking:
    def test_save_uses_lock_file(self, tmp_path: Path) -> None:
        # We don't try to race two savers (POSIX flock + OS scheduling
        # makes the race deterministic); we verify the lock-file sidecar
        # is created in the parent dir, which is the externally-visible
        # shape of the FR-CRED-13 contract.
        path = tmp_path / "creds-wifi.json"
        save_wifi_credentials(path, _make_creds())
        lock_sidecar = path.with_suffix(path.suffix + ".lock")
        assert lock_sidecar.exists()

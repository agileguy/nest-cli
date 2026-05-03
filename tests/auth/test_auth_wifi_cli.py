"""Click ``CliRunner`` tests for the ``auth wifi-setup`` / ``auth wifi-revoke``
verbs and the wifi entry of ``auth status``.

Coverage map (FR → test):

- FR-CRED-7 (master token sources): test_wifi_setup_stdin_path,
  test_wifi_setup_master_token_file_path, test_wifi_setup_env_var_path.
- FR-CRED-7 (refuse clobber):       test_wifi_setup_refuses_overwrite.
- FR-CRED-9 (atomic stub + reminder): test_wifi_revoke_writes_empty_stub,
  test_wifi_revoke_emits_permissions_reminder.
- FR-CRED-10 (status array):        test_status_emits_both_families.
- FR-WIFI-0 (experimental gate):    test_wifi_setup_requires_experimental_flag,
  test_wifi_revoke_requires_experimental_flag.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nest_cli.auth.credentials import default_credentials_path, save_credentials
from nest_cli.auth.types import CamCredentials
from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.auth_cmd import auth_group

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both default credential paths at a writable tmp dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _make_wifi_creds() -> WifiCredentials:
    return WifiCredentials(
        version=2,
        type="foyer",
        google_account_email="operator@example.com",
        master_token="android-master-token-xyz",
        android_id="0123456789abcdef",
        issued_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
    )


def _make_cam_creds() -> CamCredentials:
    return CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id="proj",
        oauth_client_id="abcdefgh12345678.apps.googleusercontent.com",
        oauth_client_secret="secret-on-disk",  # noqa: S106 - fixture
        refresh_token="refresh-on-disk",  # noqa: S106 - fixture
        access_token="access-token-on-disk",  # noqa: S106 - fixture
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# FR-WIFI-0 — experimental gate on auth wifi-setup / wifi-revoke
# ---------------------------------------------------------------------------


class TestExperimentalGate:
    def test_wifi_setup_requires_experimental_flag(self, isolated_xdg: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(auth_group, ["wifi-setup"])
        assert result.exit_code == 64, result.output
        # Hint references SRD §3.2.3 (the experimental-flag rationale).
        assert "experimental" in (result.stderr or result.output).lower()

    def test_wifi_revoke_requires_experimental_flag(self, isolated_xdg: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(auth_group, ["wifi-revoke"])
        assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# auth wifi-setup
# ---------------------------------------------------------------------------


_TEST_ANDROID_ID = "0123456789abcdef"


class TestWifiSetup:
    def test_stdin_path_persists_credentials_chmod_0600(self, isolated_xdg: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            ["wifi-setup", "--experimental-wifi", "--android-id", _TEST_ANDROID_ID],
            # email prompt + master-token stdin (hidden).
            input="me@example.com\nthe-master-token\n",
        )
        assert result.exit_code == 0, result.output

        path = default_wifi_credentials_path()
        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600
        on_disk = json.loads(path.read_text())
        assert on_disk["type"] == "foyer"
        assert on_disk["version"] == 2
        assert on_disk["google_account_email"] == "me@example.com"
        assert on_disk["master_token"] == "the-master-token"
        assert on_disk["android_id"] == _TEST_ANDROID_ID

    def test_master_token_file_path(self, isolated_xdg: Path, tmp_path: Path) -> None:
        token_file = tmp_path / "token.txt"
        token_file.write_text("token-from-file\n", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-setup",
                "--experimental-wifi",
                "--master-token-file",
                str(token_file),
                "--google-account-email",
                "me@example.com",
                "--android-id",
                _TEST_ANDROID_ID,
            ],
        )
        assert result.exit_code == 0, result.output
        on_disk = json.loads(default_wifi_credentials_path().read_text())
        assert on_disk["master_token"] == "token-from-file"
        assert on_disk["android_id"] == _TEST_ANDROID_ID

    def test_env_var_path(self, isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_ANDROID_MASTER_TOKEN", "token-from-env")
        monkeypatch.setenv("GOOGLE_ANDROID_ID", _TEST_ANDROID_ID)
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-setup",
                "--experimental-wifi",
                "--google-account-email",
                "me@example.com",
            ],
        )
        assert result.exit_code == 0, result.output
        on_disk = json.loads(default_wifi_credentials_path().read_text())
        assert on_disk["master_token"] == "token-from-env"
        assert on_disk["android_id"] == _TEST_ANDROID_ID

    def test_refuses_overwrite_without_flag(self, isolated_xdg: Path) -> None:
        path = default_wifi_credentials_path()
        save_wifi_credentials(path, _make_wifi_creds())

        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            ["wifi-setup", "--experimental-wifi", "--android-id", _TEST_ANDROID_ID],
            input="me@example.com\nnew-token\n",
        )
        assert result.exit_code == 2, result.output
        assert "overwrite" in (result.stderr or result.output).lower()

    def test_overwrite_flag_succeeds(self, isolated_xdg: Path) -> None:
        path = default_wifi_credentials_path()
        save_wifi_credentials(path, _make_wifi_creds())

        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-setup",
                "--experimental-wifi",
                "--overwrite",
                "--android-id",
                _TEST_ANDROID_ID,
            ],
            input="me2@example.com\nnew-token\n",
        )
        assert result.exit_code == 0, result.output
        on_disk = json.loads(path.read_text())
        assert on_disk["google_account_email"] == "me2@example.com"
        assert on_disk["master_token"] == "new-token"
        assert on_disk["android_id"] == _TEST_ANDROID_ID

    def test_invalid_android_id_exits_6(self, isolated_xdg: Path) -> None:
        """Non-hex / wrong-length --android-id surfaces as EXIT_CONFIG_ERROR."""
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-setup",
                "--experimental-wifi",
                "--android-id",
                "not-hex-not-16chars",
            ],
            input="me@example.com\nthe-master-token\n",
        )
        assert result.exit_code == 6, result.output
        msg = (result.stderr or result.output).lower()
        assert "android_id" in msg

    def test_short_android_id_exits_6(self, isolated_xdg: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            ["wifi-setup", "--experimental-wifi", "--android-id", "0123"],
            input="me@example.com\nthe-master-token\n",
        )
        assert result.exit_code == 6, result.output


# ---------------------------------------------------------------------------
# auth wifi-revoke
# ---------------------------------------------------------------------------


class TestWifiRevoke:
    def test_writes_empty_stub_atomically(self, isolated_xdg: Path) -> None:
        path = default_wifi_credentials_path()
        save_wifi_credentials(path, _make_wifi_creds())

        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            ["wifi-revoke", "--experimental-wifi", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert path.read_text(encoding="utf-8").strip() == "{}"
        # FR-CRED-12 — file mode preserved.
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_emits_permissions_reminder_on_stderr(self, isolated_xdg: Path) -> None:
        path = default_wifi_credentials_path()
        save_wifi_credentials(path, _make_wifi_creds())

        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            ["wifi-revoke", "--experimental-wifi", "--yes"],
        )
        assert result.exit_code == 0
        # Click 8.3+ separates stderr by default. The reminder names the
        # Google permissions URL (§6.4).
        assert "myaccount.google.com/permissions" in (result.stderr or result.output)

    def test_no_existing_creds_is_a_clean_noop(self, isolated_xdg: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            ["wifi-revoke", "--experimental-wifi", "--yes"],
        )
        # No file to scrub; idempotent success.
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# auth status — wifi entry (FR-CRED-10)
# ---------------------------------------------------------------------------


class TestStatusWifiEntry:
    def test_emits_both_families_when_both_configured(self, isolated_xdg: Path) -> None:
        save_credentials(default_credentials_path(), _make_cam_creds())
        save_wifi_credentials(default_wifi_credentials_path(), _make_wifi_creds())

        runner = CliRunner()
        result = runner.invoke(auth_group, ["status", "--output", "json"])
        assert result.exit_code == 0, result.output

        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert len(payload) == 2
        cam_record = next(r for r in payload if r["family"] == "cam")
        wifi_record = next(r for r in payload if r["family"] == "wifi")
        assert cam_record["configured"] is True
        assert wifi_record["configured"] is True
        assert wifi_record["google_account_email"] == "operator@example.com"
        assert wifi_record["issued_at"] == "2026-05-03T12:00:00Z"
        # SRD §6.7: master token is NEVER emitted.
        assert "master_token" not in result.output
        assert "android-master-token-xyz" not in result.output

    def test_wifi_entry_when_only_cam_configured(self, isolated_xdg: Path) -> None:
        save_credentials(default_credentials_path(), _make_cam_creds())

        runner = CliRunner()
        result = runner.invoke(auth_group, ["status", "--output", "json"])
        assert result.exit_code == 0, result.output

        payload = json.loads(result.output)
        wifi_record = next(r for r in payload if r["family"] == "wifi")
        assert wifi_record["configured"] is False

    def test_wifi_entry_distinguishes_revoked_from_never_set_up(self, isolated_xdg: Path) -> None:
        # Set up then atomically scrub — leaves the empty stub.
        path = default_wifi_credentials_path()
        save_wifi_credentials(path, _make_wifi_creds())
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o600)

        runner = CliRunner()
        result = runner.invoke(auth_group, ["status", "--output", "json"])
        assert result.exit_code == 0, result.output

        payload = json.loads(result.output)
        wifi_record = next(r for r in payload if r["family"] == "wifi")
        assert wifi_record["configured"] is False
        assert "revoked" in wifi_record.get("note", "")

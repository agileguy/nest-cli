"""E2E CliRunner tests for ``nest-cli auth wifi-setup`` (FR-CRED-7/8).

Coverage:

- stdin path: prompts for email + master token (hidden) → persists v2 record.
- --master-token-file path → reads token, persists v2 record.
- GOOGLE_ANDROID_MASTER_TOKEN env var path → persists v2 record.
- Refuses to overwrite existing creds without --overwrite (exit 2).
- --overwrite clobbers existing creds.
- --android-id wrong format → exit 6 family=wifi.
- --android-id wrong length → exit 6 family=wifi.
- Empty --master-token-file → exit 6 family=wifi.
- Missing --experimental-wifi → exit 64.
- File written with chmod 0600.
- File contains version=2, type="foyer", correct fields.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.auth_cmd import auth_group

SeedV2 = Any

_VALID_ANDROID_ID = "0123456789abcdef"


def test_wifi_setup_stdin_path_persists_v2_record(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        auth_group,
        ["wifi-setup", "--experimental-wifi", "--android-id", _VALID_ANDROID_ID],
        input="me@example.com\nthe-master-token\n",
    )
    assert result.exit_code == 0, result.output
    path = default_wifi_credentials_path()
    on_disk = json.loads(path.read_text())
    assert on_disk["type"] == "foyer"
    assert on_disk["version"] == 2
    assert on_disk["google_account_email"] == "me@example.com"
    assert on_disk["master_token"] == "the-master-token"
    assert on_disk["android_id"] == _VALID_ANDROID_ID


def test_wifi_setup_persists_with_chmod_0600(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        auth_group,
        ["wifi-setup", "--experimental-wifi", "--android-id", _VALID_ANDROID_ID],
        input="me@example.com\nthe-master-token\n",
    )
    assert result.exit_code == 0, result.output
    path = default_wifi_credentials_path()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_wifi_setup_master_token_file_path(
    isolated_xdg: Path, tmp_path: Path, runner: CliRunner
) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("token-from-file\n", encoding="utf-8")
    result = runner.invoke(
        auth_group,
        [
            "wifi-setup",
            "--experimental-wifi",
            "--master-token-file",
            str(token_file),
            "--google-account-email",
            "file@example.com",
            "--android-id",
            _VALID_ANDROID_ID,
        ],
    )
    assert result.exit_code == 0, result.output
    on_disk = json.loads(default_wifi_credentials_path().read_text())
    assert on_disk["master_token"] == "token-from-file"
    assert on_disk["google_account_email"] == "file@example.com"


def test_wifi_setup_env_var_path(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.setenv("GOOGLE_ANDROID_MASTER_TOKEN", "token-from-env")
    monkeypatch.setenv("GOOGLE_ANDROID_ID", _VALID_ANDROID_ID)
    result = runner.invoke(
        auth_group,
        [
            "wifi-setup",
            "--experimental-wifi",
            "--google-account-email",
            "env@example.com",
        ],
    )
    assert result.exit_code == 0, result.output
    on_disk = json.loads(default_wifi_credentials_path().read_text())
    assert on_disk["master_token"] == "token-from-env"
    assert on_disk["android_id"] == _VALID_ANDROID_ID


def test_wifi_setup_refuses_overwrite_without_flag(isolated_xdg: Path, runner: CliRunner) -> None:
    """Existing credentials → exit 2 without --overwrite."""
    seeded = WifiCredentials(
        version=2,
        type="foyer",
        google_account_email="existing@example.com",
        master_token="aas_et/existing",  # noqa: S106 - test fixture
        android_id=_VALID_ANDROID_ID,
        issued_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    save_wifi_credentials(default_wifi_credentials_path(), seeded)

    result = runner.invoke(
        auth_group,
        ["wifi-setup", "--experimental-wifi", "--android-id", _VALID_ANDROID_ID],
        input="me@example.com\nnew-token\n",
    )
    assert result.exit_code == 2, result.output
    assert "overwrite" in (result.stderr or result.output).lower()


def test_wifi_setup_overwrite_flag_succeeds(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        auth_group,
        [
            "wifi-setup",
            "--experimental-wifi",
            "--overwrite",
            "--android-id",
            _VALID_ANDROID_ID,
        ],
        input="new@example.com\nnew-token\n",
    )
    assert result.exit_code == 0, result.output
    on_disk = json.loads(default_wifi_credentials_path().read_text())
    assert on_disk["google_account_email"] == "new@example.com"
    assert on_disk["master_token"] == "new-token"


def test_wifi_setup_invalid_android_id_format_exits_6(
    isolated_xdg: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        auth_group,
        ["wifi-setup", "--experimental-wifi", "--android-id", "not-hex-not-16char"],
        input="me@example.com\nthe-master-token\n",
    )
    assert result.exit_code == 6, result.output
    err = (result.stderr or result.output).lower()
    assert "android_id" in err


def test_wifi_setup_short_android_id_exits_6(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        auth_group,
        ["wifi-setup", "--experimental-wifi", "--android-id", "0123"],
        input="me@example.com\nthe-master-token\n",
    )
    assert result.exit_code == 6, result.output


def test_wifi_setup_empty_master_token_file_exits_6(
    isolated_xdg: Path, tmp_path: Path, runner: CliRunner
) -> None:
    """Empty file is a misconfigured input — exit 6 not exit 2."""
    token_file = tmp_path / "empty.txt"
    token_file.write_text("", encoding="utf-8")
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
            _VALID_ANDROID_ID,
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 6, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_wifi_setup_without_experimental_exits_64(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(auth_group, ["wifi-setup"])
    assert result.exit_code == 64, result.output
    assert "experimental" in (result.stderr or result.output).lower()

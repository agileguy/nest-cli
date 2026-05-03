"""Tests for ``auth wifi-refresh-bootstrap`` (Phase C, 2026-05-03).

Coverage map:

- Happy path with --refresh-token flag → upgrades v2 to v3.
- Happy path with GOOGLE_REFRESH_TOKEN env var fallback.
- Bad-format token → exit 6 with family=wifi (validates ^1//[\\w-]+$).
- Missing v2 credentials → exit 6 with hint pointing at wifi-setup.
- Re-bootstrap on existing v3 → overwrites refresh_token, preserves
  master_token + android_id + email + issued_at.
- Experimental gate fires before any token resolution.
- ``auth status --output json`` reports schema_version + refresh_token_present.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    load_wifi_credentials,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.auth_cmd import auth_group
from nest_cli.errors import EXIT_CONFIG_ERROR


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_v2_creds() -> WifiCredentials:
    creds = WifiCredentials(
        version=2,
        type="foyer",
        google_account_email="operator@example.com",
        master_token="aas_et/master-token-abc",
        android_id="0123456789abcdef",
        issued_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
    )
    save_wifi_credentials(default_wifi_credentials_path(), creds)
    return creds


# ---------------------------------------------------------------------------
# Experimental gate
# ---------------------------------------------------------------------------


class TestExperimentalGate:
    def test_bootstrap_without_experimental_flag_exits_64(self, isolated_xdg: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            ["wifi-refresh-bootstrap", "--refresh-token", "1//09abc-DEF"],
        )
        assert result.exit_code == 64, result.output
        err = result.stderr or result.output
        assert "experimental" in err.lower()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestBootstrapHappyPath:
    def test_bootstrap_with_flag_upgrades_v2_to_v3(self, isolated_xdg: Path) -> None:
        _seed_v2_creds()
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-refresh-bootstrap",
                "--experimental-wifi",
                "--refresh-token",
                "1//09abc-DEF_xyz123",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["version"] == 3
        assert payload["refresh_token_present"] is True
        loaded = load_wifi_credentials(default_wifi_credentials_path())
        assert loaded.version == 3
        assert loaded.refresh_token == "1//09abc-DEF_xyz123"

    def test_bootstrap_preserves_existing_master_token_and_email(self, isolated_xdg: Path) -> None:
        seeded = _seed_v2_creds()
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-refresh-bootstrap",
                "--experimental-wifi",
                "--refresh-token",
                "1//09abc-DEF",
            ],
        )
        assert result.exit_code == 0, result.output
        loaded = load_wifi_credentials(default_wifi_credentials_path())
        assert loaded.master_token == seeded.master_token
        assert loaded.android_id == seeded.android_id
        assert loaded.google_account_email == seeded.google_account_email
        assert loaded.issued_at == seeded.issued_at

    def test_bootstrap_with_env_var_fallback(
        self, isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_v2_creds()
        monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "1//09env-token")
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            ["wifi-refresh-bootstrap", "--experimental-wifi", "--output", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["refresh_token_present"] is True
        loaded = load_wifi_credentials(default_wifi_credentials_path())
        assert loaded.refresh_token == "1//09env-token"

    def test_bootstrap_overwrites_existing_v3_refresh_token(self, isolated_xdg: Path) -> None:
        # Start at v3 with one token.
        seeded = WifiCredentials(
            version=3,
            type="foyer",
            google_account_email="operator@example.com",
            master_token="aas_et/old",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
            refresh_token="1//09old-token",
        )
        save_wifi_credentials(default_wifi_credentials_path(), seeded)
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-refresh-bootstrap",
                "--experimental-wifi",
                "--refresh-token",
                "1//09new-token",
            ],
        )
        assert result.exit_code == 0, result.output
        loaded = load_wifi_credentials(default_wifi_credentials_path())
        assert loaded.refresh_token == "1//09new-token"
        # master_token preserved
        assert loaded.master_token == seeded.master_token


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestBootstrapErrors:
    def test_bootstrap_rejects_bad_format_token(self, isolated_xdg: Path) -> None:
        _seed_v2_creds()
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-refresh-bootstrap",
                "--experimental-wifi",
                "--refresh-token",
                "ya29.not-a-refresh-token",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == EXIT_CONFIG_ERROR, result.output
        payload = json.loads(result.stderr or result.output)
        assert payload["family"] == "wifi"
        assert "refresh_token" in payload["message"]

    def test_bootstrap_rejects_when_no_v2_credentials(self, isolated_xdg: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            auth_group,
            [
                "wifi-refresh-bootstrap",
                "--experimental-wifi",
                "--refresh-token",
                "1//09abc-DEF",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == EXIT_CONFIG_ERROR, result.output
        payload = json.loads(result.stderr or result.output)
        assert payload["family"] == "wifi"
        assert "wifi-setup" in (payload.get("hint") or "")


# ---------------------------------------------------------------------------
# auth status reports refresh_token presence
# ---------------------------------------------------------------------------


class TestAuthStatusReportsRefreshToken:
    def test_status_v2_reports_refresh_token_absent(self, isolated_xdg: Path) -> None:
        _seed_v2_creds()
        runner = CliRunner()
        result = runner.invoke(auth_group, ["status", "--output", "json"])
        assert result.exit_code == 0, result.output
        records = json.loads(result.output)
        wifi = next(r for r in records if r["family"] == "wifi")
        assert wifi["configured"] is True
        assert wifi["schema_version"] == 2
        assert wifi["refresh_token_present"] is False

    def test_status_v3_reports_refresh_token_present(self, isolated_xdg: Path) -> None:
        _seed_v2_creds()
        runner = CliRunner()
        bootstrap = runner.invoke(
            auth_group,
            [
                "wifi-refresh-bootstrap",
                "--experimental-wifi",
                "--refresh-token",
                "1//09abc-DEF",
            ],
        )
        assert bootstrap.exit_code == 0, bootstrap.output
        result = runner.invoke(auth_group, ["status", "--output", "json"])
        assert result.exit_code == 0, result.output
        records = json.loads(result.output)
        wifi = next(r for r in records if r["family"] == "wifi")
        assert wifi["schema_version"] == 3
        assert wifi["refresh_token_present"] is True

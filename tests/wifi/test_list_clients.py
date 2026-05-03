"""CliRunner tests for ``nest-cli wifi list clients`` (FR-WIFI-3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.wifi_cmd import wifi_group


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_wifi_creds() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=1,
            type="foyer",
            google_account_email="me@example.com",
            master_token="t",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
        ),
    )


def test_happy_path_emits_four_clients(isolated_xdg: Path, fake_googlewifi: type) -> None:
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "list",
            "clients",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 4
    ids = sorted(c["id"] for c in payload)
    assert ids == ["sta-kid-tablet", "sta-laptop", "sta-nas-wired", "sta-phone"]


def test_band_field_normalized(isolated_xdg: Path, fake_googlewifi: type) -> None:
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "list",
            "clients",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    payload = json.loads(result.output)
    by_id = {c["id"]: c for c in payload}
    assert by_id["sta-laptop"]["band"] == "5"
    assert by_id["sta-kid-tablet"]["band"] == "2.4"
    # Ethernet client has no band.
    assert by_id["sta-nas-wired"]["band"] is None
    assert by_id["sta-nas-wired"]["connection_type"] == "ethernet"


def test_paused_and_priority_until_propagate(isolated_xdg: Path, fake_googlewifi: type) -> None:
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "list",
            "clients",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    payload = json.loads(result.output)
    kid = next(c for c in payload if c["id"] == "sta-kid-tablet")
    assert kid["paused"] is True
    # FR-22: RFC 3339 UTC ``Z`` suffix.
    assert kid["priority_until"] == "2026-05-03T13:30:00Z"
    assert kid["group_assignment"] == "parental"

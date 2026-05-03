"""CliRunner tests for ``nest-cli wifi network`` (FR-WIFI-13).

Coverage:

- Happy path → emit §10.10 WifiNetwork record with all fields.
- Group not found → exit 4 (family=wifi).
- --experimental-wifi gate enforced.
"""

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


def test_network_happy_path(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`wifi network group-home-001 --experimental-wifi --json`."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "network",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["group_id"] == "group-home-001"
    assert payload["ssid"] == "HomeMeshNet"
    assert payload["guest_enabled"] is False
    assert "ipv4" in payload
    assert "ipv6" in payload
    assert "dns_servers" in payload


def test_network_unknown_group_exits_4(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """Unknown group id → exit 4 (family=wifi)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "network",
            "group-no-such",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_network_requires_experimental_flag(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """Missing --experimental-wifi → exit 64."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "network",
            "group-home-001",
        ],
    )
    assert result.exit_code == 64

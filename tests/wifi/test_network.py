"""CliRunner tests for ``nest-cli wifi network`` (FR-WIFI-13).

Phase B status: HomeGraph carries no SSID/IPv4/IPv6/DNS data, so the
verb exits 5 (unsupported_feature, family=wifi) until Phase C maps the
real Foyer network-info RPC. The CLI surface still ships so operator
scripts wire correctly; output is just an exit-5 envelope.

Coverage:

- Verb exits 5 with family=wifi (Phase B posture).
- --experimental-wifi gate still fires before the verb body.
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
            version=2,
            type="foyer",
            google_account_email="me@example.com",
            master_token="t",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
        ),
    )


def test_network_exits_5_in_phase_b(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`wifi network group-home-001 --experimental-wifi --json` → exit 5.

    Phase B: HomeGraph projection has no network config, so the verb
    exit-5s with family=wifi rather than emit a record full of
    ``"<unknown>"`` placeholders.
    """
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
    assert result.exit_code == 5, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert payload["error"] == "unsupported_feature"


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

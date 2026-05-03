"""CliRunner tests for ``nest-cli wifi guest enable|disable`` (FR-WIFI-14).

Phase 3.1 status: the upstream ``googlewifi`` library does not yet
expose a guest-network setter (mirrors set_station_group's posture).
The verbs ship as wired-through CLI surfaces; the FoyerClient raises
EXIT_UNSUPPORTED_FEATURE so operators see a clean exit-5 with a hint
pointing at the upstream gap.

Coverage:

- ``wifi guest enable <group>`` → exit 5 (family=wifi).
- ``wifi guest disable <group>`` → exit 5 (family=wifi).
- --experimental-wifi gate enforced for both verbs.
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


def test_guest_enable_exits_5_unsupported(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`wifi guest enable group-home-001 --experimental-wifi` → exit 5."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "guest",
            "enable",
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


def test_guest_disable_exits_5_unsupported(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`wifi guest disable group-home-001 --experimental-wifi` → exit 5."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "guest",
            "disable",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 5, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_guest_enable_requires_experimental_flag(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """Missing --experimental-wifi → exit 64."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["guest", "enable", "group-home-001"],
    )
    assert result.exit_code == 64


def test_guest_disable_requires_experimental_flag(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """Missing --experimental-wifi → exit 64."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["guest", "disable", "group-home-001"],
    )
    assert result.exit_code == 64

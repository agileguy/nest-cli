"""CliRunner tests for ``nest-cli wifi speedtest run`` (FR-WIFI-8).

Coverage:

- Happy path: emit one §10.9 SpeedTest record.
- --timeout custom: forwarded to FoyerClient.
- Transport failure → exit 3 (family=wifi).
- --experimental-wifi gate enforced.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


def test_speedtest_run_happy_path(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`wifi speedtest run group-home-001 --experimental-wifi --json`."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["group_id"] == "group-home-001"
    assert payload["point_id"] == "ap-master-living-room"
    assert payload["source"] == "router"
    assert payload["download_mbps"] > 0
    assert payload["ts"].endswith("Z")


def test_speedtest_run_requires_experimental_flag(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """Missing --experimental-wifi → exit 64 (family=wifi)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 64, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert "experimental" in payload["message"].lower()


def test_speedtest_run_timeout_exits_3(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A speed test that exceeds --timeout maps to exit 3 (family=wifi)."""
    import asyncio

    class _SlowGW:
        last_instance: _SlowGW | None = None

        def __init__(self, refresh_token: str | None = None, **_: Any) -> None:
            type(self).last_instance = self

        async def connect(self) -> bool:
            return True

        async def run_speed_test(self, system_id: str) -> dict[str, Any]:
            await asyncio.sleep(5.0)
            return {}

        async def close(self) -> None:
            return None

    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _SlowGW  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--experimental-wifi",
            "--timeout",
            "0.1",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 3, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

"""CliRunner tests for ``nest-cli wifi speedtest history`` (FR-WIFI-9).

Coverage:

- Default --limit 30; results sorted descending by ts.
- Custom --limit 2 truncates client-side.
- --limit 0 / --limit 366 → Click usage error.
- Empty history (group has no test runs) → exit 0, empty list.
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


def test_speedtest_history_default_limit(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`wifi speedtest history group-home-001 --experimental-wifi --json`."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 3  # the 3-entry fixture corpus
    # Descending by ts.
    timestamps = [p["ts"] for p in payload]
    assert timestamps == sorted(timestamps, reverse=True)


def test_speedtest_history_explicit_limit(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`--limit 2` truncates to the two most recent results."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "2",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 2


def test_speedtest_history_limit_below_range_rejected(isolated_xdg: Path) -> None:
    """`--limit 0` → Click usage error."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "0",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "0" in err or "range" in err.lower() or "invalid" in err.lower()


def test_speedtest_history_limit_above_range_rejected(isolated_xdg: Path) -> None:
    """`--limit 366` → Click usage error (Foyer cap is 365)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "366",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0


def test_speedtest_history_empty_results(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty upstream history → exit 0, empty list."""

    class _EmptyHistoryGW:
        last_instance: _EmptyHistoryGW | None = None

        def __init__(self, refresh_token: str | None = None, **_: Any) -> None:
            type(self).last_instance = self

        async def connect(self) -> bool:
            return True

        async def speed_test_results(self, system_id: str) -> list[Any]:
            return []

        async def close(self) -> None:
            return None

    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _EmptyHistoryGW  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == []


def test_speedtest_history_requires_experimental_flag(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """Missing --experimental-wifi → exit 64."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
        ],
    )
    assert result.exit_code == 64

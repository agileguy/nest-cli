"""CliRunner tests for ``nest-cli wifi point-health`` (FR-WIFI-15).

Coverage:

- Master point happy path → emit §10.11 record (signal None).
- Satellite point happy path → emit record with signal_to_upstream_dbm.
- Unknown point id → exit 4 (family=wifi).
- --experimental-wifi gate enforced.
- Offline point fixture → online=false, uptime_s=0.
"""

from __future__ import annotations

import json
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
from nest_cli.wifi.client import FoyerClient


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


def test_point_health_master(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`wifi point-health ap-master-living-room` → master role record."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "point-health",
            "ap-master-living-room",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "ap-master-living-room"
    assert payload["mesh_role"] == "master"
    assert payload["online"] is True
    # Master never has an upstream signal measurement.
    assert payload["signal_to_upstream_dbm"] is None
    assert payload["uptime_s"] > 0


def test_point_health_satellite(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """Satellite point → record with signal_to_upstream_dbm populated."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "point-health",
            "ap-sat-office",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mesh_role"] == "satellite"
    assert payload["signal_to_upstream_dbm"] == -52


def test_point_health_unknown_point_exits_4(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """Unknown point id → exit 4 (family=wifi)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "point-health",
            "ap-no-such-point",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_point_health_offline_point(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline point fixture → online=false, uptime_s=0.

    Injects a single offline AP via a one-off ``_fetch_systems`` patch.
    The conftest fixtures don't expose an offline shape (they use the
    full happy-path corpus), so we inline the patch here. We also short-
    circuit the optional-extra import probe in ``__init__`` so the test
    runs without needing gpsoauth/grpc on the import path.
    """

    def _init(
        self: FoyerClient,
        creds: Any,
    ) -> None:
        self._creds = creds
        self._access_token = None
        self._access_token_expiry = 0.0

    monkeypatch.setattr(FoyerClient, "__init__", _init)

    def _fetch(self: FoyerClient) -> dict[str, dict[str, Any]]:
        return {
            "group-home-001": {
                "id": "group-home-001",
                "access_points": {
                    "ap-offline": {
                        "id": "ap-offline",
                        "isMaster": False,
                        "displayName": "Offline AP",
                        "status": {
                            "apState": "OFFLINE",
                        },
                    },
                },
                "devices": {},
            }
        }

    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)

    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "point-health",
            "ap-offline",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["online"] is False
    assert payload["uptime_s"] == 0


def test_point_health_requires_experimental_flag(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """Missing --experimental-wifi → exit 64."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["point-health", "ap-master-living-room"],
    )
    assert result.exit_code == 64

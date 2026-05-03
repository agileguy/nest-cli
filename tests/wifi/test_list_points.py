"""CliRunner tests for ``nest-cli wifi list points`` (FR-WIFI-2)."""

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


def test_happy_path_emits_two_points(isolated_xdg: Path, fake_googlewifi: type) -> None:
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "list",
            "points",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 2
    ids = sorted(p["id"] for p in payload)
    assert ids == ["ap-master-living-room", "ap-sat-office"]
    master = next(p for p in payload if p["is_master"])
    satellite = next(p for p in payload if not p["is_master"])
    # FR §10.7: connected_clients_count is computed by FoyerClient.
    assert master["connected_clients_count"] == 3
    assert satellite["connected_clients_count"] == 1
    assert satellite["mesh_role"] == "satellite"


def test_unknown_group_exits_4(isolated_xdg: Path, fake_googlewifi: type) -> None:
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["list", "points", "group-no-such", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 4, result.output
    # Stderr error envelope carries family=wifi.
    err_text = result.stderr or result.output
    assert "wifi" in err_text


def test_upstream_shape_rotation_exits_1(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If googlewifi returns a non-dict, exit 1 (device_error, family=wifi)."""
    import sys

    class _RotatedGoogleWifi:
        def __init__(self, refresh_token: str | None = None, **_: object) -> None:
            pass

        async def get_systems(self) -> list[str]:
            return ["wrong", "shape"]

        async def close(self) -> None:
            return None

    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _RotatedGoogleWifi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["list", "points", "group-home-001", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 1, result.output

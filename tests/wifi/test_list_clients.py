"""CliRunner tests for ``nest-cli wifi list clients`` (FR-WIFI-3, Phase C).

Phase C implements the verb via ``GET /v2/groups/{gid}/stations`` on the
Foyer REST surface. Tests seed v3 credentials so the OnHub mint is
reachable, then monkey-patch ``FoyerClient._rest`` to return canned
station payloads without touching the OAuth chain.
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


def _seed_v3() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=3,
            type="foyer",
            google_account_email="me@example.com",
            master_token="aas_et/m",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
            refresh_token="1//09abc-DEF",
        ),
    )


@pytest.fixture
def stub_rest(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch _rest to return a canned response per-call for list_clients."""
    state: dict[str, Any] = {"calls": [], "response": {"stations": []}}

    def _fake_rest(
        self: FoyerClient,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        state["calls"].append({"method": method, "path": path, "json": json, "params": params})
        return state["response"]

    monkeypatch.setattr(FoyerClient, "_rest", _fake_rest)
    return state


def test_list_clients_empty_inventory(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: dict[str, Any]
) -> None:
    _seed_v3()
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
    assert payload == []
    assert stub_rest["calls"][0]["path"] == "/v2/groups/group-home-001/stations"


def test_list_clients_returns_station_records(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: dict[str, Any]
) -> None:
    _seed_v3()
    stub_rest["response"] = {
        "stations": [
            {
                "id": "sta-laptop",
                "friendlyName": "Laptop",
                "apId": "ap-master-living-room",
                "macAddress": "aa:bb:cc:dd:ee:ff",
                "frequencyBand": "BAND_5_GHZ",
            }
        ]
    }
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
    assert len(payload) == 1
    assert payload[0]["id"] == "sta-laptop"
    assert payload[0]["friendly_name"] == "Laptop"
    assert payload[0]["band"] == "5"

"""E2E CliRunner tests for ``nest-cli wifi list clients`` (FR-WIFI-3).

Coverage:

- Empty inventory → empty JSON list, GET to /v2/groups/<gid>/stations.
- Populated inventory → list of station records with normalized fields.
- Missing credentials → exit 2 family=wifi.
- Missing --experimental-wifi → exit 64.
- Missing positional group_id → exit != 0.
- v2 creds exit 2 with bootstrap hint.
- --output jsonl emits one record per line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group
from tests.e2e.conftest import RestRecorder

SeedV3 = Any
SeedV2 = Any


def test_list_clients_empty_inventory_returns_empty_list(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
    stub_rest.register("GET", "/v2/groups/group-home-001/stations", {"stations": []})
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
    assert stub_rest.calls[0]["method"] == "GET"
    assert stub_rest.calls[0]["path"] == "/v2/groups/group-home-001/stations"


def test_list_clients_populated_returns_normalized_records(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
    stub_rest.register(
        "GET",
        "/v2/groups/group-home-001/stations",
        {
            "stations": [
                {
                    "id": "sta-laptop",
                    "friendlyName": "Laptop",
                    "apId": "ap-master-living-room",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                    "frequencyBand": "BAND_5_GHZ",
                },
                {
                    "id": "sta-phone",
                    "friendlyName": "Phone",
                    "apId": "ap-sat-office",
                    "macAddress": "11:22:33:44:55:66",
                    "frequencyBand": "BAND_2_4_GHZ",
                },
            ]
        },
    )
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
    assert len(payload) == 2
    laptop = next(p for p in payload if p["id"] == "sta-laptop")
    assert laptop["friendly_name"] == "Laptop"
    assert laptop["band"] == "5"


def test_list_clients_jsonl_mode_emits_one_per_line(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
    stub_rest.register(
        "GET",
        "/v2/groups/group-home-001/stations",
        {
            "stations": [
                {
                    "id": "sta-a",
                    "friendlyName": "A",
                    "apId": "ap-master-living-room",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "frequencyBand": "BAND_5_GHZ",
                },
                {
                    "id": "sta-b",
                    "friendlyName": "B",
                    "apId": "ap-sat-office",
                    "macAddress": "aa:bb:cc:dd:ee:02",
                    "frequencyBand": "BAND_5_GHZ",
                },
            ]
        },
    )
    result = runner.invoke(
        wifi_group,
        ["list", "clients", "group-home-001", "--experimental-wifi", "--jsonl"],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().splitlines() if line]
    assert len(lines) == 2
    ids = {json.loads(line)["id"] for line in lines}
    assert ids == {"sta-a", "sta-b"}


def test_list_clients_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
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
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_list_clients_v2_creds_exits_2_with_bootstrap_hint(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
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
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert "wifi-refresh-bootstrap" in (payload.get("hint") or "")


def test_list_clients_without_experimental_exits_64(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "clients", "group-home-001"],
    )
    assert result.exit_code == 64, result.output


def test_list_clients_missing_positional_arg_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "clients", "--experimental-wifi"],
    )
    assert result.exit_code != 0

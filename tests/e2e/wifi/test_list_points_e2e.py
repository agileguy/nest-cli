"""E2E CliRunner tests for ``nest-cli wifi list points`` (FR-WIFI-2).

Coverage:

- Happy path → emits §10.7 WifiPoint records with master + satellite roles.
- Unknown group id → exit 4 family=wifi.
- Missing credentials → exit 2 family=wifi.
- Missing --experimental-wifi → exit 64.
- Missing positional group_id → exit != 0.
- --output jsonl emits one record per line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group

SeedV2 = Any


def test_list_points_happy_path_emits_two_points(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
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
    assert master["mesh_role"] == "master"


def test_list_points_jsonl_mode_emits_one_per_line(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "points", "group-home-001", "--experimental-wifi", "--jsonl"],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().splitlines() if line]
    assert len(lines) == 2


def test_list_points_unknown_group_exits_4(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        [
            "list",
            "points",
            "group-no-such",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 4, result.output
    err_text = result.stderr or result.output
    assert "wifi" in err_text


def test_list_points_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
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
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_list_points_without_experimental_exits_64(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "points", "group-home-001"],
    )
    assert result.exit_code == 64, result.output


def test_list_points_missing_positional_arg_rejected(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "points", "--experimental-wifi"],
    )
    assert result.exit_code != 0

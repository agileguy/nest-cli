"""E2E CliRunner tests for ``nest-cli wifi point-health`` (FR-WIFI-15).

Coverage:

- Master point happy path → record with mesh_role=master, signal=None.
- Satellite point happy path → record with signal_to_upstream_dbm populated.
- Unknown point id → exit 4 family=wifi.
- Missing credentials → exit 2 family=wifi.
- Missing --experimental-wifi → exit 64.
- Missing positional arg → exit != 0.
- --output json emits parseable record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group

SeedV2 = Any


def test_point_health_master_emits_record(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
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
    assert payload["signal_to_upstream_dbm"] is None


def test_point_health_satellite_emits_record_with_signal(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
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
    assert payload["signal_to_upstream_dbm"] is not None


def test_point_health_unknown_id_exits_4(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        [
            "point-health",
            "ap-no-such",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_point_health_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
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
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_point_health_without_experimental_exits_64(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["point-health", "ap-master-living-room"],
    )
    assert result.exit_code == 64, result.output


def test_point_health_missing_positional_arg_rejected(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["point-health", "--experimental-wifi"],
    )
    assert result.exit_code != 0

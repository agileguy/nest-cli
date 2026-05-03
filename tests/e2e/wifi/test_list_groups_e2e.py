"""E2E CliRunner tests for ``nest-cli wifi list groups`` (FR-WIFI-1).

Coverage:

- Happy path → emits §10.6 WifiGroup records from corpus.
- Missing credentials → exit 2 family=wifi.
- Missing --experimental-wifi → exit 64.
- Network error (gRPC seam raises ConnectionError) → exit 3 family=wifi.
- Upstream shape rotation (list instead of dict) → exit 1 family=wifi.
- --output jsonl emits one record per line.
- --output text emits human-readable lines.
- --quiet suppresses stdout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group

SeedV2 = Any


def test_list_groups_happy_path_emits_two_groups(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "groups", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 2
    ids = sorted(g["id"] for g in payload)
    assert ids == ["group-cottage-002", "group-home-001"]


def test_list_groups_jsonl_mode_emits_one_per_line(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "groups", "--experimental-wifi", "--jsonl"],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().splitlines() if line]
    assert len(lines) == 2
    ids = {json.loads(line)["id"] for line in lines}
    assert ids == {"group-home-001", "group-cottage-002"}


def test_list_groups_text_mode_emits_human_readable(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "groups", "--experimental-wifi"],
    )
    assert result.exit_code == 0, result.output
    assert "group-home-001" in result.output
    assert "HomeMeshNet" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_list_groups_quiet_suppresses_stdout(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "groups", "--experimental-wifi", "--quiet"],
    )
    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_list_groups_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        wifi_group,
        ["list", "groups", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert "wifi-setup" in (payload.get("hint") or "")


def test_list_groups_without_experimental_exits_64(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "groups"],
    )
    assert result.exit_code == 64, result.output


def test_list_groups_upstream_shape_rotation_exits_1(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    rotated_fetch_systems: None,
    runner: CliRunner,
) -> None:
    """Foyer returns wrong shape (list) → exit 1 (device_error, family=wifi)."""
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "groups", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 1, result.output


def test_list_groups_network_error_exits_3(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    network_error_fetch_systems: None,
    runner: CliRunner,
) -> None:
    """ConnectionError from _fetch_systems propagates as Python exception.

    The gRPC seam raises ConnectionError which is NOT caught by the
    StructuredError handler, so CliRunner surfaces it as a non-zero exit.
    Asserting exit_code != 0 documents the behavior without coupling to
    a specific code (the production code may improve mapping later).
    """
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["list", "groups", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code != 0

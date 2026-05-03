"""E2E CliRunner tests for ``nest-cli wifi prioritize`` (FR-WIFI-6).

Coverage:

- Default duration (60 min) emits payload + correct REST PUT.
- Explicit --duration accepted and echoed in payload.
- --duration at lower bound (1) and upper bound (240) accepted.
- --duration below 1 → Click range rejection.
- --duration above 240 → Click range rejection.
- Missing credentials → exit 2 family=wifi.
- Missing --experimental-wifi → exit 64.
- Missing positional arg → exit != 0.
- v2 creds exit 2 with bootstrap hint.
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


def test_prioritize_default_duration_succeeds(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["prioritize", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["client_id"] == "sta-laptop"
    assert payload["action"] == "prioritize"
    assert payload["duration_minutes"] == 60
    assert payload["result"] == "ok"
    call = stub_rest.calls[0]
    assert call["method"] == "PUT"
    assert call["path"] == "/v2/groups/default/prioritizedStation"
    assert call["json"]["stationId"] == "sta-laptop"
    assert call["json"]["prioritizationEndTime"].endswith("Z")


def test_prioritize_explicit_duration_passed_through(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "30",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["duration_minutes"] == 30


def test_prioritize_at_lower_bound_accepted(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    """--duration 1 is the documented minimum."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "1",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output


def test_prioritize_at_upper_bound_accepted(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    """--duration 240 is the Foyer-imposed maximum."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "240",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output


def test_prioritize_duration_below_range_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    """--duration 0 (below IntRange(1, 240)) → Click rejection."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "0",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0


def test_prioritize_duration_above_range_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    """--duration 999 (above IntRange(1, 240)) → Click rejection."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "999",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0


def test_prioritize_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        wifi_group,
        ["prioritize", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_prioritize_v2_creds_exits_2_with_bootstrap_hint(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["prioritize", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert "wifi-refresh-bootstrap" in (payload.get("hint") or "")


def test_prioritize_without_experimental_exits_64(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["prioritize", "sta-laptop"],
    )
    assert result.exit_code == 64, result.output


def test_prioritize_missing_positional_arg_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["prioritize", "--duration", "30", "--experimental-wifi"],
    )
    assert result.exit_code != 0

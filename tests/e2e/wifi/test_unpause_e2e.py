"""E2E CliRunner tests for ``nest-cli wifi unpause`` (FR-WIFI-5).

Coverage:

- Happy path → exit 0, JSON payload, REST PUT with blocked=false.
- Idempotent re-unpause still succeeds.
- Unknown station id still issues call (Foyer is the source of truth).
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


def test_unpause_happy_path_emits_ok_envelope(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["client_id"] == "sta-laptop"
    assert payload["action"] == "unpause"
    assert payload["result"] == "ok"
    assert len(stub_rest.calls) == 1
    assert stub_rest.calls[0]["json"]["blocked"] == "false"
    assert stub_rest.calls[0]["json"]["stationId"] == "sta-laptop"


def test_unpause_already_unpaused_is_idempotent(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    """Foyer accepts re-unblocks without error — verb returns OK."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-other", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output


def test_unpause_unknown_station_still_issues_call(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    """Unknown id still PUTs — server-side 404 mapping is Foyer's job."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-no-such", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    assert stub_rest.calls[0]["json"]["stationId"] == "sta-no-such"


def test_unpause_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_unpause_v2_creds_exits_2_with_bootstrap_hint(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert "wifi-refresh-bootstrap" in (payload.get("hint") or "")


def test_unpause_without_experimental_exits_64(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-laptop"],
    )
    assert result.exit_code == 64, result.output


def test_unpause_missing_positional_arg_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["unpause", "--experimental-wifi"],
    )
    assert result.exit_code != 0

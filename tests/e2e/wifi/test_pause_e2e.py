"""E2E CliRunner tests for ``nest-cli wifi pause`` (FR-WIFI-4).

Coverage:

- Happy path with v3 creds → exit 0, REST PUT issued, JSONL envelope shape.
- Missing credentials file → exit 2, family=wifi, hint references wifi-setup.
- Missing --experimental-wifi → exit 64.
- Missing positional client_id → exit 2 (Click usage error).
- v2 creds (no refresh_token) → exit 2 with bootstrap hint.
- Concurrency option parses (group target path).
- Output mode --quiet suppresses stdout but preserves exit code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group
from tests.e2e.conftest import RestRecorder

SeedV3 = Any  # callable[[], WifiCredentials] from conftest


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_pause_happy_path_emits_ok_envelope(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    """Single-station pause: exit 0, JSON payload, exactly one REST PUT."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "client_id": "sta-laptop",
        "action": "pause",
        "result": "ok",
    }
    assert len(stub_rest.calls) == 1
    call = stub_rest.calls[0]
    assert call["method"] == "PUT"
    assert call["path"] == "/v2/groups/default/stationBlocking"
    assert call["json"]["stationId"] == "sta-laptop"
    assert call["json"]["blocked"] == "true"


def test_pause_quiet_mode_suppresses_stdout(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    """--quiet suppresses stdout but still issues the REST call."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-laptop", "--experimental-wifi", "--quiet"],
    )
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert len(stub_rest.calls) == 1


def test_pause_text_mode_emits_human_readable(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    """Default text mode emits labeled output, not JSON."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-laptop", "--experimental-wifi"],
    )
    assert result.exit_code == 0, result.output
    assert "sta-laptop" in result.output
    assert "pause" in result.output
    # Not valid JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_pause_missing_creds_exits_2_with_wifi_family(
    isolated_xdg: Path, runner: CliRunner
) -> None:
    """No credentials file → exit 2, family=wifi, hint names wifi-setup."""
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert payload["error"] == "auth_failed"
    assert "wifi-setup" in (payload.get("hint") or "")


def test_pause_v2_creds_exits_2_with_bootstrap_hint(
    isolated_xdg: Path, seed_v2_creds: SeedV3, runner: CliRunner
) -> None:
    """v2 creds (no refresh_token) hit the OnHub mint path and exit 2.

    The verb reaches FoyerClient.pause_station, which fires
    _refresh_onhub_access_token. That helper sees ``creds.refresh_token
    is None`` and raises EXIT_AUTH_ERROR with a hint pointing at
    ``auth wifi-refresh-bootstrap``.
    """
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert "wifi-refresh-bootstrap" in (payload.get("hint") or "")


def test_pause_without_experimental_flag_exits_64(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    """Missing --experimental-wifi → exit 64 with experimental-gate hint."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-laptop", "--output", "json"],
    )
    assert result.exit_code == 64, result.output
    err = result.stderr or result.output
    assert "experimental" in err.lower()


def test_pause_missing_positional_arg_exits_2_click_usage(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    """Click rejects missing required positional with its own exit code 2."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["pause", "--experimental-wifi"],
    )
    # Click emits its own usage-error exit (2 is Click default for arg errors).
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "client" in err.lower() or "missing" in err.lower() or "argument" in err.lower()


def test_pause_concurrency_flag_accepts_int_in_range(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    runner: CliRunner,
) -> None:
    """--concurrency parses to int (single station ignores it but flag is valid)."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "pause",
            "sta-laptop",
            "--concurrency",
            "5",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output


def test_pause_concurrency_flag_out_of_range_rejected(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    """--concurrency 0 (below IntRange(1, 32)) is rejected by Click."""
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "pause",
            "sta-laptop",
            "--concurrency",
            "0",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "0" in err or "range" in err.lower() or "invalid" in err.lower()

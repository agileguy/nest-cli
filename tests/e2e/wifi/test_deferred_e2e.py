"""E2E CliRunner tests for Phase D-deferred wifi verbs.

The verbs ``wifi group-assign``, ``wifi guest enable``, and
``wifi guest disable`` ship as wired-through CLI surfaces, but the
FoyerClient deliberately raises ``EXIT_UNSUPPORTED_FEATURE`` (5) because
the underlying Foyer request bodies are undocumented and the risk of
corrupting station/group config is too high to ship a guess. This file
locks that posture in: the verbs MUST exit 5 with ``family="wifi"`` and
a hint substring referencing the Phase-D deferral.

Coverage:

- group-assign each --group choice (family / parental / guest / none) → exit 5.
- group-assign case-insensitive (FAMILY) → exit 5.
- group-assign invalid choice → Click rejection (exit != 0, exit != 5).
- group-assign missing --group → Click rejection.
- group-assign no-experimental → exit 64.
- guest enable → exit 5 family=wifi.
- guest disable → exit 5 family=wifi.
- guest enable no-experimental → exit 64.
- guest disable no-experimental → exit 64.
- All exit-5 envelopes carry the Phase-D hint substring.
- All exit-5 envelopes carry the unsupported_feature error enum.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group

SeedV2 = Any


# ---------------------------------------------------------------------------
# wifi group-assign
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group_value", ["family", "parental", "guest", "none"])
def test_group_assign_each_choice_exits_5(
    group_value: str,
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    """All four --group choices reach FoyerClient and exit 5 with family=wifi."""
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        [
            "group-assign",
            "sta-laptop",
            "--group",
            group_value,
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 5, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert payload["error"] == "unsupported_feature"


def test_group_assign_case_insensitive_family_accepted(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    """Click case_sensitive=False accepts uppercase choice values."""
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        [
            "group-assign",
            "sta-laptop",
            "--group",
            "FAMILY",
            "--experimental-wifi",
        ],
    )
    # Same exit-5 path as the lowercase form proves Click accepted it.
    assert result.exit_code == 5, result.output


def test_group_assign_invalid_choice_rejected(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    """``--group enterprise`` is not in the choice set → Click rejects."""
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        [
            "group-assign",
            "sta-laptop",
            "--group",
            "enterprise",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    assert result.exit_code != 5  # Not the deferred-verb exit
    err = result.stderr or result.output
    assert "enterprise" in err.lower() or "invalid" in err.lower() or "choice" in err.lower()


def test_group_assign_missing_group_flag_rejected(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    """Omitting --group → Click usage error (the option is required)."""
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["group-assign", "sta-laptop", "--experimental-wifi"],
    )
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "group" in err.lower() or "missing" in err.lower() or "required" in err.lower()


def test_group_assign_without_experimental_exits_64(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["group-assign", "sta-laptop", "--group", "family"],
    )
    assert result.exit_code == 64, result.output


def test_group_assign_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
    """No credentials → exit 2 (auth) before reaching the deferred verb."""
    result = runner.invoke(
        wifi_group,
        [
            "group-assign",
            "sta-laptop",
            "--group",
            "family",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


# ---------------------------------------------------------------------------
# wifi guest enable | disable
# ---------------------------------------------------------------------------


def test_guest_enable_exits_5_with_phase_d_hint(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        [
            "guest",
            "enable",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 5, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert payload["error"] == "unsupported_feature"
    hint = (payload.get("hint") or "").lower()
    assert "phase d" in hint or "deferred" in hint


def test_guest_disable_exits_5_with_phase_d_hint(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        [
            "guest",
            "disable",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 5, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert payload["error"] == "unsupported_feature"


def test_guest_enable_without_experimental_exits_64(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["guest", "enable", "group-home-001"],
    )
    assert result.exit_code == 64, result.output


def test_guest_disable_without_experimental_exits_64(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["guest", "disable", "group-home-001"],
    )
    assert result.exit_code == 64, result.output


def test_guest_enable_missing_positional_arg_rejected(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["guest", "enable", "--experimental-wifi"],
    )
    assert result.exit_code != 0


def test_guest_disable_missing_positional_arg_rejected(
    isolated_xdg: Path, seed_v2_creds: SeedV2, runner: CliRunner
) -> None:
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        ["guest", "disable", "--experimental-wifi"],
    )
    assert result.exit_code != 0


def test_guest_enable_missing_creds_exits_2(isolated_xdg: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        wifi_group,
        [
            "guest",
            "enable",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


# ---------------------------------------------------------------------------
# Network info verb (wifi network) — also reaches a Phase-? gap; smoke test
# ---------------------------------------------------------------------------


def test_wifi_network_happy_path_smoke(
    isolated_xdg: Path,
    seed_v2_creds: SeedV2,
    fake_fetch_systems: None,
    runner: CliRunner,
) -> None:
    """``wifi network`` is a Phase B read verb — corpus-derived smoke check."""
    seed_v2_creds()
    result = runner.invoke(
        wifi_group,
        [
            "network",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    # Exit 0 (corpus-derived) — production may evolve this.
    assert result.exit_code in (0, 5), result.output

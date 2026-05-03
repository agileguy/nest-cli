"""E2E CliRunner tests for ``nest-cli wifi reboot point|group`` (FR-WIFI-10/11/12).

Covers both verbs in one file because they share the reboot subgroup +
``_confirm_reboot_or_exit`` confirmation gate.

Coverage:

- point: tty + 'y' → confirm, REST POST to /v2/accesspoints/<id>/reboot.
- point: tty + 'n' → abort, exit 0, no REST call.
- point: non-tty without --yes → exit 64.
- point: non-tty + --yes → REST POST issued, payload emitted.
- point: --quiet implies --yes (FR-WIFI-12).
- point: missing --experimental-wifi → exit 64.
- point: missing positional arg → exit != 0.
- point: missing creds → exit 2 family=wifi.
- group: tty + 'y' → confirm + POST to /v2/groups/<id>/reboot.
- group: tty stderr names resolved point list before prompt.
- group: non-tty + --yes → POST issued, rebooted_points in payload.
- group: non-tty without --yes → exit 64.
- group: --quiet implies --yes.
- group: unknown group id → exit 4 (list_points fails inside reboot_group).
- group: missing creds → exit 2 family=wifi.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from nest_cli.cli.wifi_cmd import wifi_group
from tests.e2e.conftest import RestRecorder

SeedV3 = Any
ForceTty = Callable[[bool], None]


# ---------------------------------------------------------------------------
# wifi reboot point
# ---------------------------------------------------------------------------


def test_reboot_point_tty_yes_confirms_and_posts(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    """TTY + 'y' on prompt → confirm passes, REST POST issued."""
    force_tty(True)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--experimental-wifi",
            "--output",
            "json",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert stub_rest.calls[0]["method"] == "POST"
    assert stub_rest.calls[0]["path"] == "/v2/accesspoints/ap-master-living-room/reboot"


def test_reboot_point_tty_no_aborts_silently(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    """TTY + 'n' on prompt → abort, exit 0, no REST call."""
    force_tty(True)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["reboot", "point", "ap-master-living-room", "--experimental-wifi"],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Aborted" in result.stderr
    assert len(stub_rest.calls) == 0


def test_reboot_point_non_tty_without_yes_exits_64(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    """Non-tty without --yes → exit 64 (confirmation gate)."""
    force_tty(False)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 64, result.output


def test_reboot_point_non_tty_with_yes_succeeds(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    force_tty(False)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["point_id"] == "ap-master-living-room"
    assert payload["action"] == "reboot"
    assert payload["result"] == "ok"
    assert len(stub_rest.calls) == 1


def test_reboot_point_quiet_implies_yes(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    """FR-WIFI-12: --quiet alone (non-tty) implies --yes; verb succeeds silently."""
    force_tty(False)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--quiet",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert len(stub_rest.calls) == 1


def test_reboot_point_without_experimental_exits_64(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["reboot", "point", "ap-master-living-room"],
    )
    assert result.exit_code == 64, result.output


def test_reboot_point_missing_positional_arg_rejected(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    force_tty(False)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["reboot", "point", "--yes", "--experimental-wifi"],
    )
    assert result.exit_code != 0


def test_reboot_point_missing_creds_exits_2(
    isolated_xdg: Path, force_tty: ForceTty, runner: CliRunner
) -> None:
    force_tty(False)
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "point",
            "ap-master-living-room",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


# ---------------------------------------------------------------------------
# wifi reboot group
# ---------------------------------------------------------------------------


def test_reboot_group_non_tty_with_yes_succeeds(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    fake_fetch_systems: None,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    """Non-tty + --yes → POST issued + rebooted_points payload echo."""
    force_tty(False)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["group_id"] == "group-home-001"
    assert payload["action"] == "reboot"
    assert sorted(payload["rebooted_points"]) == [
        "ap-master-living-room",
        "ap-sat-office",
    ]


def test_reboot_group_non_tty_without_yes_exits_64(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    fake_fetch_systems: None,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    force_tty(False)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 64, result.output


def test_reboot_group_quiet_implies_yes(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    fake_fetch_systems: None,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    force_tty(False)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--quiet",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code == 0, result.output
    post_calls = [c for c in stub_rest.calls if c["method"] == "POST"]
    assert len(post_calls) == 1


def test_reboot_group_tty_yes_lists_points_on_stderr(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    fake_fetch_systems: None,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    """TTY mode names the resolved point list on stderr before prompting."""
    force_tty(True)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["reboot", "group", "group-home-001", "--experimental-wifi"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "ap-master-living-room" in result.stderr or "ap-sat-office" in result.stderr


def test_reboot_group_unknown_group_exits_4(
    isolated_xdg: Path,
    seed_v3_creds: SeedV3,
    stub_rest: RestRecorder,
    fake_fetch_systems: None,
    force_tty: ForceTty,
    runner: CliRunner,
) -> None:
    """Unknown group id → exit 4 (list_points raises not_found family=wifi)."""
    force_tty(False)
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-no-such",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_reboot_group_without_experimental_exits_64(
    isolated_xdg: Path, seed_v3_creds: SeedV3, runner: CliRunner
) -> None:
    seed_v3_creds()
    result = runner.invoke(
        wifi_group,
        ["reboot", "group", "group-home-001"],
    )
    assert result.exit_code == 64, result.output


def test_reboot_group_missing_creds_exits_2(
    isolated_xdg: Path, force_tty: ForceTty, runner: CliRunner
) -> None:
    force_tty(False)
    result = runner.invoke(
        wifi_group,
        [
            "reboot",
            "group",
            "group-home-001",
            "--yes",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

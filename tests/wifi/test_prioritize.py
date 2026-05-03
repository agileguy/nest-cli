"""CliRunner tests for ``nest-cli wifi prioritize`` (FR-WIFI-6).

Coverage:

- Happy path with default duration (60 minutes → 1 hour upstream).
- Explicit ``--duration 120`` (→ 2 hours upstream).
- Below range (``--duration 0``) → Click usage error.
- Above range (``--duration 300``) → Click usage error.
- Unknown client_id → exit 4.
- Minutes-to-hours conversion uses ceiling division.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli.wifi_cmd import wifi_group


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_wifi_creds() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=1,
            type="foyer",
            google_account_email="me@example.com",
            master_token="t",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
        ),
    )


# ---------------------------------------------------------------------------
# Happy path + default duration
# ---------------------------------------------------------------------------


def test_prioritize_default_duration_60_minutes(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """No ``--duration`` → default 60 minutes (1 hour upstream)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "client_id": "sta-laptop",
        "action": "prioritize",
        "duration_minutes": 60,
        "result": "ok",
    }
    last = fake_googlewifi.last_instance
    assert last is not None
    prio_calls = [c for c in last.calls if c[0] == "prioritize_device"]
    assert len(prio_calls) == 1
    _, args, _ = prio_calls[0]
    # 60 minutes → 1 hour upstream.
    assert args == ("group-home-001", "sta-laptop", 1)


def test_prioritize_explicit_duration_120_minutes(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """`--duration 120` → 2 hours upstream."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "120",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["duration_minutes"] == 120
    last = fake_googlewifi.last_instance
    assert last is not None
    prio_calls = [c for c in last.calls if c[0] == "prioritize_device"]
    _, args, _ = prio_calls[0]
    # 120 minutes → 2 hours upstream.
    assert args == ("group-home-001", "sta-laptop", 2)


def test_prioritize_45_minutes_rounds_up_to_one_hour(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """Sub-hour minutes round UP via ceiling division (45min → 1h)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "45",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    prio_calls = [c for c in last.calls if c[0] == "prioritize_device"]
    _, args, _ = prio_calls[0]
    assert args[2] == 1  # 45 minutes → 1 hour


def test_prioritize_91_minutes_rounds_up_to_two_hours(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """91 minutes → 2 hours upstream (ceiling division)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "91",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    prio_calls = [c for c in last.calls if c[0] == "prioritize_device"]
    _, args, _ = prio_calls[0]
    assert args[2] == 2


# ---------------------------------------------------------------------------
# Boundary values (FR-WIFI-6: 1..240 minutes)
# ---------------------------------------------------------------------------


def test_prioritize_below_range_zero_minutes_rejected(isolated_xdg: Path) -> None:
    """`--duration 0` → Click usage error (IntRange(1, 240))."""
    _seed_wifi_creds()
    runner = CliRunner()
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
    # Click usage error → non-zero exit. Click defaults to exit 2 on
    # parameter validation.
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "0" in err.lower() or "range" in err.lower() or "invalid" in err.lower()


def test_prioritize_above_range_300_minutes_rejected(isolated_xdg: Path) -> None:
    """`--duration 300` → Click usage error (above 240 ceiling)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "300",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "300" in err or "range" in err.lower() or "invalid" in err.lower()


def test_prioritize_at_max_240_minutes_accepted(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """`--duration 240` is the inclusive upper bound."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "240",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    prio_calls = [c for c in last.calls if c[0] == "prioritize_device"]
    _, args, _ = prio_calls[0]
    # 240 minutes → 4 hours upstream (240/60 == 4).
    assert args[2] == 4


# ---------------------------------------------------------------------------
# Unknown client
# ---------------------------------------------------------------------------


def test_prioritize_unknown_client_exits_4(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-no-such-client",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

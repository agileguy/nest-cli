"""CliRunner tests for ``nest-cli wifi prioritize`` (FR-WIFI-6).

Phase B status (2026-05-03): the prioritize action verb has not yet been
mapped onto the Foyer gRPC surface; every invocation that reaches the
client layer exits 5 (``unsupported_feature``, family=wifi).

Click-side validation (the IntRange(1, 240) on ``--duration``) still
runs *before* the verb body, so the below-range / above-range tests
remain meaningful — they verify Click rejects the input without ever
reaching the FoyerClient. The minutes→hours rounding tests have been
removed because the conversion code path no longer fires.
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
from nest_cli.errors import EXIT_UNSUPPORTED_FEATURE


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_wifi_creds() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=2,
            type="foyer",
            google_account_email="me@example.com",
            master_token="t",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
        ),
    )


# ---------------------------------------------------------------------------
# Phase B exit-5 posture (Click parsed --duration successfully, then verb stub
# fired). Phase C will reinstate the upstream-arg / rounding tests.
# ---------------------------------------------------------------------------


def test_prioritize_default_duration_exits_5(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """No ``--duration`` → Click defaults 60, then verb exits 5."""
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_prioritize_explicit_duration_exits_5(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """`--duration 120` is parsed by Click, then verb exits 5."""
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


def test_prioritize_at_max_240_minutes_exits_5(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """`--duration 240` is the inclusive upper bound — Click accepts, verb stubs."""
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


# ---------------------------------------------------------------------------
# Boundary values (FR-WIFI-6: 1..240 minutes) — Click-side validation fires
# BEFORE the verb body, so these still produce a Click usage error.
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
    # parameter validation. Critically NOT exit 5 — the verb body never
    # runs because Click rejected the choice first.
    assert result.exit_code != 0
    assert result.exit_code != EXIT_UNSUPPORTED_FEATURE
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
    assert result.exit_code != EXIT_UNSUPPORTED_FEATURE
    err = result.stderr or result.output
    assert "300" in err or "range" in err.lower() or "invalid" in err.lower()


# ---------------------------------------------------------------------------
# Unknown client → exit 5 (was 4 pre-Phase-B)
# ---------------------------------------------------------------------------


def test_prioritize_unknown_client_exits_5(
    isolated_xdg: Path, fake_googlewifi: None
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
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

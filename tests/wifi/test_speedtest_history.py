"""CliRunner tests for ``nest-cli wifi speedtest history`` (FR-WIFI-9).

Phase B status (2026-05-03): the speedtest-history action verb has not
yet been mapped onto the Foyer gRPC surface; every invocation that
reaches the FoyerClient layer exits 5 (``unsupported_feature``,
family=wifi).

Click-side validation on ``--limit`` (IntRange) still fires before the
verb body, so the below-range / above-range tests remain meaningful —
they verify Click rejects the input. The default-limit / explicit-limit
/ empty-results / sort-order tests have been reduced to exit-5 posture
checks; Phase C will reinstate the upstream-call-shape assertions.
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


def test_speedtest_history_default_limit_exits_5(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """`wifi speedtest history group-home-001` exits 5 (Phase B stub)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_speedtest_history_explicit_limit_exits_5(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """`--limit 2` is parsed by Click; verb body exits 5."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "2",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


def test_speedtest_history_limit_below_range_rejected(isolated_xdg: Path) -> None:
    """`--limit 0` → Click usage error (IntRange validation, not exit-5)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "0",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    assert result.exit_code != EXIT_UNSUPPORTED_FEATURE
    err = result.stderr or result.output
    assert "0" in err or "range" in err.lower() or "invalid" in err.lower()


def test_speedtest_history_limit_above_range_rejected(isolated_xdg: Path) -> None:
    """`--limit 366` → Click usage error (Foyer cap is 365)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
            "--limit",
            "366",
            "--experimental-wifi",
        ],
    )
    assert result.exit_code != 0
    assert result.exit_code != EXIT_UNSUPPORTED_FEATURE


def test_speedtest_history_requires_experimental_flag(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """Missing --experimental-wifi → exit 64 (gate fires before verb body)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "history",
            "group-home-001",
        ],
    )
    assert result.exit_code == 64

"""CliRunner tests for ``nest-cli wifi speedtest run`` (FR-WIFI-8).

Phase B status (2026-05-03): the speedtest-run action verb has not yet
been mapped onto the Foyer gRPC surface; every invocation that reaches
the FoyerClient layer exits 5 (``unsupported_feature``, family=wifi).
The happy-path / timeout / transport-error tests will be reinstated when
Phase C lands the actual ``run_speedtest`` RPC.

The ``--experimental-wifi`` gate fires *before* the verb body, so the
gate-enforcement test stays exit-64.
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


def test_speedtest_run_exits_5(isolated_xdg: Path, fake_googlewifi: None) -> None:
    """`wifi speedtest run group-home-001` exits 5 with family=wifi (Phase B)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_speedtest_run_requires_experimental_flag(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """Missing --experimental-wifi → exit 64 (gate fires before verb body)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 64, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert "experimental" in payload["message"].lower()


def test_speedtest_run_timeout_flag_still_parsed(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """`--timeout 0.1` is parsed by Click; verb body still exits 5."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "speedtest",
            "run",
            "group-home-001",
            "--experimental-wifi",
            "--timeout",
            "0.1",
            "--output",
            "json",
        ],
    )
    # Click accepted the float; verb body exits 5 before the timeout
    # would have fired against any real RPC.
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output

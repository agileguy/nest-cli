"""CliRunner tests for ``nest-cli wifi unpause`` (FR-WIFI-5).

Phase B status (2026-05-03): the unpause action verb has not yet been
mapped onto the Foyer gRPC surface; every invocation exits 5
(``unsupported_feature``, family=wifi). The happy-path / idempotence /
upstream-arg tests will be reinstated when Phase C lands the actual
``unpause_station`` RPC.
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


def test_unpause_paused_client_exits_5(isolated_xdg: Path, fake_googlewifi: None) -> None:
    """`wifi unpause sta-kid-tablet` exits 5 with family=wifi (Phase B)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "unpause",
            "sta-kid-tablet",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_unpause_already_unpaused_client_exits_5(
    isolated_xdg: Path, fake_googlewifi: None
) -> None:
    """Idempotence test reduced to exit-5 (verb stubs out before any RPC)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-laptop", "--experimental-wifi"],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


def test_unpause_unknown_client_exits_5(isolated_xdg: Path, fake_googlewifi: None) -> None:
    """Unknown client_id exits 5 (was 4 pre-Phase-B; verb no longer validates)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "unpause",
            "sta-no-such-client",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

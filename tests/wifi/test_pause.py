"""CliRunner tests for ``nest-cli wifi pause`` (FR-WIFI-4).

Phase B status (2026-05-03): the pause action verb has not yet been
mapped onto the Foyer gRPC surface, so every invocation exits 5
(``unsupported_feature``, family=wifi) with a hint pointing at the
Phase-C deferral. These tests verify the exit-5 posture; once Phase C
lands the actual ``pause_station`` RPC, the happy-path / idempotence /
upstream-arg tests will need to be reinstated against the new transport.
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
# Phase B exit-5 posture (Phase C will reinstate the happy-path tests)
# ---------------------------------------------------------------------------


def test_pause_known_client_exits_5(isolated_xdg: Path, fake_googlewifi: None) -> None:
    """`wifi pause sta-laptop` exits 5 with family=wifi (Phase B stub)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "pause",
            "sta-laptop",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"


def test_pause_already_paused_client_exits_5(isolated_xdg: Path, fake_googlewifi: None) -> None:
    """Idempotence test reduced to exit-5: even paused clients fail-fast."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "pause",
            "sta-kid-tablet",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output


def test_pause_unknown_client_exits_5(isolated_xdg: Path, fake_googlewifi: None) -> None:
    """Unknown client_id still exits 5 — the verb stubs out before validation.

    Pre-Phase-B this test asserted exit 4 (not_found); Phase B exits 5
    earlier in the call chain because the FoyerClient method has no RPC
    to dispatch the lookup against. exit-5 wins.
    """
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "pause",
            "sta-no-such-client",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

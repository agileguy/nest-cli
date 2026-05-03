"""End-to-end test for ``wifi pause @group`` fan-out (FR-6, FR-8a).

Phase B status (2026-05-03): the wifi ``pause`` verb stubs out at the
FoyerClient layer with exit-5 (``unsupported_feature``, family=wifi).
The fan-out machinery still emits one FR-9a envelope per target — both
sub-targets fail with exit-5, and the FR-8a aggregate exit code follows
the all-failed rule (= exit code of the first resolved target = 5).

Once Phase C lands the actual ``pause_station`` RPC, this test will be
reinstated against the new transport with exit-0 envelopes.

Reuses the ``fake_googlewifi`` fixture name from ``tests/wifi/conftest.py``
(now an alias for the Phase-B ``fake_foyer_client`` fixture, kept for
backward compatibility). For this batch test, the fixture is a no-op
because the verb exits 5 before reaching any fetch path; we still
ensure the FoyerClient extras-import probe is short-circuited.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nest_cli.auth.wifi_credentials import (
    default_wifi_credentials_path,
    save_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli import cli as cli_root
from nest_cli.errors import EXIT_UNSUPPORTED_FEATURE
from nest_cli.wifi.client import FoyerClient


@pytest.fixture
def patched_foyer_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit FoyerClient.__init__ extras-import probe.

    The wifi ``pause`` verb constructs a FoyerClient and immediately
    raises exit-5 (the action verb is a Phase-B stub), but the
    constructor still runs and probes for gpsoauth/grpc/ghome_foyer_api.
    Tests should not depend on those modules being installed; we replace
    ``__init__`` with a minimal version that skips the probe.

    ``_fetch_systems`` is also patched to a stub that returns ``{}`` for
    safety, even though pause never reaches it.
    """

    def _init(self: FoyerClient, creds: WifiCredentials) -> None:
        self._creds = creds
        self._access_token = None
        self._access_token_expiry = 0.0

    def _fetch(self: FoyerClient) -> dict[str, dict[str, Any]]:
        return {}

    monkeypatch.setattr(FoyerClient, "__init__", _init)
    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)


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
            master_token="t",  # noqa: S106
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
        ),
    )


def _write_config_with_group(xdg_root: Path) -> None:
    """Write a config.toml with two wifi station aliases + a group."""
    config_path = xdg_root / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[aliases]\n"
        'kid-tablet = "wifi:sta-kid-tablet"\n'
        'phone = "wifi:sta-phone"\n'
        "\n"
        "[groups]\n"
        'kids-devices = ["kid-tablet", "phone"]\n',
        encoding="utf-8",
    )


class TestWifiPauseGroup:
    def test_wifi_pause_at_group_emits_two_exit5_envelopes(
        self,
        isolated_xdg: Path,
        patched_foyer_client: None,
    ) -> None:
        """``wifi pause @kids-devices`` → two FR-9a envelopes, exit 5.

        Both sub-targets exit 5; the FR-8a aggregate is 5 (all-failed →
        first target's exit code).
        """
        _seed_wifi_creds()
        _write_config_with_group(isolated_xdg)

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["wifi", "pause", "@kids-devices", "--experimental-wifi", "--jsonl"],
        )
        assert result.exit_code == EXIT_UNSUPPORTED_FEATURE, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2
        names = [json.loads(ln)["target"] for ln in lines]
        assert names == ["kid-tablet", "phone"]
        for ln in lines:
            envelope = json.loads(ln)
            assert envelope["status"] == "error"
            assert envelope["exit_code"] == EXIT_UNSUPPORTED_FEATURE
            assert envelope["error"]["code"] == "unsupported_feature"

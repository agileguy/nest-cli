"""End-to-end test for ``wifi pause @group`` fan-out (FR-6, FR-8a, Phase C).

Phase C lands the real REST implementation for ``wifi pause``. The
fan-out machinery emits one FR-9a envelope per target; both should now
succeed (exit 0) when the underlying REST call returns success.

Reuses the ``fake_googlewifi`` fixture name from ``tests/wifi/conftest.py``
(now an alias for the Phase-B ``fake_foyer_client`` fixture, kept for
backward compatibility). For this batch test the gRPC fixture isn't
exercised — the verb body now hits ``_rest`` instead.
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
from nest_cli.wifi.client import FoyerClient


@pytest.fixture
def patched_foyer_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Short-circuit FoyerClient.__init__ + record _rest calls."""

    def _init(self: FoyerClient, creds: WifiCredentials) -> None:
        import threading as _threading

        self._creds = creds
        self._access_token = None
        self._access_token_expiry = 0.0
        self._onhub_token = None
        self._onhub_token_expiry = 0.0
        self._onhub_token_lock = _threading.Lock()
        self._step1_web_token = None
        self._step1_web_token_expiry = 0.0
        # Pre-fill resolver cache to a deterministic single group id so
        # the fan-out workers don't try to list_groups (PR #9 review fix #1).
        self._resolved_default_group_id = "home-mesh-001"
        self._default_group_lock = _threading.Lock()
        self._rest_session = None

    monkeypatch.setattr(FoyerClient, "__init__", _init)
    calls: list[dict[str, Any]] = []

    def _fake_rest(
        self: FoyerClient,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append({"method": method, "path": path, "json": json, "params": params})
        return None

    monkeypatch.setattr(FoyerClient, "_rest", _fake_rest)
    return calls


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_wifi_creds() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=3,
            type="foyer",
            google_account_email="me@example.com",
            master_token="aas_et/m",  # noqa: S106
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
            refresh_token="1//09abc-DEF",
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
    def test_wifi_pause_at_group_emits_two_success_envelopes(
        self,
        isolated_xdg: Path,
        patched_foyer_client: list[dict[str, Any]],
    ) -> None:
        """``wifi pause @kids-devices`` → two FR-9a envelopes, exit 0."""
        _seed_wifi_creds()
        _write_config_with_group(isolated_xdg)

        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["wifi", "pause", "@kids-devices", "--experimental-wifi", "--jsonl"],
        )
        assert result.exit_code == 0, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2
        envelopes = [json.loads(ln) for ln in lines]
        names = [e["target"] for e in envelopes]
        assert names == ["kid-tablet", "phone"]
        for env in envelopes:
            assert env["status"] == "ok"
            assert env["exit_code"] == 0
            assert env["result"]["action"] == "pause"
        # Two REST calls (one per target)
        assert len(patched_foyer_client) == 2
        for call in patched_foyer_client:
            assert call["path"] == "/v2/groups/home-mesh-001/stationBlocking"
            assert call["json"]["blocked"] == "true"

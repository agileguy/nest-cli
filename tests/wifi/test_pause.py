"""CliRunner tests for ``nest-cli wifi pause`` (FR-WIFI-4).

Coverage:

- Happy path: pause an unpaused client → exit 0, structured envelope.
- Idempotent: pause an already-paused client → exit 0, no error.
- Unknown client_id → exit 4 (family=wifi).
- Network failure during action → exit 3 (family=wifi).
- Error envelope carries family=wifi.
"""

from __future__ import annotations

import json
import sys
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
# Happy path + idempotence (FR-WIFI-4)
# ---------------------------------------------------------------------------


def test_pause_known_client_emits_ok_envelope(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """`wifi pause sta-laptop --experimental-wifi` exits 0 with envelope."""
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
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "client_id": "sta-laptop",
        "action": "pause",
        "result": "ok",
    }


def test_pause_already_paused_client_idempotent(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """FR-WIFI-4 idempotence: pausing an already-paused client returns OK."""
    _seed_wifi_creds()
    runner = CliRunner()
    # ``sta-kid-tablet`` is paused=true in the fixture corpus.
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
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"] == "ok"


def test_pause_passes_correct_args_to_upstream(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """The FoyerClient resolves group_id from client_id and calls pause_device."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-phone", "--experimental-wifi"],
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    pause_calls = [c for c in last.calls if c[0] == "pause_device"]
    assert len(pause_calls) == 1
    name, args, _ = pause_calls[0]
    assert args == ("group-home-001", "sta-phone", True)


# ---------------------------------------------------------------------------
# Unknown client (FR-19 / SRD §11.2 exit 4)
# ---------------------------------------------------------------------------


def test_pause_unknown_client_exits_4(isolated_xdg: Path, fake_googlewifi: type) -> None:
    """Unknown client_id → exit 4 with family=wifi."""
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
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert "sta-no-such-client" in payload["message"]


# ---------------------------------------------------------------------------
# Transport error (SRD §11.2 exit 3)
# ---------------------------------------------------------------------------


def test_pause_network_error_exits_3(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection error during pause → exit 3 (family=wifi)."""

    class _NetworkErrorGoogleWifi:
        last_instance: _NetworkErrorGoogleWifi | None = None

        def __init__(self, refresh_token: str | None = None, **_: Any) -> None:
            type(self).last_instance = self

        async def connect(self) -> bool:
            return True

        async def get_systems(self) -> dict[str, Any]:
            # Same single-client corpus so resolution succeeds.
            return {
                "group-home-001": {
                    "id": "group-home-001",
                    "devices": {"sta-laptop": {"id": "sta-laptop"}},
                }
            }

        async def pause_device(self, system_id: str, device_id: str, pause_state: bool) -> bool:
            raise ConnectionError("DNS resolution failed")

        async def close(self) -> None:
            return None

    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _NetworkErrorGoogleWifi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)

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
    assert result.exit_code == 3, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

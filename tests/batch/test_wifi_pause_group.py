"""End-to-end test for ``wifi pause @group`` fan-out (FR-6, FR-8a).

The wifi side already gates every verb behind ``--experimental-wifi``
(FR-WIFI-0); fan-out wiring preserves the gate (the gate fires before
group resolution so an operator who forgot ``--experimental-wifi`` sees
the same exit-64 hint regardless of whether they passed a group).

Reuses the ``fake_googlewifi`` fixture from ``tests/wifi/conftest.py``
which is auto-discovered by pytest because that conftest lives at the
sibling level. Pytest's conftest discovery walks up directory trees
from the test file; ``tests/batch/`` is a sibling, not a parent, so we
explicitly import the fixture by referencing it via the test fixture
chain — but the simplest path is to add the fixture in the local
conftest.py if needed. For this single test we instead inline a minimal
``fake_googlewifi`` setup so this directory remains self-contained.
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
from nest_cli.cli import cli as cli_root

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "foyer" / "samples"


def _load_fixture(name: str) -> Any:
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


class _FakeGoogleWifi:
    """Minimal googlewifi fake — mirror of ``tests/wifi/conftest.py``."""

    last_instance: _FakeGoogleWifi | None = None

    def __init__(self, refresh_token: str | None = None, **_: Any) -> None:
        self.refresh_token = refresh_token
        self._systems = _load_fixture("groups.json")
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        type(self).last_instance = self

    async def get_systems(self) -> dict[str, Any]:
        return self._systems

    async def close(self) -> None:
        return None

    async def connect(self) -> bool:
        return True

    async def pause_device(self, system_id: str, device_id: str, pause_state: bool) -> bool:
        self.calls.append(("pause_device", (system_id, device_id, pause_state), {}))
        return True


@pytest.fixture
def fake_googlewifi(monkeypatch: pytest.MonkeyPatch) -> type[_FakeGoogleWifi]:
    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _FakeGoogleWifi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)
    return _FakeGoogleWifi


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
            master_token="t",  # noqa: S106
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
    def test_wifi_pause_at_group_emits_two_envelopes(
        self,
        isolated_xdg: Path,
        fake_googlewifi: type[_FakeGoogleWifi],  # noqa: ARG002
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
        names = [json.loads(ln)["target"] for ln in lines]
        assert names == ["kid-tablet", "phone"]
        for ln in lines:
            envelope = json.loads(ln)
            assert envelope["status"] == "ok"
            assert envelope["exit_code"] == 0

"""CliRunner tests for ``nest-cli wifi unpause`` (FR-WIFI-5).

Coverage:

- Happy path: unpause a paused client → exit 0, structured envelope.
- Idempotent: unpause an already-unpaused client → exit 0, no error.
- Unknown client_id → exit 4 (family=wifi).
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


def test_unpause_paused_client_emits_ok_envelope(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """`wifi unpause sta-kid-tablet --experimental-wifi` succeeds."""
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
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "client_id": "sta-kid-tablet",
        "action": "unpause",
        "result": "ok",
    }


def test_unpause_already_unpaused_client_idempotent(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """FR-WIFI-5 idempotence: unpausing an unpaused client returns OK."""
    _seed_wifi_creds()
    runner = CliRunner()
    # ``sta-laptop`` is paused=false in the fixture corpus.
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-laptop", "--experimental-wifi"],
    )
    assert result.exit_code == 0, result.output


def test_unpause_passes_correct_args_to_upstream(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """unpause should call pause_device(..., pause_state=False)."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-laptop", "--experimental-wifi"],
    )
    assert result.exit_code == 0, result.output
    last = fake_googlewifi.last_instance
    assert last is not None
    pause_calls = [c for c in last.calls if c[0] == "pause_device"]
    assert len(pause_calls) == 1
    _, args, _ = pause_calls[0]
    assert args == ("group-home-001", "sta-laptop", False)


def test_unpause_unknown_client_exits_4(
    isolated_xdg: Path, fake_googlewifi: type
) -> None:
    """Unknown client_id → exit 4 with family=wifi."""
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
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"

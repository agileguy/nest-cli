"""CliRunner tests for ``nest-cli wifi unpause`` (FR-WIFI-5, Phase C)."""

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
from nest_cli.cli.wifi_cmd import wifi_group
from nest_cli.wifi.client import FoyerClient


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_v3() -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=3,
            type="foyer",
            google_account_email="me@example.com",
            master_token="aas_et/m",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
            refresh_token="1//09abc-DEF",
        ),
    )


@pytest.fixture
def stub_rest(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
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


def test_unpause_paused_client_succeeds(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: list[dict[str, Any]]
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "unpause"
    assert payload["result"] == "ok"
    assert stub_rest[0]["json"]["blocked"] == "false"


def test_unpause_already_unpaused_client_still_succeeds(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: list[dict[str, Any]]
) -> None:
    """Idempotent — Foyer accepts a re-unblock with no error."""
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-kid-tablet", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    assert len(stub_rest) == 1


def test_unpause_unknown_client_call_still_issues(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: list[dict[str, Any]]
) -> None:
    """Unknown station id still fires the REST call — Foyer is the source of truth.

    A real 404 from Foyer would map to EXIT_NOT_FOUND via _rest's error
    mapping; our stub returns success-with-empty so the test confirms
    the call was made with the operator-supplied id.
    """
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["unpause", "sta-no-such", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    assert stub_rest[0]["json"]["stationId"] == "sta-no-such"

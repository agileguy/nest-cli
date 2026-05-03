"""CliRunner tests for ``nest-cli wifi prioritize`` (FR-WIFI-6, Phase C)."""

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
    """Same pattern as test_pause.py — short-circuit the resolver."""
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
    monkeypatch.setattr(
        FoyerClient,
        "_resolve_default_group_id",
        lambda self: "home-mesh-001",
    )
    return calls


def test_prioritize_default_duration_succeeds(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: list[dict[str, Any]]
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["prioritize", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["client_id"] == "sta-laptop"
    assert payload["duration_minutes"] == 60
    assert stub_rest[0]["method"] == "PUT"
    assert stub_rest[0]["path"] == "/v2/groups/home-mesh-001/prioritizedStation"
    assert stub_rest[0]["json"]["stationId"] == "sta-laptop"
    assert stub_rest[0]["json"]["prioritizationEndTime"].endswith("Z")


def test_prioritize_explicit_duration_passed_through(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: list[dict[str, Any]]
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "30",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["duration_minutes"] == 30


def test_prioritize_at_max_240_minutes_accepted(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: list[dict[str, Any]]
) -> None:
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-laptop",
            "--duration",
            "240",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output


def test_prioritize_unknown_client_call_still_issues(
    isolated_xdg: Path, fake_googlewifi: None, stub_rest: list[dict[str, Any]]
) -> None:
    """Unknown station id still fires the REST call — Foyer maps 404 → exit 4."""
    _seed_v3()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        [
            "prioritize",
            "sta-no-such",
            "--experimental-wifi",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stub_rest[0]["json"]["stationId"] == "sta-no-such"

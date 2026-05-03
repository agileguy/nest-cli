"""CliRunner tests for ``nest-cli wifi pause`` (FR-WIFI-4, Phase C).

Phase C lands the real REST implementation. Tests seed v3 credentials
(carrying a refresh_token) so the CLI verb reaches the FoyerClient
``pause_station`` method, then monkey-patch ``FoyerClient._rest`` to
record the resulting REST call without actually touching the OAuth
chain or HTTP transport.
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
from nest_cli.cli.wifi_cmd import wifi_group
from nest_cli.wifi.client import FoyerClient


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


def _seed_wifi_creds(version: int = 3) -> None:
    save_wifi_credentials(
        default_wifi_credentials_path(),
        WifiCredentials(
            version=version,
            type="foyer",
            google_account_email="me@example.com",
            master_token="aas_et/m",
            android_id="0123456789abcdef",
            issued_at=datetime(2026, 5, 3, tzinfo=UTC),
            refresh_token="1//09abc-DEF" if version == 3 else None,
        ),
    )


@pytest.fixture
def stub_rest(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch FoyerClient._rest to record calls and return success-with-empty.

    Also stubs ``_resolve_default_group_id`` to short-circuit the
    list-groups round-trip; pause/unpause inherit the new dynamic group
    resolution path (see Phase C review fix #1) but the fixture pretends
    the operator has a single mesh group named ``home-mesh-001`` so the
    REST call lands on a real-looking path without each test needing to
    seed a single-group fixture.
    """
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


# ---------------------------------------------------------------------------
# Phase C happy path
# ---------------------------------------------------------------------------


def test_pause_known_client_succeeds_via_rest(
    isolated_xdg: Path,
    fake_googlewifi: None,
    stub_rest: list[dict[str, Any]],
) -> None:
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["client_id"] == "sta-laptop"
    assert payload["action"] == "pause"
    assert payload["result"] == "ok"
    # Exactly one REST call PUT to stationBlocking with blocked=true
    assert len(stub_rest) == 1
    assert stub_rest[0]["method"] == "PUT"
    assert stub_rest[0]["path"] == "/v2/groups/home-mesh-001/stationBlocking"
    assert stub_rest[0]["json"]["blocked"] == "true"


def test_pause_already_paused_client_still_succeeds(
    isolated_xdg: Path,
    fake_googlewifi: None,
    stub_rest: list[dict[str, Any]],
) -> None:
    """Idempotent — Foyer accepts a re-pause without erroring."""
    _seed_wifi_creds()
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-kid-tablet", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    assert len(stub_rest) == 1


def test_pause_v2_credentials_exit_2_with_bootstrap_hint(
    isolated_xdg: Path, fake_googlewifi: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2 creds (no refresh_token) hit the OnHub mint path and exit 2.

    Stub the resolver so the test reaches the OnHub mint path; the
    resolver itself uses the gRPC HomeGraph (works on v2 creds) but
    short-circuiting it keeps the test's intent unchanged: confirm that
    the token-mint failure is what surfaces, not the multi-group fan-out.
    """
    _seed_wifi_creds(version=2)
    # No stub_rest fixture — real _refresh_onhub_access_token runs and
    # refuses immediately because creds.refresh_token is None.
    monkeypatch.setattr(
        FoyerClient,
        "_resolve_default_group_id",
        lambda self: "home-mesh-001",
    )
    runner = CliRunner()
    result = runner.invoke(
        wifi_group,
        ["pause", "sta-laptop", "--experimental-wifi", "--output", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr or result.output)
    assert payload["family"] == "wifi"
    assert "wifi-refresh-bootstrap" in (payload.get("hint") or "")

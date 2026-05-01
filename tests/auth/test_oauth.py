"""Unit tests for ``nest_cli.auth.oauth``.

We mock the upstream ``InstalledAppFlow`` because (a) it would otherwise
open a browser and bind a real port, and (b) CI must never touch Google's
endpoints (SRD §12.2).

Coverage map (FR → test):

- FR-CRED-1 (interactive consent + persisted refresh+access token):
  test_run_oauth_flow_returns_populated_credentials,
  test_run_oauth_flow_uses_sdm_scope.
- FR-CRED-1 port-collision remediation:
  test_run_oauth_flow_port_in_use_exits_2.
- Empty refresh-token edge case (Google re-consent omits it):
  test_run_oauth_flow_no_refresh_token_raises.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from nest_cli.auth import oauth as oauth_mod
from nest_cli.auth.credentials import EXIT_AUTH_ERROR, CredentialError
from nest_cli.auth.oauth import SDM_SCOPE, run_oauth_flow
from nest_cli.auth.types import CamCredentials


def _make_fake_flow_factory(
    *,
    refresh_token: str | None = "refresh-from-google",
    access_token: str = "access-from-google",
    expiry_in_s: int = 3600,
    raise_on_run: Exception | None = None,
) -> tuple[MagicMock, list[dict[str, Any]]]:
    """Build a stand-in for ``InstalledAppFlow.from_client_config``.

    Returns the factory-mock plus a list that captures every kwargs dict
    the test code uses to call ``from_client_config``, so tests can assert
    on scope / client_id / client_secret.
    """
    factory_calls: list[dict[str, Any]] = []
    flow_instance = MagicMock(name="flow")

    if raise_on_run is not None:
        flow_instance.run_local_server.side_effect = raise_on_run

    expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=expiry_in_s)
    creds_mock = MagicMock(name="credentials")
    creds_mock.refresh_token = refresh_token
    creds_mock.token = access_token
    creds_mock.expiry = expiry
    flow_instance.credentials = creds_mock

    def factory(client_config: dict[str, Any], scopes: list[str], **kwargs: Any) -> MagicMock:
        factory_calls.append({"client_config": client_config, "scopes": scopes, **kwargs})
        return flow_instance

    factory_mock = MagicMock(side_effect=factory)
    return factory_mock, factory_calls


def test_run_oauth_flow_returns_populated_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful flow returns a fully-formed ``CamCredentials`` model."""
    factory, _ = _make_fake_flow_factory()
    monkeypatch.setattr(
        oauth_mod.InstalledAppFlow,
        "from_client_config",
        classmethod(lambda cls, *a, **k: factory(*a, **k)),
    )

    result = run_oauth_flow(
        client_id="my-client-id",
        client_secret="my-client-secret",
        project_id="my-gcp-project",
        callback_port=8765,
        open_browser=False,
    )

    assert isinstance(result, CamCredentials)
    assert result.google_cloud_project_id == "my-gcp-project"
    assert result.oauth_client_id == "my-client-id"
    assert result.oauth_client_secret == "my-client-secret"
    assert result.refresh_token == "refresh-from-google"
    assert result.access_token == "access-from-google"
    # expires_at must be tz-aware UTC (FR-22).
    assert result.expires_at.tzinfo is not None


def test_run_oauth_flow_uses_sdm_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flow MUST request only the SDM scope (no Pub/Sub yet — Phase 2)."""
    factory, calls = _make_fake_flow_factory()
    monkeypatch.setattr(
        oauth_mod.InstalledAppFlow,
        "from_client_config",
        classmethod(lambda cls, *a, **k: factory(*a, **k)),
    )

    run_oauth_flow(
        client_id="cid",
        client_secret="csec",
        project_id="proj",
        callback_port=8765,
        open_browser=False,
    )

    assert len(calls) == 1
    assert calls[0]["scopes"] == [SDM_SCOPE]
    cfg = calls[0]["client_config"]
    assert cfg["installed"]["client_id"] == "cid"
    assert cfg["installed"]["client_secret"] == "csec"


def test_run_oauth_flow_port_in_use_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` from ``run_local_server`` is mapped to exit 2 (auth).

    The OAuth flow could not complete, so the operator's terminal state
    is "unauthenticated". The hint names the remediation:
    ``--callback-port <other-port>``.
    """
    factory, _ = _make_fake_flow_factory(raise_on_run=OSError(48, "Address already in use"))
    monkeypatch.setattr(
        oauth_mod.InstalledAppFlow,
        "from_client_config",
        classmethod(lambda cls, *a, **k: factory(*a, **k)),
    )

    with pytest.raises(CredentialError) as exc:
        run_oauth_flow(
            client_id="cid",
            client_secret="csec",
            project_id="proj",
            callback_port=8765,
            open_browser=False,
        )

    assert exc.value.exit_code == EXIT_AUTH_ERROR
    assert "callback-port" in (exc.value.hint or "")


def test_run_oauth_flow_no_refresh_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google sometimes omits the refresh token on re-consent → exit 2."""
    factory, _ = _make_fake_flow_factory(refresh_token=None)
    monkeypatch.setattr(
        oauth_mod.InstalledAppFlow,
        "from_client_config",
        classmethod(lambda cls, *a, **k: factory(*a, **k)),
    )

    with pytest.raises(CredentialError) as exc:
        run_oauth_flow(
            client_id="cid",
            client_secret="csec",
            project_id="proj",
            callback_port=8765,
            open_browser=False,
        )

    assert exc.value.exit_code == EXIT_AUTH_ERROR
    assert "myaccount.google.com" in (exc.value.hint or "")


def test_run_oauth_flow_generic_exception_maps_to_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected upstream error during the flow lands on exit 2."""
    factory, _ = _make_fake_flow_factory(raise_on_run=RuntimeError("operator denied consent"))
    monkeypatch.setattr(
        oauth_mod.InstalledAppFlow,
        "from_client_config",
        classmethod(lambda cls, *a, **k: factory(*a, **k)),
    )

    with pytest.raises(CredentialError) as exc:
        run_oauth_flow(
            client_id="cid",
            client_secret="csec",
            project_id="proj",
            callback_port=8765,
            open_browser=False,
        )

    assert exc.value.exit_code == EXIT_AUTH_ERROR

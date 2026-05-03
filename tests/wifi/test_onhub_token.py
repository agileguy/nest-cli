"""Tests for FoyerClient OnHub-token mint + REST helper + op-poller (Phase C).

Coverage map:

- TestOnHubTokenRefresh covers the two-step OAuth chain:
  * Step 1 calls oauth2/v4/token with the operator's refresh_token.
  * Step 2 calls issuetoken with the OnHub app id + scopes.
  * Successful mint caches the token with the 60s skew window.
  * Subsequent _ensure_onhub_token calls reuse the cached token.
  * Expired cache forces a fresh mint.
  * Missing refresh_token (v2 creds) → EXIT_AUTH_ERROR with bootstrap hint.
  * HTTP 4xx from Step 1 → EXIT_AUTH_ERROR.
  * HTTP 4xx from Step 2 → EXIT_AUTH_ERROR.

- TestRestHelper covers _rest:
  * Issues request to FOYER_REST_BASE + path with bearer auth.
  * 2xx returns parsed JSON; 204 returns None.
  * 401/403 → EXIT_AUTH_ERROR; 404 → EXIT_NOT_FOUND;
    5xx → EXIT_NETWORK_ERROR; other → EXIT_DEVICE_ERROR.
  * Network exceptions → EXIT_NETWORK_ERROR.

- TestWaitForOperation covers _wait_for_operation:
  * Polls until operationState == "DONE" then returns the payload.
  * Raises EXIT_NETWORK_ERROR on timeout.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    StructuredError,
)
from nest_cli.wifi import client as wifi_client_mod
from nest_cli.wifi.client import (
    ACCESS_TOKEN_SKEW_S,
    FOYER_REST_BASE,
    ONHUB_ISSUETOKEN_URL,
    ONHUB_OAUTH2_TOKEN_URL,
    FoyerClient,
)


@pytest.fixture
def v3_creds(make_v2_creds: Any) -> WifiCredentials:
    """A v3 credentials record carrying a refresh token."""
    base = make_v2_creds()
    return WifiCredentials(
        version=3,
        type=base.type,
        google_account_email=base.google_account_email,
        master_token=base.master_token,
        android_id=base.android_id,
        issued_at=base.issued_at,
        refresh_token="1//09abc-DEF_xyz",
    )


@pytest.fixture
def v3_client(monkeypatch: pytest.MonkeyPatch, v3_creds: WifiCredentials) -> FoyerClient:
    """Construct a FoyerClient from v3 creds with extras-import skipped."""

    def _init(self: FoyerClient, creds: WifiCredentials) -> None:
        self._creds = creds
        self._access_token = None
        self._access_token_expiry = 0.0
        self._onhub_token = None
        self._onhub_token_expiry = 0.0
        import threading as _threading

        self._onhub_token_lock = _threading.Lock()
        self._step1_web_token = None
        self._step1_web_token_expiry = 0.0
        self._resolved_default_group_id = None
        self._default_group_lock = _threading.Lock()
        self._rest_session = None

    monkeypatch.setattr(FoyerClient, "__init__", _init)
    return FoyerClient(v3_creds)


@pytest.fixture
def v2_client(monkeypatch: pytest.MonkeyPatch, make_v2_creds: Any) -> FoyerClient:
    """Construct a FoyerClient from v2 creds (no refresh_token)."""

    def _init(self: FoyerClient, creds: WifiCredentials) -> None:
        self._creds = creds
        self._access_token = None
        self._access_token_expiry = 0.0
        self._onhub_token = None
        self._onhub_token_expiry = 0.0
        import threading as _threading

        self._onhub_token_lock = _threading.Lock()
        self._step1_web_token = None
        self._step1_web_token_expiry = 0.0
        self._resolved_default_group_id = None
        self._default_group_lock = _threading.Lock()
        self._rest_session = None

    monkeypatch.setattr(FoyerClient, "__init__", _init)
    return FoyerClient(make_v2_creds())


def _mock_response(status_code: int, json_body: Any = None, text: str = "") -> MagicMock:
    """Build a MagicMock that mimics requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (str(json_body) if json_body is not None else "")
    resp.content = b"{}" if json_body is None and 200 <= status_code < 300 else b"data"
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no JSON")
    return resp


# ---------------------------------------------------------------------------
# OnHub two-step OAuth chain
# ---------------------------------------------------------------------------


class TestOnHubTokenRefresh:
    def test_step1_calls_oauth2_with_refresh_token_grant(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.post.side_effect = [
            _mock_response(200, {"access_token": "ya29.web-token"}),
            _mock_response(200, {"token": "onhub-token", "expiresIn": 3600}),
        ]
        monkeypatch.setattr(v3_client, "_get_rest_session", lambda: session)

        token = v3_client._refresh_onhub_access_token()

        assert token == "onhub-token"
        # Step 1 call args
        step1_call = session.post.call_args_list[0]
        assert step1_call.args[0] == ONHUB_OAUTH2_TOKEN_URL
        assert step1_call.kwargs["data"]["grant_type"] == "refresh_token"
        assert step1_call.kwargs["data"]["refresh_token"] == "1//09abc-DEF_xyz"

    def test_step2_calls_issuetoken_with_onhub_app_id(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.post.side_effect = [
            _mock_response(200, {"access_token": "ya29.web-token"}),
            _mock_response(200, {"token": "onhub-token", "expiresIn": 3600}),
        ]
        monkeypatch.setattr(v3_client, "_get_rest_session", lambda: session)

        v3_client._refresh_onhub_access_token()

        step2_call = session.post.call_args_list[1]
        assert step2_call.args[0] == ONHUB_ISSUETOKEN_URL
        assert step2_call.kwargs["data"]["app_id"] == "com.google.OnHub"
        # Bearer auth uses the Step 1 web token
        assert step2_call.kwargs["headers"]["Authorization"] == ("Bearer ya29.web-token")

    def test_token_cached_with_skew_window(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.post.side_effect = [
            _mock_response(200, {"access_token": "ya29.web"}),
            _mock_response(200, {"token": "onhub", "expiresIn": 3600}),
        ]
        monkeypatch.setattr(v3_client, "_get_rest_session", lambda: session)

        t_before = time.time()
        v3_client._refresh_onhub_access_token()
        t_after = time.time()

        expected_lower = t_before + 3600 - ACCESS_TOKEN_SKEW_S
        expected_upper = t_after + 3600 - ACCESS_TOKEN_SKEW_S
        assert expected_lower <= v3_client._onhub_token_expiry <= expected_upper

    def test_subsequent_calls_reuse_cached_token(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.post.side_effect = [
            _mock_response(200, {"access_token": "ya29.web"}),
            _mock_response(200, {"token": "onhub", "expiresIn": 3600}),
        ]
        monkeypatch.setattr(v3_client, "_get_rest_session", lambda: session)

        v3_client._ensure_onhub_token()
        v3_client._ensure_onhub_token()
        v3_client._ensure_onhub_token()

        # Two POSTs total (one Step 1 + one Step 2), not six.
        assert session.post.call_count == 2

    def test_expired_cache_forces_remint(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.post.side_effect = [
            _mock_response(200, {"access_token": "ya29.web1"}),
            _mock_response(200, {"token": "onhub-1", "expiresIn": 3600}),
            _mock_response(200, {"access_token": "ya29.web2"}),
            _mock_response(200, {"token": "onhub-2", "expiresIn": 3600}),
        ]
        monkeypatch.setattr(v3_client, "_get_rest_session", lambda: session)

        first = v3_client._ensure_onhub_token()
        # Force expiry
        v3_client._onhub_token_expiry = time.time() - 1.0
        second = v3_client._ensure_onhub_token()

        assert first == "onhub-1"
        assert second == "onhub-2"
        assert session.post.call_count == 4

    def test_missing_refresh_token_raises_auth_error(self, v2_client: FoyerClient) -> None:
        with pytest.raises(StructuredError) as exc_info:
            v2_client._refresh_onhub_access_token()
        err = exc_info.value
        assert err.code == EXIT_AUTH_ERROR
        assert err.family == "wifi"
        assert "wifi-refresh-bootstrap" in (err.hint or "")

    def test_step1_http_4xx_surfaces_as_auth_error(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.post.side_effect = [
            _mock_response(401, text="invalid_grant"),
        ]
        monkeypatch.setattr(v3_client, "_get_rest_session", lambda: session)

        with pytest.raises(StructuredError) as exc_info:
            v3_client._refresh_onhub_access_token()
        assert exc_info.value.code == EXIT_AUTH_ERROR
        assert "wifi-refresh-bootstrap" in (exc_info.value.hint or "")

    def test_step2_http_4xx_surfaces_as_auth_error(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.post.side_effect = [
            _mock_response(200, {"access_token": "ya29.web"}),
            _mock_response(403, text="permission denied"),
        ]
        monkeypatch.setattr(v3_client, "_get_rest_session", lambda: session)

        with pytest.raises(StructuredError) as exc_info:
            v3_client._refresh_onhub_access_token()
        assert exc_info.value.code == EXIT_AUTH_ERROR


# ---------------------------------------------------------------------------
# REST helper
# ---------------------------------------------------------------------------


class TestRestHelper:
    def _arm(
        self,
        client: FoyerClient,
        monkeypatch: pytest.MonkeyPatch,
        response: MagicMock,
    ) -> MagicMock:
        """Stub _ensure_onhub_token + return the underlying session mock."""
        monkeypatch.setattr(client, "_ensure_onhub_token", lambda: "tok")
        session = MagicMock()
        session.request.return_value = response
        monkeypatch.setattr(client, "_get_rest_session", lambda: session)
        return session

    def test_rest_issues_request_to_foyer_base(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = self._arm(v3_client, monkeypatch, _mock_response(200, {"ok": True}))
        v3_client._rest("GET", "/v2/groups/g1/stations")
        call = session.request.call_args
        assert call.args[0] == "GET"
        assert call.args[1] == FOYER_REST_BASE + "/v2/groups/g1/stations"

    def test_rest_sets_authorization_header_from_onhub_token(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = self._arm(v3_client, monkeypatch, _mock_response(200, {"ok": True}))
        v3_client._rest("GET", "/v2/groups/g1/stations")
        headers = session.request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok"

    def test_rest_returns_parsed_json_for_2xx(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(v3_client, monkeypatch, _mock_response(200, {"stations": []}))
        result = v3_client._rest("GET", "/v2/groups/g1/stations")
        assert result == {"stations": []}

    def test_rest_returns_none_for_empty_2xx(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = MagicMock()
        empty.status_code = 204
        empty.content = b""
        empty.text = ""
        self._arm(v3_client, monkeypatch, empty)
        result = v3_client._rest("POST", "/v2/groups/g1/reboot", json={})
        assert result is None

    def test_rest_401_maps_to_auth_error(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(v3_client, monkeypatch, _mock_response(401, text="bad token"))
        with pytest.raises(StructuredError) as exc_info:
            v3_client._rest("GET", "/v2/groups/g1/stations")
        assert exc_info.value.code == EXIT_AUTH_ERROR
        assert exc_info.value.family == "wifi"

    def test_rest_404_maps_to_not_found(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(v3_client, monkeypatch, _mock_response(404, text="no such"))
        with pytest.raises(StructuredError) as exc_info:
            v3_client._rest("GET", "/v2/groups/no-such")
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_rest_500_maps_to_network_error(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(v3_client, monkeypatch, _mock_response(503, text="Foyer down"))
        with pytest.raises(StructuredError) as exc_info:
            v3_client._rest("GET", "/v2/groups/g1/stations")
        assert exc_info.value.code == EXIT_NETWORK_ERROR

    def test_rest_unexpected_status_maps_to_device_error(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(v3_client, monkeypatch, _mock_response(418, text="teapot"))
        with pytest.raises(StructuredError) as exc_info:
            v3_client._rest("GET", "/v2/groups/g1/stations")
        assert exc_info.value.code == EXIT_DEVICE_ERROR

    def test_rest_connection_error_maps_to_network_error(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        monkeypatch.setattr(v3_client, "_ensure_onhub_token", lambda: "tok")
        session = MagicMock()
        session.request.side_effect = requests.ConnectionError("DNS failed")
        monkeypatch.setattr(v3_client, "_get_rest_session", lambda: session)

        with pytest.raises(StructuredError) as exc_info:
            v3_client._rest("GET", "/v2/groups/g1/stations")
        assert exc_info.value.code == EXIT_NETWORK_ERROR


# ---------------------------------------------------------------------------
# Async-operation poller
# ---------------------------------------------------------------------------


class TestWaitForOperation:
    def test_polls_until_done_then_returns_payload(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Three poll responses: pending, pending, DONE.
        responses = [
            {"operationState": "PENDING"},
            {"operationState": "RUNNING"},
            {"operationState": "DONE", "result": {"download_mbps": 900}},
        ]
        rest_calls: list[tuple[str, str]] = []

        def fake_rest(self: FoyerClient, method: str, path: str, **_: Any) -> Any:
            rest_calls.append((method, path))
            return responses.pop(0)

        monkeypatch.setattr(FoyerClient, "_rest", fake_rest)
        # Make sleep instant
        monkeypatch.setattr(wifi_client_mod.time, "sleep", lambda s: None)

        payload = v3_client._wait_for_operation("op-123", timeout_s=60.0)
        assert payload["operationState"] == "DONE"
        assert payload["result"]["download_mbps"] == 900
        assert all(p == "/v2/operations/op-123" for _, p in rest_calls)

    def test_timeout_raises_network_error(
        self, v3_client: FoyerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Always pending; we control time.time so the deadline trips
        # immediately on the first iteration.
        def fake_rest(self: FoyerClient, method: str, path: str, **_: Any) -> Any:
            return {"operationState": "PENDING"}

        monkeypatch.setattr(FoyerClient, "_rest", fake_rest)
        monkeypatch.setattr(wifi_client_mod.time, "sleep", lambda s: None)
        # Force time.time to advance past the deadline on second call.
        ticks = [1000.0, 1000.0, 9999.0]

        def fake_time() -> float:
            return ticks.pop(0) if ticks else 9999.0

        monkeypatch.setattr(wifi_client_mod.time, "time", fake_time)

        with pytest.raises(StructuredError) as exc_info:
            v3_client._wait_for_operation("op-123", timeout_s=10.0)
        assert exc_info.value.code == EXIT_NETWORK_ERROR
        assert exc_info.value.family == "wifi"

"""Shared fixtures for the e2e CLI tests.

The e2e tier invokes the real Click commands via ``CliRunner.invoke`` and
mocks at two seams:

1. ``FoyerClient.__init__`` — bypassed via ``_patch_skip_extras_check``
   so the gpsoauth/grpc/ghome-foyer-api optional-extra import probe never
   runs (cam-only installs would otherwise exit 5 on every wifi verb).
2. ``FoyerClient._rest`` — replaced with a recorder that captures
   ``(method, path, json, params)`` tuples and serves canned responses.

Tests then seed credentials via ``save_wifi_credentials`` so the verb's
``_load_wifi_creds_or_exit`` path finds a valid file and the OnHub
access-token mint code can be skipped (the recorder feeds the responses
directly without the real chain ever running).

Fixtures provided
-----------------

- ``isolated_xdg``       — point ``XDG_CONFIG_HOME`` at a tmp dir.
- ``seed_v3_creds``      — write a v3 ``credentials-wifi.json`` (with
                           refresh_token); returns the seeded record.
- ``seed_v2_creds``      — write a v2 ``credentials-wifi.json`` (no
                           refresh_token); returns the seeded record.
- ``patch_foyer_init``   — bypass the optional-extra import probe.
- ``stub_rest``          — record FoyerClient._rest calls + serve canned.
- ``force_tty``          — monkeypatch ``_stdin_is_tty`` to True/False.
- ``runner``             — a fresh ``CliRunner`` instance.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
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
from nest_cli.wifi.client import FoyerClient

# ---------------------------------------------------------------------------
# Default seeded values (deterministic across all e2e tests)
# ---------------------------------------------------------------------------

_DEFAULT_EMAIL = "operator@example.com"
_DEFAULT_MASTER_TOKEN = "aas_et/dummy-master-token"  # noqa: S105 - test fixture
_DEFAULT_ANDROID_ID = "0123456789abcdef"
_DEFAULT_ISSUED_AT = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
_DEFAULT_REFRESH_TOKEN = "1//09abc-DEF_xyz123"  # noqa: S105 - test fixture


# ---------------------------------------------------------------------------
# CliRunner
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Return a fresh ``CliRunner`` per test (isolated stdin/stdout)."""
    return CliRunner()


# ---------------------------------------------------------------------------
# XDG isolation — ensures every test gets a private credentials dir
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``XDG_CONFIG_HOME`` at a writable tmp dir.

    All wifi/auth verbs route credentials through
    ``default_wifi_credentials_path()`` which honours ``XDG_CONFIG_HOME``;
    isolating per-test prevents one test's seeded creds from leaking into
    the next.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "nest-cli"


# ---------------------------------------------------------------------------
# Credential seeding helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def seed_v3_creds(isolated_xdg: Path) -> Callable[..., WifiCredentials]:
    """Factory: write a v3 credentials-wifi.json and return the record.

    Call as ``creds = seed_v3_creds()`` for defaults, or override fields:
    ``creds = seed_v3_creds(google_account_email="custom@example.com")``.
    """

    def _seed(
        *,
        google_account_email: str = _DEFAULT_EMAIL,
        master_token: str = _DEFAULT_MASTER_TOKEN,
        android_id: str = _DEFAULT_ANDROID_ID,
        issued_at: datetime = _DEFAULT_ISSUED_AT,
        refresh_token: str = _DEFAULT_REFRESH_TOKEN,
    ) -> WifiCredentials:
        creds = WifiCredentials(
            version=3,
            type="foyer",
            google_account_email=google_account_email,
            master_token=master_token,
            android_id=android_id,
            issued_at=issued_at,
            refresh_token=refresh_token,
        )
        save_wifi_credentials(default_wifi_credentials_path(), creds)
        return creds

    return _seed


@pytest.fixture
def seed_v2_creds(isolated_xdg: Path) -> Callable[..., WifiCredentials]:
    """Factory: write a v2 credentials-wifi.json (no refresh_token)."""

    def _seed(
        *,
        google_account_email: str = _DEFAULT_EMAIL,
        master_token: str = _DEFAULT_MASTER_TOKEN,
        android_id: str = _DEFAULT_ANDROID_ID,
        issued_at: datetime = _DEFAULT_ISSUED_AT,
    ) -> WifiCredentials:
        creds = WifiCredentials(
            version=2,
            type="foyer",
            google_account_email=google_account_email,
            master_token=master_token,
            android_id=android_id,
            issued_at=issued_at,
        )
        save_wifi_credentials(default_wifi_credentials_path(), creds)
        return creds

    return _seed


# ---------------------------------------------------------------------------
# FoyerClient init bypass — skips the optional-extra import probe
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_foyer_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``FoyerClient.__init__`` with a no-op extras-check stub.

    The real ``__init__`` lazy-imports gpsoauth + grpc + ghome_foyer_api
    so a cam-only install gets a clean exit-5. In the e2e suite we want
    the verb to reach the ``_rest`` seam (which we mock separately), not
    bail out at construction time. This fixture replicates the state the
    real ``__init__`` would set up, sans the import probe.
    """

    def _init(self: FoyerClient, creds: WifiCredentials) -> None:
        self._creds = creds
        self._access_token = None
        self._access_token_expiry = 0.0
        self._onhub_token = None
        self._onhub_token_expiry = 0.0
        self._onhub_token_lock = threading.Lock()
        self._rest_session = None

    monkeypatch.setattr(FoyerClient, "__init__", _init)


# ---------------------------------------------------------------------------
# REST seam recorder — captures method/path/json/params, serves canned data
# ---------------------------------------------------------------------------


class RestRecorder:
    """Records each ``FoyerClient._rest`` call and serves canned responses.

    Tests register canned responses keyed by ``(method, path)`` (exact
    match, or prefix match with a trailing ``*``), then assert against
    ``recorder.calls`` after invoking the verb. Unmatched calls return
    ``None`` (success-with-empty-body).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: dict[str, Any] = {}

    def register(self, method: str, path: str, response: Any) -> None:
        """Stash a canned response for ``method path``.

        Use a trailing ``*`` on ``path`` for prefix matching.
        """
        self._responses[f"{method} {path}"] = response

    def __call__(
        self,
        client: FoyerClient,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "json": json, "params": params})
        key = f"{method} {path}"
        if key in self._responses:
            return self._responses[key]
        for k, v in self._responses.items():
            if k.endswith("*") and key.startswith(k[:-1]):
                return v
        return None


@pytest.fixture
def stub_rest(monkeypatch: pytest.MonkeyPatch, patch_foyer_init: None) -> RestRecorder:
    """Patch FoyerClient._rest to a RestRecorder; auto-bypasses init probe.

    Depends on ``patch_foyer_init`` so tests don't need to declare both.
    Returns the recorder so tests can register canned responses and
    assert against ``recorder.calls`` after invocation.
    """
    recorder = RestRecorder()

    def _rest_proxy(
        self: FoyerClient,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return recorder(self, method, path, json=json, params=params)

    monkeypatch.setattr(FoyerClient, "_rest", _rest_proxy)
    return recorder


# ---------------------------------------------------------------------------
# TTY simulation — reboot verbs branch on ``_stdin_is_tty()``
# ---------------------------------------------------------------------------


@pytest.fixture
def force_tty(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Factory: monkeypatch ``wifi_cmd._stdin_is_tty`` to a bool.

    Call as ``force_tty(True)`` to simulate a tty for reboot prompts, or
    ``force_tty(False)`` for the non-tty branch. The reboot verbs in
    ``wifi_cmd.py`` indirect through this helper so CliRunner (which sets
    ``isatty=False`` unconditionally) can be overridden test-by-test.
    """

    def _set(value: bool) -> None:
        monkeypatch.setattr("nest_cli.cli.wifi_cmd._stdin_is_tty", lambda: value)

    return _set


# ---------------------------------------------------------------------------
# gRPC seam (read verbs) — fan corpus into FoyerClient._fetch_systems
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_fetch_systems(monkeypatch: pytest.MonkeyPatch, patch_foyer_init: None) -> None:
    """Make ``FoyerClient._fetch_systems`` return the test corpus.

    Read verbs (list groups, list points, point-health) route through
    ``_fetch_systems`` rather than ``_rest``. The corpus lives at
    ``tests/fixtures/foyer/samples/groups.json`` and matches the legacy
    googlewifi shape that the model classmethods consume unchanged.
    """
    import json as _json

    fixtures_dir = Path(__file__).parent.parent / "fixtures" / "foyer" / "samples"
    systems = _json.loads((fixtures_dir / "groups.json").read_text(encoding="utf-8"))

    def _fetch(self: FoyerClient) -> dict[str, dict[str, Any]]:
        return systems

    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)


@pytest.fixture
def rotated_fetch_systems(monkeypatch: pytest.MonkeyPatch, patch_foyer_init: None) -> None:
    """Make ``_fetch_systems`` return a list (wrong shape — SRD §3.2.3)."""

    def _fetch(self: FoyerClient) -> Any:
        return ["wrong", "shape"]

    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)


@pytest.fixture
def network_error_fetch_systems(monkeypatch: pytest.MonkeyPatch, patch_foyer_init: None) -> None:
    """Make ``_fetch_systems`` raise ConnectionError (network failure path)."""

    def _fetch(self: FoyerClient) -> Any:
        raise ConnectionError("DNS resolution failed")

    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)

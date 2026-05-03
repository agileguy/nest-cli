"""Shared fixtures for the wifi-side test modules (Phase B, 2026-05-03).

The Phase B FoyerClient talks to Foyer via gpsoauth + gRPC instead of
``googlewifi``. Tests mock the ``_fetch_systems`` method on
``FoyerClient`` directly — that's the single seam between credential-mint
+ transport and the legacy googlewifi-shaped dict the model classmethods
consume. By patching at that seam we avoid having to fake gpsoauth and
the gRPC stack on every test.

Fixtures:

- ``fake_foyer_client``     — patches ``FoyerClient.__init__`` to skip the
                              optional-extra import check + makes
                              ``_fetch_systems`` return the existing
                              ``tests/fixtures/foyer/samples/groups.json``
                              corpus. Tests construct
                              ``FoyerClient(_make_v2_creds())`` normally.
- ``empty_foyer_client``    — same patching but ``_fetch_systems`` returns
                              ``{}`` (account with no mesh groups).
- ``missing_extras``        — forces the optional-extra import to raise
                              ``ImportError`` so we can test the exit-5
                              missing-extra path.
- ``rotated_foyer_client``  — ``_fetch_systems`` returns a list (wrong
                              shape, simulating SRD §3.2.3 rotation).
- ``network_error_foyer_client`` — ``_fetch_systems`` raises
                              ConnectionError (network failure path).
"""

from __future__ import annotations

import builtins
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.wifi import client as wifi_client_mod
from nest_cli.wifi.client import FoyerClient

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "foyer" / "samples"


def _load_fixture(name: str) -> Any:
    """Read a JSON fixture from the shared foyer corpus."""
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _make_v2_creds(
    *,
    google_account_email: str = "operator@example.com",
    master_token: str = "aas_et/dummy-master-token",  # noqa: S107 - fixture
    android_id: str = "0123456789abcdef",
) -> WifiCredentials:
    """Construct a v2 WifiCredentials suitable for FoyerClient(creds)."""
    return WifiCredentials(
        version=2,
        type="foyer",
        google_account_email=google_account_email,
        master_token=master_token,
        android_id=android_id,
        issued_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
    )


def _patch_skip_extras_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch FoyerClient.__init__ to skip the optional-extra import probe.

    The real ``__init__`` lazy-imports gpsoauth + grpc + ghome_foyer_api so
    a cam-only install gets a clean exit-5. Tests usually have those
    installed (they're in dev-deps via uv), but the import probe also
    creates an unnecessary side-effect for unit tests that never actually
    talk to gRPC. We replace the probe with a no-op, then store the same
    state the original __init__ does.
    """

    def _init(
        self: FoyerClient,
        creds: WifiCredentials,
    ) -> None:
        self._creds = creds
        self._access_token = None
        self._access_token_expiry = 0.0

    monkeypatch.setattr(FoyerClient, "__init__", _init)


@pytest.fixture
def make_v2_creds() -> Any:
    """Expose the ``_make_v2_creds`` factory to tests as a fixture."""
    return _make_v2_creds


@pytest.fixture
def fake_foyer_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``FoyerClient._fetch_systems`` return the test corpus.

    Tests construct ``FoyerClient(_make_v2_creds())`` then call read
    methods (``list_groups``, ``list_points``, etc.). Those methods route
    through ``_fetch_systems``, which now returns the same fixture dict
    the old fake produced. The legacy classmethods consume it unchanged.
    """
    _patch_skip_extras_check(monkeypatch)
    systems = _load_fixture("groups.json")

    def _fetch(self: FoyerClient) -> dict[str, dict[str, Any]]:
        return systems

    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)


@pytest.fixture
def empty_foyer_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_fetch_systems`` returns an empty dict (no mesh groups)."""
    _patch_skip_extras_check(monkeypatch)

    def _fetch(self: FoyerClient) -> dict[str, dict[str, Any]]:
        return {}

    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)


@pytest.fixture
def rotated_foyer_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_fetch_systems`` returns a list (wrong shape — SRD §3.2.3)."""
    _patch_skip_extras_check(monkeypatch)

    def _fetch(self: FoyerClient) -> Any:
        # Simulate upstream-shape rotation; FoyerClient.list_groups
        # iterates `.items()` which fails on a list and surfaces as
        # exit 1.
        return ["this", "is", "the", "wrong", "shape"]

    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)


@pytest.fixture
def network_error_foyer_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_fetch_systems`` raises ConnectionError (network failure)."""
    _patch_skip_extras_check(monkeypatch)

    def _fetch(self: FoyerClient) -> Any:
        raise ConnectionError("DNS resolution failed")

    monkeypatch.setattr(FoyerClient, "_fetch_systems", _fetch)


@pytest.fixture
def missing_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``import gpsoauth`` (or grpc, ghome_foyer_api) to raise.

    Used to verify the ``FoyerClient.__init__`` exit-5 path when the
    operator's install lacks the ``[wifi]`` extra. Replaces
    ``builtins.__import__`` because the lazy-import path inside
    ``__init__`` runs at construction time and we want the very next
    ``FoyerClient(creds)`` call to fail.
    """
    monkeypatch.delitem(sys.modules, "gpsoauth", raising=False)
    monkeypatch.delitem(sys.modules, "grpc", raising=False)
    monkeypatch.delitem(sys.modules, "ghome_foyer_api", raising=False)
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("gpsoauth", "grpc") or name.startswith("ghome_foyer_api"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


# ---------------------------------------------------------------------------
# Backward-compat fixture aliases — older tests reference these names but
# the Phase B replacement is functionally equivalent. Aliasing keeps the
# action-verb test suites running without per-file rewrites of fixture
# parameter names.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_googlewifi(fake_foyer_client: None) -> None:
    """Phase B compat alias for ``fake_foyer_client``.

    The action-verb test suites still reference ``fake_googlewifi`` as a
    fixture parameter; the new client makes the fixture a no-op for those
    suites (the action verbs raise exit-5 before ever hitting the fetch
    path) but keeping the fixture name avoids per-test signature churn.
    """
    return None


@pytest.fixture
def empty_googlewifi(empty_foyer_client: None) -> None:
    """Phase B compat alias for ``empty_foyer_client``."""
    return None


@pytest.fixture
def missing_googlewifi(missing_extras: None) -> None:
    """Phase B compat alias for ``missing_extras``."""
    return None


# ---------------------------------------------------------------------------
# Helpers used by tests that need to access ``wifi_client_mod`` constants
# (e.g. action verb tests asserting on the ``_PHASE_C_HINT`` text).
# ---------------------------------------------------------------------------


@pytest.fixture
def phase_c_hint_substring() -> str:
    """Return a stable substring of the Phase-B unsupported-feature hint.

    Lets action-verb tests assert that the exit-5 hint text references the
    Phase-C deferral without being brittle to wording tweaks.
    """
    # Pull from the live module so a future rewording auto-propagates.
    return "Phase B" if "Phase B" in wifi_client_mod._PHASE_C_HINT else "deferred"

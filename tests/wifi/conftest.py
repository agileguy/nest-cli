"""Shared fixtures for the wifi-side test modules.

The FoyerClient lazy-imports ``googlewifi`` inside ``__init__``. Tests
inject a fake ``GoogleWifi`` class via ``sys.modules`` BEFORE the
FoyerClient is constructed; that fake reads the on-disk fixture
corpus under ``tests/fixtures/foyer/samples/`` and emits the same
shape the real upstream library would.

This conftest does NOT touch the real ``googlewifi`` or
``glocaltokens`` packages — the tests run in any environment, including
ones where the optional ``[wifi]`` extra is uninstalled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "foyer" / "samples"


def _load_fixture(name: str) -> Any:
    """Read a JSON fixture from the shared foyer corpus."""
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


class _FakeGoogleWifi:
    """Async test double for ``googlewifi.GoogleWifi``.

    Mirrors the surface ``FoyerClient`` actually calls. The fake reads
    the fixture corpus on construction and replays it on demand. The
    ``refresh_token`` constructor arg is accepted but unused.

    Phase 3A surface: ``get_systems`` + ``close``.
    Phase 3B surface (action verbs): ``connect``, ``pause_device``,
    ``prioritize_device``. Each action method records its call args
    on ``self.calls`` for spy-style assertion in tests.
    """

    # Class-level call recorder so tests can assert against the LAST
    # constructed instance even though each FoyerClient method spins
    # up a fresh GoogleWifi internally. Tests reset this in fixtures.
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

    # ------------------------------------------------------------------
    # Phase 3B action surface (FR-WIFI-4..7)
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Real upstream gates every action method behind ``connect()``.

        Returns True so the action method's body executes during tests.
        """
        return True

    async def pause_device(self, system_id: str, device_id: str, pause_state: bool) -> bool:
        """Spy that records the call and returns success.

        Records ``("pause_device", (system_id, device_id, pause_state), {})``
        on ``self.calls`` so tests can assert the FoyerClient passed the
        right args. Returns True (mirrors upstream's "operationState ==
        CREATED" success path).
        """
        self.calls.append(("pause_device", (system_id, device_id, pause_state), {}))
        return True

    async def prioritize_device(
        self, system_id: str, device_id: str, duration_hours: int = 1
    ) -> bool:
        """Spy that records the call and returns success."""
        self.calls.append(
            (
                "prioritize_device",
                (system_id, device_id, duration_hours),
                {},
            )
        )
        return True


@pytest.fixture
def fake_googlewifi(monkeypatch: pytest.MonkeyPatch) -> type[_FakeGoogleWifi]:
    """Inject ``_FakeGoogleWifi`` as ``googlewifi.GoogleWifi``.

    Replaces ``sys.modules['googlewifi']`` with a module shim whose
    ``GoogleWifi`` attribute is the fake. Any prior real-package import
    is shadowed for the duration of the test (monkeypatch reverses on
    teardown). ``FoyerClient.__init__`` performs ``from googlewifi
    import GoogleWifi``, so the lazy-import path picks up our fake.
    """
    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _FakeGoogleWifi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)
    return _FakeGoogleWifi


@pytest.fixture
def empty_googlewifi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake ``googlewifi`` whose ``get_systems`` returns ``{}``."""

    class _EmptyGoogleWifi(_FakeGoogleWifi):
        async def get_systems(self) -> dict[str, Any]:
            return {}

    fake_module = type(sys)("googlewifi")
    fake_module.GoogleWifi = _EmptyGoogleWifi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googlewifi", fake_module)


@pytest.fixture
def missing_googlewifi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``import googlewifi`` to raise ``ImportError``.

    Used to verify the ``FoyerClient.__init__`` exit-5 path when the
    operator's install lacks the ``[wifi]`` extra. We replace
    ``builtins.__import__`` because ``sys.modules['googlewifi'] = None``
    is not enough — Python's import machinery raises ``ModuleNotFoundError``
    on a None-valued module entry only on the FIRST import attempt; a
    cached "module under construction" sentinel can still be reused.
    Replacing the import hook is the unambiguous path.
    """
    import builtins

    monkeypatch.delitem(sys.modules, "googlewifi", raising=False)
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "googlewifi" or name.startswith("googlewifi."):
            raise ImportError("No module named 'googlewifi'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

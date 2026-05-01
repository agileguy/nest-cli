"""Click command modules for ``nest-cli``.

Engineer B owns the root ``main`` Click group wiring inside this package.
Engineer A's ``auth`` subgroup lives in ``nest_cli.cli.auth_cmd`` and is
imported by Engineer B's root group at wire-up time.

This ``__init__`` is intentionally empty so individual ``*_cmd`` modules
remain independently importable without triggering side effects.
"""

from __future__ import annotations

__all__: list[str] = []

"""Wifi-side surface for nest-cli (SRD §3.2 / §5.4 / §10.6-10.8).

Public re-exports from this package:

- ``WifiGroup`` / ``WifiPoint`` / ``WifiClient`` — pydantic models that mirror
  SRD §10.6 / §10.7 / §10.8 exactly.
- ``FoyerClient`` — thin sync wrapper around ``googlewifi.GoogleWifi``.

This package depends on the optional ``[wifi]`` install extra
(``googlewifi``, ``glocaltokens``); ``FoyerClient`` lazy-imports those so
that an operator on a cam-only install never pays the import cost. The
extras-missing path is mapped to ``StructuredError(EXIT_UNSUPPORTED_FEATURE,
family="wifi")`` with a hint pointing at ``pip install 'nest-cli[wifi]'``.
"""

from __future__ import annotations

from nest_cli.wifi.types import WifiClient, WifiGroup, WifiPoint

__all__ = ["WifiClient", "WifiGroup", "WifiPoint"]

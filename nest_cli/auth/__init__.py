"""Cam-side and wifi-side authentication: OAuth + Foyer credentials I/O.

Public re-exports:

- ``CamCredentials``       — Pydantic model for ``credentials-cam.json``.
- ``WifiCredentials``      — Pydantic model for ``credentials-wifi.json``.
- ``CredentialError``      — cam auth-layer exception with ``exit_code``.
- ``WifiCredentialError``  — wifi auth-layer exception with ``exit_code``.

Note on imports
---------------

The Click ``auth`` subgroup lives at ``nest_cli.cli.auth_cmd:auth_group``,
not in this package. The root CLI module imports from there directly.
We do not re-export ``auth_group`` here because doing so would
create a circular import (``nest_cli.auth`` → ``nest_cli.cli.auth_cmd`` →
``nest_cli.auth``).
"""

from __future__ import annotations

from nest_cli.auth.credentials import CredentialError
from nest_cli.auth.types import CamCredentials
from nest_cli.auth.wifi_credentials import WifiCredentialError
from nest_cli.auth.wifi_types import WifiCredentials

__all__ = [
    "CamCredentials",
    "CredentialError",
    "WifiCredentialError",
    "WifiCredentials",
]

"""Cam-side authentication: OAuth flow + on-disk credentials I/O.

Public re-exports:

- ``CamCredentials`` — Pydantic model for ``credentials-cam.json``.
- ``CredentialError`` — auth-layer exception with an SRD §11.1 ``exit_code``.

Note on imports
---------------

The Click ``auth`` subgroup lives at ``nest_cli.cli.auth_cmd:auth_group``,
not in this package. Engineer B's root CLI module imports from there
directly. We do not re-export ``auth_group`` here because doing so would
create a circular import (``nest_cli.auth`` → ``nest_cli.cli.auth_cmd`` →
``nest_cli.auth``).
"""

from __future__ import annotations

from nest_cli.auth.credentials import CredentialError
from nest_cli.auth.types import CamCredentials

__all__ = ["CamCredentials", "CredentialError"]

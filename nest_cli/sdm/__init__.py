"""Smart Device Management (SDM) API client and data model.

Public surface:

- ``SdmClient`` — thin REST wrapper around ``smartdevicemanagement.googleapis.com``
  that auto-refreshes the OAuth access token on 401.
- ``Camera`` / ``CameraTrait`` — Pydantic records mirroring SRD §10.1.
"""

from __future__ import annotations

from nest_cli.sdm.client import SdmClient
from nest_cli.sdm.types import Camera, CameraTrait

__all__ = ["Camera", "CameraTrait", "SdmClient"]

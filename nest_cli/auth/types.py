"""Pydantic models for cam-side credentials (SDM OAuth).

The on-disk schema mirrors SRD FR-CRED-3 exactly; ``extra="forbid"`` is the
mechanism that turns "unknown additional keys" into a config-validation error
(exit 6, per SRD §11.1).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CamCredentials(BaseModel):
    """On-disk shape of ``credentials-cam.json`` (FR-CRED-3).

    Field constraints are encoded in the schema so that ``model_validate`` is
    the single source of truth for "is this credentials file usable". The
    ``version`` field is bounded to ``1`` for v0.1.0; if/when a v2 layout is
    introduced, the bound is widened and a migration helper is added.

    Note: ``extra="forbid"`` means unknown keys raise ``ValidationError``,
    which the credentials loader maps to exit 6 (FR-CRED-3).
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(..., ge=1, le=1)
    type: str = Field(..., pattern="^oauth$")
    google_cloud_project_id: str = Field(..., min_length=1)
    oauth_client_id: str = Field(..., min_length=1)
    oauth_client_secret: str = Field(..., min_length=1)
    refresh_token: str = Field(..., min_length=1)
    access_token: str = Field(..., min_length=1)
    expires_at: datetime

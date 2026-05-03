"""Pydantic models for wifi-side credentials (Foyer master token).

The on-disk schema mirrors SRD FR-CRED-8 exactly; ``extra="forbid"`` is
the mechanism that turns "unknown additional keys" into a
config-validation error (exit 6, per SRD §11.1).

Distinct from ``CamCredentials`` because the cam side carries an OAuth
refresh token + access token + GCP project metadata, while the wifi side
carries a long-lived Foyer master token + Google account email. SRD §6.1
+ Decision 8 explicitly require the two credential families to live in
separate files for separate-blast-radius reasons (§4.7).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class WifiCredentials(BaseModel):
    """On-disk shape of ``credentials-wifi.json`` (FR-CRED-8).

    Field constraints are encoded in the schema so ``model_validate`` is
    the single source of truth for "is this credentials file usable".
    The ``version`` field is bounded to ``1`` for v0.3.0; if/when a v2
    layout is introduced, the bound is widened and a migration helper
    is added.

    ``extra="forbid"`` means unknown keys raise ``ValidationError``,
    which the credentials loader maps to exit 6 (FR-CRED-8).
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(..., ge=1, le=1)
    type: str = Field(..., pattern="^foyer$")
    google_account_email: str = Field(..., min_length=3)
    master_token: str = Field(..., min_length=1)
    issued_at: datetime

    @field_serializer("issued_at", when_used="json")
    def _serialize_issued_at(self, dt: datetime) -> str:
        """Render ``issued_at`` as RFC 3339 UTC with the literal ``Z`` suffix.

        Pydantic v2's default JSON datetime serializer emits ``+00:00``;
        SRD FR-22 mandates the literal ``Z`` form. Apply explicitly so
        the on-disk JSON exactly matches the over-the-wire format used
        by ``auth status --json``.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

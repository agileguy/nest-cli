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

    Phase B (v2): ``android_id`` is required. The Foyer access-token mint
    path (``gpsoauth.perform_oauth``) needs a real Android ``android_id``
    in addition to the master token; we persist it alongside so subsequent
    invocations don't have to re-prompt. v1 files (no ``android_id``) fail
    schema validation; the loader maps that to a config-error exit with a
    hint pointing at ``auth wifi-setup --overwrite``.

    Phase C (v3): adds optional ``refresh_token``. The action verbs
    (pause/prioritize/speedtest/reboot/...) hit Foyer REST endpoints at
    ``/v2/groups/...`` that reject the gpsoauth-minted access token; they
    require an OnHub-scoped token derived through a two-step OAuth chain
    rooted in a standard refresh token (``1//<chars>``). v3 records
    persist that token alongside the master token; v2 records remain
    loadable but Foyer REST verbs exit-2 with a hint pointing at
    ``auth wifi-refresh-bootstrap`` until the file is upgraded. The gRPC
    read path continues to use master_token + android_id regardless.

    ``extra="forbid"`` means unknown keys raise ``ValidationError``,
    which the credentials loader maps to exit 6 (FR-CRED-8).
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(..., ge=2, le=3)
    type: str = Field(..., pattern="^foyer$")
    google_account_email: str = Field(..., min_length=3)
    master_token: str = Field(..., min_length=1)
    android_id: str = Field(..., min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    issued_at: datetime
    refresh_token: str | None = Field(default=None, pattern=r"^1//[\w-]+$")

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

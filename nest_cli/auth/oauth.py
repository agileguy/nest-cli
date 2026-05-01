"""OAuth Desktop flow for the SDM scope.

Wraps ``google_auth_oauthlib.flow.InstalledAppFlow`` to implement
FR-CRED-1: prompt the operator's browser for consent, listen for the
local-callback redirect, exchange the auth code for a refresh token +
access token, and return them as a ``CamCredentials`` instance ready for
``save_credentials``.

``google-auth-oauthlib`` is a transitive dependency via ``google-nest-sdm``
(verified at Phase 0 install time). We import from it directly rather than
shelling out to the SDM client because the SDM client doesn't expose the
flow surface — it expects credentials already in hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from google_auth_oauthlib.flow import InstalledAppFlow

from nest_cli.auth.credentials import EXIT_AUTH_ERROR, CredentialError
from nest_cli.auth.types import CamCredentials

# The single OAuth scope nest-cli v0.1.0 requests. Pub/Sub scope is added in
# Phase 2 (SRD §16.2). Anything else is over-asking.
SDM_SCOPE = "https://www.googleapis.com/auth/sdm.service"

# Default access-token lifetime when Google omits ``expires_in`` from the
# initial token exchange. 1 hour matches Google's documented default.
_DEFAULT_TOKEN_LIFETIME_S = 3600


def _build_client_config(client_id: str, client_secret: str) -> dict[str, Any]:
    """Shape a Google ``installed`` client_config dict for ``InstalledAppFlow``.

    The "installed" type is the right one for Desktop OAuth clients (FR-CRED-1
    starts from a Desktop client JSON downloaded from
    ``console.cloud.google.com/apis/credentials``).
    """
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://127.0.0.1"],
        }
    }


def run_oauth_flow(
    client_id: str,
    client_secret: str,
    project_id: str,
    *,
    callback_port: int = 8765,
    open_browser: bool = True,
) -> CamCredentials:
    """Run the OAuth Desktop flow and return populated ``CamCredentials``.

    Side-effects: starts a short-lived HTTP listener on
    ``127.0.0.1:<callback_port>``, opens the operator's browser to the
    consent URL (unless ``open_browser=False`` for testing), blocks on
    consent completion.

    The ``project_id`` argument is the Google Cloud project id — not used
    by the OAuth flow itself, but threaded through into the returned
    ``CamCredentials`` because ``credentials-cam.json`` stores it
    alongside the tokens (FR-CRED-3).

    Failure modes:
    - Port already in use → ``CredentialError`` mapped to exit 2 (FR-CRED-1
      "wait for callback completion" cannot succeed without a free port).
      The error names a remediation: pass ``--callback-port=<other>``.
    - Operator denies consent → upstream raises; we let the error propagate
      with a helpful wrap.
    """
    flow = InstalledAppFlow.from_client_config(
        _build_client_config(client_id, client_secret),
        scopes=[SDM_SCOPE],
    )
    try:
        flow.run_local_server(
            host="127.0.0.1",
            port=callback_port,
            open_browser=open_browser,
            authorization_prompt_message=(
                "Open this URL in your browser to authorize nest-cli: {url}"
            ),
            success_message=("nest-cli authorization complete. You may close this browser tab."),
        )
    except OSError as exc:
        # ``OSError [Errno 48] Address already in use`` is the canonical
        # signal that another process holds the port. ``EADDRINUSE`` (48 on
        # macOS, 98 on Linux) is the most common; we treat any OSError
        # raised from the listener as a bind-time failure for v0.1.0. The
        # operator's remediation is to pick a different port — but we map
        # to exit 2 (auth) per task brief, because the OAuth flow itself
        # could not complete. The hint names the actionable next step.
        raise CredentialError(
            f"could not bind OAuth callback listener to 127.0.0.1:{callback_port}: {exc}",
            exit_code=EXIT_AUTH_ERROR,
            hint=(
                f"Another process is using port {callback_port}. "
                "Re-run with `--callback-port <other-port>` or stop the conflicting process."
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - upstream uses bare Exception for flow errors
        raise CredentialError(
            f"OAuth flow did not complete: {exc}",
            exit_code=EXIT_AUTH_ERROR,
            hint="Re-run `nest-cli auth setup`. If the browser never opened, pass --no-browser.",
        ) from exc

    creds = flow.credentials
    if not creds.refresh_token:
        # The Google library may legitimately return a credentials object
        # without a refresh token if ``access_type`` was not 'offline'. We
        # request offline implicitly via ``InstalledAppFlow`` defaults; if
        # the operator's account has previously consented to nest-cli,
        # Google sometimes omits the refresh token on re-consent. The fix
        # is to revoke at myaccount.google.com/permissions and retry.
        raise CredentialError(
            "OAuth flow returned no refresh token (Google may have omitted it on re-consent)",
            exit_code=EXIT_AUTH_ERROR,
            hint=(
                "Visit https://myaccount.google.com/permissions, remove the nest-cli client, "
                "then re-run `nest-cli auth setup`."
            ),
        )

    # ``creds.expiry`` is a naive datetime in UTC per upstream contract; we
    # promote it to aware UTC. If it's somehow missing, fall back to the
    # documented 1-hour default.
    if creds.expiry is None:
        expires_at = datetime.now(UTC) + timedelta(seconds=_DEFAULT_TOKEN_LIFETIME_S)
    else:
        expires_at = creds.expiry.replace(tzinfo=UTC)

    return CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id=project_id,
        oauth_client_id=client_id,
        oauth_client_secret=client_secret,
        refresh_token=creds.refresh_token,
        access_token=creds.token,
        expires_at=expires_at,
    )

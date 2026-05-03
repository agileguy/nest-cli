"""Thin REST wrapper around ``smartdevicemanagement.googleapis.com`` v1.

The SDM API requires an OAuth Bearer access token in every request.
``CamCredentials`` (Engineer A's Pydantic model) carries the access token,
the refresh token, and the expiry. We delegate refresh to Engineer A's
``refresh_access_token_if_needed`` so the rotation logic lives in one
place — but we ALSO retry once on a fresh 401, in case our cached
``CamCredentials.expires_at`` was wrong (clock skew, manual revocation
between commands, etc.).

Failure mapping (SRD §11.1):

- HTTP 401 after a forced refresh → ``StructuredError(EXIT_AUTH_ERROR)``
- HTTP 404 → ``StructuredError(EXIT_NOT_FOUND)``
- HTTP 4xx (other) → ``StructuredError(EXIT_DEVICE_ERROR)``
- HTTP 5xx → ``StructuredError(EXIT_NETWORK_ERROR)``
- Connection / DNS / TLS errors → ``StructuredError(EXIT_NETWORK_ERROR)``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from nest_cli.auth.credentials import (
    default_credentials_path,
    refresh_access_token_if_needed,
    save_credentials,
)
from nest_cli.auth.types import CamCredentials
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    StructuredError,
)
from nest_cli.sdm.stream_types import RtspStreamResult, WebRtcStreamResult
from nest_cli.sdm.types import Camera

SDM_API_ROOT = "https://smartdevicemanagement.googleapis.com/v1"

# Per SRD §3.1.2 / Decision 18: stream commands live under the
# CameraLiveStream trait. v0.2.0 ships RTSP-only extend/stop wired
# (FR-CAM-13 / FR-CAM-14 spec wording is `--extension-token`); the
# WebRTC extend/stop commands are kept reachable via the public method
# surface but only the RTSP variants are exposed to the CLI for now.
CMD_GENERATE_RTSP_STREAM = "sdm.devices.commands.CameraLiveStream.GenerateRtspStream"
CMD_GENERATE_WEBRTC_STREAM = "sdm.devices.commands.CameraLiveStream.GenerateWebRtcStream"
CMD_EXTEND_RTSP_STREAM = "sdm.devices.commands.CameraLiveStream.ExtendRtspStream"
CMD_STOP_RTSP_STREAM = "sdm.devices.commands.CameraLiveStream.StopRtspStream"

# Per-request timeout. SRD §7.4 puts the per-operation default at 10s;
# we mirror that here. Long-running operations (snapshot, stream
# negotiation) live on the operational verbs and apply their own knob.
DEFAULT_TIMEOUT_S = 10


class SdmClient:
    """SDM REST client backed by ``requests``.

    The client does NOT manage its own session by default; each request
    opens and closes a fresh connection. For v0.1.0 the CLI verbs are
    one-shot, so the connection-reuse benefit is small and the simpler
    failure model wins.

    Token refresh: on a 401, the client calls ``refresh_access_token_if_needed``
    with ``force=True`` to mint a new access token, then retries the
    original request once. A second 401 raises auth-error.
    """

    def __init__(
        self,
        credentials: CamCredentials,
        *,
        credentials_path: Path | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._credentials = credentials
        self._credentials_path = credentials_path or default_credentials_path()
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def list_devices(self, project_id: str) -> list[Camera]:
        """Call SDM ``enterprises/{project_id}/devices`` and parse to Cameras.

        Per FR-2/FR-2a returns the inventory the operator's credentials
        can see. Empty inventory returns ``[]`` with exit 0 (FR-3); the
        caller is responsible for the empty-output INFO log line on stderr.
        """
        url = f"{SDM_API_ROOT}/enterprises/{project_id}/devices"
        payload = self._get_with_refresh(url)
        devices = payload.get("devices") or []
        if not isinstance(devices, list):
            raise StructuredError(
                code=EXIT_DEVICE_ERROR,
                message="SDM devices.list returned a non-list 'devices' field",
                hint="Run with -vv to see the raw response.",
            )
        return [Camera.from_sdm_response(d) for d in devices if isinstance(d, dict)]

    def get_device(self, full_name: str) -> Camera:
        """Call SDM ``devices.get`` and parse the response to a Camera.

        ``full_name`` is the SDM device path (``enterprises/{proj}/devices/{id}``).
        """
        url = f"{SDM_API_ROOT}/{full_name}"
        payload = self._get_with_refresh(url)
        return Camera.from_sdm_response(payload)

    def execute_command(
        self,
        device_name: str,
        command: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """POST to ``{device_name}:executeCommand`` and return the parsed result.

        Mirrors the SDM ``executeCommand`` REST contract (SRD §3.1; used by
        FR-CAM-3..27). The request body is ``{"command": "<dotted>", "params": {...}}``;
        the response body is whatever the per-command result envelope is — this
        wrapper does not interpret it, just hands it back to the caller as a dict.

        HTTP failure mappings exactly mirror ``_get_with_refresh``:

        - 401 → force-refresh access token, retry once; second 401 raises ``EXIT_AUTH_ERROR``.
        - 404 → ``EXIT_NOT_FOUND``.
        - Other 4xx → ``EXIT_DEVICE_ERROR``.
        - 5xx → ``EXIT_NETWORK_ERROR``.
        - Connection / DNS / TLS / timeout → ``EXIT_NETWORK_ERROR``.

        The verbs that depend on this (snapshot, chime, stream, etc.) layer their
        own application-level error handling on top — e.g. SDM may return HTTP 200
        with a per-device error body for some failure modes; the verb is responsible
        for inspecting the returned dict's shape.
        """
        url = f"{SDM_API_ROOT}/{device_name}:executeCommand"
        body = {"command": command, "params": params}
        return self._post_with_refresh(url, body)

    # ------------------------------------------------------------------
    # Stream commands (FR-CAM-6..14 / SRD §3.1.2 / §10.2)
    # ------------------------------------------------------------------

    def generate_rtsp_stream(self, target_id: str) -> RtspStreamResult:
        """Issue ``GenerateRtspStream`` for ``target_id``; parse the response.

        Implements the cam-side of FR-CAM-7. Caller (the verb) builds
        the §10.2 Stream record from the parsed result.
        """
        payload = self.execute_command(target_id, CMD_GENERATE_RTSP_STREAM, {})
        return RtspStreamResult.from_sdm_response(payload)

    def generate_webrtc_stream(self, target_id: str, *, offer_sdp: str) -> WebRtcStreamResult:
        """Issue ``GenerateWebRtcStream`` with ``offer_sdp`` in params.

        Implements the cam-side of FR-CAM-8. Decision 6: the operator
        supplies the offer SDP; the CLI does NOT generate it. Verb
        layer enforces FR-CAM-9 (missing ``--offer-sdp`` exits 64).
        """
        payload = self.execute_command(
            target_id,
            CMD_GENERATE_WEBRTC_STREAM,
            {"offerSdp": offer_sdp},
        )
        return WebRtcStreamResult.from_sdm_response(payload)

    def extend_stream(self, target_id: str, *, extension_token: str) -> RtspStreamResult:
        """Issue ``ExtendRtspStream`` to refresh an active session.

        Implements the cam-side of FR-CAM-13. v0.2.0 wires only the RTSP
        extend path; WebRTC extend (``mediaSessionId`` keyed) is reachable
        via SDM but not yet exposed by the CLI verb.
        """
        payload = self.execute_command(
            target_id,
            CMD_EXTEND_RTSP_STREAM,
            {"streamExtensionToken": extension_token},
        )
        return RtspStreamResult.from_sdm_response(payload)

    def stop_stream(self, target_id: str, *, extension_token: str) -> None:
        """Issue ``StopRtspStream`` to invalidate an active session.

        Implements the cam-side of FR-CAM-14. Returns ``None`` on
        success (SDM returns an empty ``results`` object).
        """
        self.execute_command(
            target_id,
            CMD_STOP_RTSP_STREAM,
            {"streamExtensionToken": extension_token},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_with_refresh(self, url: str) -> dict[str, Any]:
        """GET ``url`` with a single auto-refresh-on-401 retry.

        First attempt uses the cached access token (after a maybe-refresh
        if it's near expiry). If the response is 401, force-refresh and
        retry once. A second 401 is fatal — auth error.
        """
        # Lazy refresh on near-expiry. ``refresh_access_token_if_needed``
        # is a no-op when the token has plenty of life left.
        self._credentials = refresh_access_token_if_needed(
            self._credentials,
            self._credentials_path,
        )

        status, body = self._do_get(url, self._credentials.access_token)
        if status == 401:
            # Force-refresh and retry once. Save the rotated creds so the
            # next CLI invocation starts from the post-rotation state.
            self._credentials = refresh_access_token_if_needed(
                self._credentials,
                self._credentials_path,
                force=True,
            )
            # Persist explicitly in case ``refresh_access_token_if_needed``
            # short-circuited (e.g. another process rotated already).
            save_credentials(self._credentials_path, self._credentials)
            status, body = self._do_get(url, self._credentials.access_token)
            if status == 401:
                raise StructuredError(
                    code=EXIT_AUTH_ERROR,
                    message="SDM API rejected access token even after refresh",
                    hint=(
                        "Run `nest-cli auth refresh`; if that fails, "
                        "`nest-cli auth setup --overwrite`."
                    ),
                )

        return _interpret_status(url, status, body)

    def _post_with_refresh(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST ``body`` to ``url`` with a single auto-refresh-on-401 retry.

        Symmetrical to ``_get_with_refresh``. Lazy near-expiry refresh,
        then retry once on 401 with a forced refresh, then surface as
        auth error if still 401.
        """
        self._credentials = refresh_access_token_if_needed(
            self._credentials,
            self._credentials_path,
        )

        status, body_bytes = self._do_post(url, self._credentials.access_token, body)
        if status == 401:
            self._credentials = refresh_access_token_if_needed(
                self._credentials,
                self._credentials_path,
                force=True,
            )
            save_credentials(self._credentials_path, self._credentials)
            status, body_bytes = self._do_post(url, self._credentials.access_token, body)
            if status == 401:
                raise StructuredError(
                    code=EXIT_AUTH_ERROR,
                    message="SDM API rejected access token even after refresh",
                    hint=(
                        "Run `nest-cli auth refresh`; if that fails, "
                        "`nest-cli auth setup --overwrite`."
                    ),
                )

        return _interpret_status(url, status, body_bytes)

    def _do_post(
        self,
        url: str,
        access_token: str,
        body: dict[str, Any],
    ) -> tuple[int, bytes]:
        """Issue a single POST with JSON body. Returns (status_code, raw_body)."""
        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self._timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"network error contacting SDM API: {exc}",
                hint="Check your internet connection and retry.",
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"timed out contacting SDM API after {self._timeout}s",
                hint="Google's SDM endpoint is slow or unreachable; retry shortly.",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"unexpected requests error contacting SDM API: {exc}",
            ) from exc

        return response.status_code, response.content

    def _do_get(self, url: str, access_token: str) -> tuple[int, bytes]:
        """Issue a single GET. Returns (status_code, raw_body)."""
        try:
            response = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"network error contacting SDM API: {exc}",
                hint="Check your internet connection and retry.",
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"timed out contacting SDM API after {self._timeout}s",
                hint="Google's SDM endpoint is slow or unreachable; retry shortly.",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"unexpected requests error contacting SDM API: {exc}",
            ) from exc

        return response.status_code, response.content


def _interpret_status(url: str, status: int, body: bytes) -> dict[str, Any]:
    """Map an HTTP response to a parsed dict or a StructuredError.

    200 — return parsed JSON.
    401 — caller already retried; map to auth error.
    404 — not found; map to exit 4.
    Other 4xx — device error; map to exit 1.
    5xx — network error; map to exit 3.
    """
    if status == 200:
        try:
            import json

            decoded = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as exc:
            raise StructuredError(
                code=EXIT_DEVICE_ERROR,
                message=f"SDM API returned non-JSON body for {url}: {exc}",
            ) from exc
        if not isinstance(decoded, dict):
            raise StructuredError(
                code=EXIT_DEVICE_ERROR,
                message=f"SDM API returned non-object JSON body for {url}",
            )
        return decoded

    snippet = body.decode("utf-8", errors="replace")[:200]
    if status == 401:
        raise StructuredError(
            code=EXIT_AUTH_ERROR,
            message=f"SDM API rejected access token for {url}",
            hint="Run `nest-cli auth refresh`.",
            details={"status_code": status, "body_snippet": snippet},
        )
    if status == 404:
        raise StructuredError(
            code=EXIT_NOT_FOUND,
            message=f"SDM API: device or path not found at {url}",
            hint="Run `nest-cli discover` to refresh the inventory.",
            details={"status_code": status},
        )
    if 400 <= status < 500:
        raise StructuredError(
            code=EXIT_DEVICE_ERROR,
            message=f"SDM API returned HTTP {status} for {url}",
            details={"status_code": status, "body_snippet": snippet},
        )
    raise StructuredError(
        code=EXIT_NETWORK_ERROR,
        message=f"SDM API returned HTTP {status} for {url}",
        hint="Google's SDM endpoint is degraded; retry shortly.",
        details={"status_code": status, "body_snippet": snippet},
    )

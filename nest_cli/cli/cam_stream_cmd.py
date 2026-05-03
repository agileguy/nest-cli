"""``nest-cli cam stream`` / ``stream-extend`` / ``stream-stop`` verbs.

Implements FR-CAM-6 through FR-CAM-14 (SRD §5.3.3 and §5.3.4): the
session-metadata-emitter side of the SDM ``CameraLiveStream`` trait.

The asymmetry is real (SRD §3.1.2):

- **RTSP cameras** (1st-gen Nest IQ, wired Hello, etc.) — emit a Stream
  record with a directly-usable rtsps:// URL.
- **WebRTC cameras** (2nd-gen Battery Cam, Battery Doorbell, Floodlight,
  all post-2021 hardware) — require the operator to supply the offer
  SDP via ``--offer-sdp <path-or-stdin>``; the CLI emits the answer
  SDP plus session metadata. Per SRD Decision 6 the CLI does NOT
  generate the offer SDP itself.

This module is split out from ``cam_cmd.py`` to keep the merge surface
minimal. Engineer A is independently extending ``cam_cmd.py`` with
snapshot/chime/battery/signal verbs; pulling stream verbs into a
sibling module avoids step-on-toes during Phase 2 development. The
``cam_group`` registers all four verbs in ``cam_cmd.py``.

Exit codes (SRD §11.1):

- 0 — success.
- 1 — SDM 4xx during the executeCommand POST (e.g., malformed offer SDP).
- 2 — auth failure (refresh-token rejected, chmod violation).
- 3 — network error (DNS / TLS / 5xx / timeout).
- 4 — alias unknown / SDM 404 (target removed).
- 6 — config error (validation).
- 64 — usage error: WebRTC camera without ``--offer-sdp``,
  ``stream-extend`` / ``stream-stop`` without ``--extension-token``,
  output-flag conflict.
"""

from __future__ import annotations

import sys

import click

from nest_cli.cli._shared import (
    exit_on_structured_error,
    load_credentials_or_exit,
)
from nest_cli.config import default_config_path, load_config, resolve_alias
from nest_cli.errors import EXIT_USAGE_ERROR, StructuredError
from nest_cli.output import OutputMode, add_output_options, emit
from nest_cli.sdm.client import SdmClient
from nest_cli.sdm.stream_types import Stream
from nest_cli.sdm.types import Camera

# SDM trait + protocol token for the WebRTC vs RTSP routing decision.
_TRAIT_LIVE_STREAM = "sdm.devices.traits.CameraLiveStream"
_PROTO_RTSP = "RTSP"
_PROTO_WEBRTC = "WEB_RTC"


@click.command("stream")
@click.argument("target")
@click.option(
    "--offer-sdp",
    "offer_sdp_source",
    default=None,
    help=(
        "WebRTC offer SDP. Required for WebRTC cameras (FR-CAM-9). "
        "Pass a file path, or '-' to read from stdin (FR-CAM-10). "
        "Per SRD Decision 6 the operator owns SDP generation."
    ),
)
@add_output_options
def cam_stream(
    target: str,
    offer_sdp_source: str | None,
    output_mode: OutputMode,
) -> None:
    """Negotiate a stream session and emit a §10.2 Stream record.

    Implements FR-CAM-6 / FR-CAM-7 / FR-CAM-8.

    Behavior:

    1. Resolve ``target`` against ``[aliases]`` in config.
    2. Fetch the camera record (SDM ``devices.get``) to detect protocol.
    3. RTSP cameras: call ``GenerateRtspStream``, emit Stream record.
    4. WebRTC cameras: require ``--offer-sdp`` (else exit 64); call
       ``GenerateWebRtcStream``, emit Stream record.

    Per FR-CAM-11 the CLI does not decode/transcode/proxy video. The
    output is the operator's input to a downstream consumer
    (ffmpeg/mpv for RTSP; a WebRTC-capable peer for WebRTC).
    """
    camera = _fetch_camera(target, output_mode)
    protocol = _detect_stream_protocol(camera)
    client = _make_client(output_mode)

    try:
        if protocol == "rtsp":
            if offer_sdp_source is not None:
                # Defensive: --offer-sdp on an RTSP camera is harmless
                # but probably an operator mistake. Keep silent (FR-CAM-7
                # does not forbid the flag); the verb still proceeds.
                pass
            rtsp = client.generate_rtsp_stream(camera.target_id)
            stream = Stream.from_rtsp_result(target=target, result=rtsp)
        else:  # webrtc
            offer_sdp = _read_offer_sdp(offer_sdp_source, output_mode)
            webrtc = client.generate_webrtc_stream(camera.target_id, offer_sdp=offer_sdp)
            stream = Stream.from_webrtc_result(target=target, result=webrtc)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
        raise  # unreachable

    emit(stream, output_mode)


@click.command("stream-extend")
@click.argument("target")
@click.option(
    "--extension-token",
    "extension_token",
    default=None,
    help="Stream extension token returned by the previous stream / stream-extend.",
)
@add_output_options
def cam_stream_extend(
    target: str,
    extension_token: str | None,
    output_mode: OutputMode,
) -> None:
    """Refresh an active RTSP session, returning the updated §10.2 Stream.

    Implements FR-CAM-13. Without ``--extension-token``: exit 64. WebRTC
    extend (``--media-session-id``) is not yet wired in v0.2.0 — the
    SDM ``ExtendWebRtcStream`` command exists but the verb shape per
    SRD names ``--extension-token``, so v0.2.0 supports the RTSP form
    only.
    """
    if not extension_token:
        _exit_missing_required(
            "--extension-token",
            "stream-extend requires --extension-token <tok> (FR-CAM-13).",
            output_mode,
        )

    camera = _fetch_camera(target, output_mode)
    client = _make_client(output_mode)
    try:
        rtsp = client.extend_stream(camera.target_id, extension_token=extension_token)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
        raise  # unreachable

    emit(Stream.from_rtsp_result(target=target, result=rtsp), output_mode)


@click.command("stream-stop")
@click.argument("target")
@click.option(
    "--extension-token",
    "extension_token",
    default=None,
    help="Stream extension token returned by the previous stream / stream-extend.",
)
@add_output_options
def cam_stream_stop(
    target: str,
    extension_token: str | None,
    output_mode: OutputMode,
) -> None:
    """Invalidate an active RTSP session. Exit 0 on success.

    Implements FR-CAM-14. No stdout payload is emitted on success
    beyond what ``--output`` requests (text mode prints nothing; json
    mode prints ``{"target": ..., "stopped": true}`` for completeness).
    """
    if not extension_token:
        _exit_missing_required(
            "--extension-token",
            "stream-stop requires --extension-token <tok> (FR-CAM-14).",
            output_mode,
        )

    camera = _fetch_camera(target, output_mode)
    client = _make_client(output_mode)
    try:
        client.stop_stream(camera.target_id, extension_token=extension_token)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
        raise  # unreachable

    emit({"target": target, "stopped": True}, output_mode)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _fetch_camera(target: str, output_mode: OutputMode) -> Camera:
    """Resolve ``target`` against config + SDM, returning the Camera record.

    Mirrors ``cam_cmd._fetch_camera`` but lives here to avoid the
    cross-module import-cycle risk during Phase 2 parallel
    development. The function is internal to this module.
    """
    try:
        config = load_config(default_config_path())
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    resolved = resolve_alias(config, target)
    creds = load_credentials_or_exit(output_mode)
    client = SdmClient(creds)
    try:
        return client.get_device(resolved)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
        raise  # unreachable


def _make_client(output_mode: OutputMode) -> SdmClient:
    """Build an SdmClient with credentials loaded for the second-call path."""
    creds = load_credentials_or_exit(output_mode)
    return SdmClient(creds)


def _detect_stream_protocol(camera: Camera) -> str:
    """Return ``"rtsp"`` or ``"webrtc"`` for ``camera``.

    Reads the camera's ``CameraLiveStream`` trait's ``supportedProtocols``
    array. If both are listed, prefer RTSP (operator can override by
    invoking against a target that explicitly negotiates WebRTC — out
    of scope for v0.2.0). If neither is listed (the SDM trait shape
    changed), default to WebRTC and let the request fail at the SDM
    layer with a structured error.
    """
    for trait in camera.traits:
        if trait.name == _TRAIT_LIVE_STREAM:
            extra = trait.model_dump()
            protos = extra.get("supportedProtocols") or []
            if isinstance(protos, list):
                if _PROTO_RTSP in protos:
                    return "rtsp"
                if _PROTO_WEBRTC in protos:
                    return "webrtc"
    # Defensive default — if the trait shape ever loses the field we
    # still emit a request and let SDM tell us no.
    return "webrtc"


def _read_offer_sdp(source: str | None, output_mode: OutputMode) -> str:
    """Resolve ``--offer-sdp`` input. ``None`` exits 64 per FR-CAM-9.

    - ``None``  → exit 64 with hint pointing at FR-CAM-8 / §3.1.2.
    - ``"-"``   → read from stdin (FR-CAM-10).
    - else     → treat as file path; read contents.
    """
    if source is None:
        _exit_missing_required(
            "--offer-sdp",
            (
                "WebRTC cameras require --offer-sdp <path-or-stdin>. "
                "Per SRD Decision 6 / FR-CAM-8, the operator generates "
                "the offer SDP; the CLI does not embed a WebRTC stack. "
                "See SRD §3.1.2 for the protocol asymmetry."
            ),
            output_mode,
        )
    if source == "-":
        return sys.stdin.read()
    try:
        with open(source, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        exit_on_structured_error(
            StructuredError(
                code=EXIT_USAGE_ERROR,
                message=f"could not read --offer-sdp file {source}: {exc}",
                hint="Pass a readable file path, or '-' to read from stdin.",
            ),
            output_mode,
        )
        raise  # unreachable


def _exit_missing_required(
    flag: str,
    hint: str,
    output_mode: OutputMode,
) -> None:
    """Emit a structured error for a missing-required-arg condition (exit 64)."""
    exit_on_structured_error(
        StructuredError(
            code=EXIT_USAGE_ERROR,
            message=f"missing required argument: {flag}",
            hint=hint,
        ),
        output_mode,
    )

"""``nest-cli cam`` subgroup — camera-side commands.

v0.1.0 shipped three verbs from SRD §5.3 (``list``, ``info``, ``capabilities``).

Phase 2 (v0.2.0) adds the direct-command verbs that the SDM
``executeCommand`` endpoint backs:

- ``cam snapshot`` (FR-CAM-3..5) — JPEG capture with two-tier fallback.
- ``cam chime`` (FR-CAM-15..16) — invoke ``DoorbellChime.Chime``.
- ``cam battery`` (FR-CAM-26) — emit battery state for battery-powered cams.
- ``cam signal`` (FR-CAM-27) — emit RSSI / last-online metadata.

The streaming and events verbs (``stream``, ``stream-extend``, ``stream-stop``,
``events``) are owned by Engineer B and land alongside in the same v0.2.0
release.

Target resolution
-----------------

The ``<target>`` argument is resolved against the local config:

- If it matches an alias key in ``[aliases]``, the configured target
  string is used.
- Otherwise the input is treated verbatim (operator passed a literal
  SDM device path or device id).

Unknown alias → exit 4 (FR-19). Known alias whose remote target no
longer exists → exit 4 from the SDM 404 path with a hint pointing at
``nest-cli discover``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
import requests

from nest_cli.cli._shared import (
    exit_on_structured_error,
    filter_aliases_by_family,
    load_credentials_or_exit,
)
from nest_cli.cli.cam_events_cmd import cam_events
from nest_cli.cli.cam_stream_cmd import cam_stream, cam_stream_extend, cam_stream_stop
from nest_cli.cli.list_cmd import _probe_records
from nest_cli.config import default_config_path, load_config, resolve_alias
from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_UNSUPPORTED_FEATURE,
    EXIT_USAGE_ERROR,
    StructuredError,
)
from nest_cli.output import (
    OutputMode,
    _resolve_output_mode,
    add_output_options,
    emit,
)
from nest_cli.sdm.client import SdmClient
from nest_cli.sdm.types import Camera

# ---------------------------------------------------------------------------
# SDM command names (executeCommand body's ``command`` field)
# ---------------------------------------------------------------------------

_SDM_CMD_DOORBELL_CHIME = "sdm.devices.commands.DoorbellChime.Chime"
_SDM_CMD_CAMERA_IMAGE = "sdm.devices.commands.CameraImage.GenerateImage"
_SDM_CMD_CAMERA_EVENT_IMAGE = "sdm.devices.commands.CameraEventImage.GenerateImage"

# Trait names that gate verb support.
_TRAIT_DOORBELL_CHIME = "sdm.devices.traits.DoorbellChime"
_TRAIT_CAMERA_IMAGE = "sdm.devices.traits.CameraImage"
_TRAIT_CAMERA_EVENT_IMAGE = "sdm.devices.traits.CameraEventImage"

# ---------------------------------------------------------------------------
# Trait → supported-verb mapping (SRD §5.3, FR-CAM-28)
# ---------------------------------------------------------------------------
#
# Each entry maps an SDM trait name to the list of ``nest-cli cam`` verbs
# that depend on it. The SRD names the trait → verb relationships in
# §5.3.2 (snapshot), §5.3.3 (stream), §5.3.5 (chime), §5.3.8 (events).
#
# Phase 2 adds entries for the direct-command verbs (snapshot, chime).
# Streaming and events verbs are extended by Engineer B in the parallel
# Phase 2 effort.
#
# Two verbs are universal — every camera supports them — and don't need
# a trait gate:
#
# - ``info``         — every camera responds to ``devices.get``
# - ``capabilities`` — local computation over the trait list
#
# These are added unconditionally to ``supported_verbs``.
#
# Two verbs (battery, signal) are gated on data presence rather than
# trait presence: SDM does not currently expose a documented trait for
# battery state or RSSI. ``Camera.battery_pct`` and ``Camera.signal_strength``
# are nullable fields populated when the upstream payload includes them.
# ``_supported_verbs_for`` consults the parsed Camera object directly for
# these two verbs (see ``_PREDICATE_VERBS`` below).

_TRAIT_TO_VERBS: dict[str, list[str]] = {
    "sdm.devices.traits.CameraEventImage": ["snapshot"],
    "sdm.devices.traits.CameraImage": ["snapshot"],
    "sdm.devices.traits.CameraLiveStream": ["stream", "stream-extend", "stream-stop"],
    "sdm.devices.traits.CameraMotion": ["events"],
    "sdm.devices.traits.CameraPerson": ["events"],
    "sdm.devices.traits.CameraSound": ["events"],
    # DoorbellChime is dual-purpose: it gates the chime verb AND emits
    # doorbell-press events that the events verb consumes.
    "sdm.devices.traits.DoorbellChime": ["chime", "events"],
}

# Verbs every camera has (no trait gate).
_UNIVERSAL_VERBS = ("info", "capabilities")

# Verbs gated on Camera-record predicates rather than SDM trait names.
# Each entry is (verb_name, predicate) — predicate takes a Camera and
# returns True if the verb is supported. Used by ``_supported_verbs_for``.
_PREDICATE_VERBS: tuple[tuple[str, str], ...] = (
    ("battery", "battery_pct"),
    ("signal", "signal_strength"),
)


cam_group = click.Group(
    name="cam",
    help="Nest camera commands (SDM API). Implements FR-CAM-1, FR-CAM-2, FR-CAM-28.",
)

# Phase 2 stream verbs (FR-CAM-6..14). Defined in the sibling
# ``cam_stream_cmd`` module to keep the merge surface small while two
# engineers extend ``cam_cmd`` in parallel.
cam_group.add_command(cam_stream)
cam_group.add_command(cam_stream_extend)
cam_group.add_command(cam_stream_stop)

# Phase 2 events verb (FR-CAM-19..25 one-shot drain). --follow is
# Phase 2.1 and not yet implemented.
cam_group.add_command(cam_events)


@cam_group.command("list")
@click.option(
    "--probe",
    is_flag=True,
    default=False,
    help="Probe each camera for liveness; populates the 'online' field.",
)
@click.option(
    "--online-only",
    is_flag=True,
    default=False,
    help="Implies --probe; emit only cameras that responded.",
)
@add_output_options
def cam_list(probe: bool, online_only: bool, output_mode: OutputMode) -> None:
    """List cam aliases from the local config (synonym for ``list --family cam``).

    Implements FR-CAM-1.
    """
    try:
        config = load_config(default_config_path())
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    aliases = filter_aliases_by_family(config.aliases, "cam")
    records = [
        {"name": name, "target": target, "family": "cam"} for name, target in aliases.items()
    ]
    if probe or online_only:
        records = _probe_records(records, output_mode)
        if online_only:
            records = [r for r in records if r.get("online") is True]
    emit(records, output_mode)


@cam_group.command("info")
@click.argument("target")
@add_output_options
def cam_info(target: str, output_mode: OutputMode) -> None:
    """Issue an SDM ``devices.get`` and emit the §10.1 Camera record.

    Implements FR-CAM-2.

    ``<target>`` is either an alias name (resolved via ``[aliases]``) or
    a literal SDM device path (``enterprises/{proj}/devices/{id}``).
    Unknown-alias and unknown-device both exit 4.
    """
    camera = _fetch_camera(target, output_mode)
    emit(camera, output_mode)


@cam_group.command("capabilities")
@click.argument("target")
@add_output_options
def cam_capabilities(target: str, output_mode: OutputMode) -> None:
    """Emit traits + derived supported_verbs list (FR-CAM-28).

    The ``supported_verbs`` list reflects what ``nest-cli cam`` sub-verbs
    can be invoked on this specific camera. Universal verbs (``info``,
    ``capabilities``) are always included; trait-gated verbs are added
    if the camera has the relevant trait. The mapping table grows in
    future phases as new verbs land.
    """
    camera = _fetch_camera(target, output_mode)
    supported = _supported_verbs_for(camera)
    payload = {
        "target_id": camera.target_id,
        "type": camera.type,
        "traits": [t.model_dump(mode="json") for t in camera.traits],
        "supported_verbs": sorted(supported),
    }
    emit(payload, output_mode)


@cam_group.command("chime")
@click.argument("target")
@add_output_options
def cam_chime(target: str, output_mode: OutputMode) -> None:
    """Invoke ``DoorbellChime.Chime`` on a doorbell.

    Implements FR-CAM-15 / FR-CAM-16. Cameras lacking the
    ``sdm.devices.traits.DoorbellChime`` trait exit 5 with a hint
    enumerating the operator-configured aliases that DO support the
    chime command.
    """
    camera = _fetch_camera(target, output_mode)
    if not camera.has_trait(_TRAIT_DOORBELL_CHIME):
        hint = _doorbell_capable_hint(target, output_mode)
        err = StructuredError(
            code=EXIT_UNSUPPORTED_FEATURE,
            message=(f"camera {target!r} does not support chime (missing {_TRAIT_DOORBELL_CHIME})"),
            hint=hint,
            details={"target": target, "missing_trait": _TRAIT_DOORBELL_CHIME},
        )
        exit_on_structured_error(err, output_mode)

    creds = load_credentials_or_exit(output_mode)
    client = SdmClient(creds)
    try:
        client.execute_command(camera.target_id, _SDM_CMD_DOORBELL_CHIME, {})
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit({"target": target, "command": _SDM_CMD_DOORBELL_CHIME, "result": "ok"}, output_mode)


@cam_group.command("battery")
@click.argument("target")
@add_output_options
def cam_battery(target: str, output_mode: OutputMode) -> None:
    """Emit battery state for battery-powered cameras.

    Implements FR-CAM-26. Cameras with a non-null ``battery_pct`` field
    emit ``{target, battery_pct, is_battery_powered: true, last_event_ts}``
    at exit 0. Cameras without battery state exit 5 with
    ``is_battery_powered: false`` and the target name in the structured
    error details.

    SDM does not expose a documented trait for battery state; this verb
    is gated on the parsed ``Camera.battery_pct`` field, which is
    populated when the upstream SDM payload includes a ``battery_pct``
    key. Today that means battery state surfaces only for hardware where
    Google's SDM response carries it; operators with battery cams that
    don't currently expose this field will see exit 5 honestly rather
    than synthesized data.
    """
    camera = _fetch_camera(target, output_mode)
    if camera.battery_pct is None:
        err = StructuredError(
            code=EXIT_UNSUPPORTED_FEATURE,
            message=(
                f"camera {target!r} does not expose battery state (no battery_pct in SDM response)"
            ),
            hint=(
                "SDM only surfaces battery_pct for hardware that exposes it. "
                "Run `nest-cli cam info <target>` to inspect the raw record."
            ),
            details={"target": target, "is_battery_powered": False},
        )
        exit_on_structured_error(err, output_mode)

    payload: dict[str, Any] = {
        "target": target,
        "target_id": camera.target_id,
        "battery_pct": camera.battery_pct,
        "is_battery_powered": True,
    }
    if camera.last_event_ts is not None:
        payload["last_event_ts"] = camera.last_event_ts
    emit(payload, output_mode)


@cam_group.command("signal")
@click.argument("target")
@add_output_options
def cam_signal(target: str, output_mode: OutputMode) -> None:
    """Emit signal-strength (RSSI dBm) and last-online timestamp.

    Implements FR-CAM-27. Cameras with a non-null ``signal_strength``
    emit ``{target, target_id, signal_strength_dbm, last_online_ts?}``
    at exit 0. Cameras whose SDM response does not expose RSSI exit 5.

    SDM does not currently expose a documented trait for signal strength;
    this verb gates on the parsed ``Camera.signal_strength`` field. The
    last-online timestamp is sourced from ``Camera.last_event_ts`` when
    present, since the camera is provably online at the moment its last
    event was captured.
    """
    camera = _fetch_camera(target, output_mode)
    if camera.signal_strength is None:
        err = StructuredError(
            code=EXIT_UNSUPPORTED_FEATURE,
            message=(
                f"camera {target!r} does not expose a signal-strength "
                "surface (no signal_strength in SDM response)"
            ),
            hint=(
                "SDM only surfaces signal_strength for hardware that exposes it. "
                "Run `nest-cli cam info <target>` to inspect the raw record."
            ),
            details={"target": target, "has_signal_strength": False},
        )
        exit_on_structured_error(err, output_mode)

    payload: dict[str, Any] = {
        "target": target,
        "target_id": camera.target_id,
        "signal_strength_dbm": camera.signal_strength,
    }
    if camera.last_event_ts is not None:
        payload["last_online_ts"] = camera.last_event_ts
    emit(payload, output_mode)


@cam_group.command("snapshot")
@click.argument("target")
@click.option(
    "--output",
    "output_path",
    required=True,
    type=str,
    help="Path to write the JPEG. Use '-' to write to stdout.",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Emit a JSON SnapshotResult envelope after writing the JPEG.",
)
@click.option(
    "--jsonl",
    "jsonl_flag",
    is_flag=True,
    default=False,
    help="Emit a JSON-lines SnapshotResult envelope after writing the JPEG.",
)
@click.option(
    "--quiet",
    "quiet_flag",
    is_flag=True,
    default=False,
    help="Suppress the SnapshotResult envelope; only the JPEG file is produced.",
)
def cam_snapshot(
    target: str,
    output_path: str,
    json_flag: bool,
    jsonl_flag: bool,
    quiet_flag: bool,
) -> None:
    """Capture a JPEG with the FR-CAM-3..5 two-tier fallback.

    Tier 1: ``CameraImage.GenerateImage`` (preferred when present).
    Tier 2: ``CameraEventImage.GenerateImage`` keyed off the most recent
    eventId in the past 60s. Tier 3 (ffmpeg-from-RTSP) is deferred.

    Auth-rejection at any tier exits 2 immediately, no fallback (FR-CAM-4a).
    A camera with neither trait AND no event in window exits 5 (FR-CAM-4b).

    ``--output -`` writes JPEG bytes to stdout. ``--output -`` is mutually
    exclusive with ``--json`` / ``--jsonl`` (FR-CAM-5) — passing both exits 64.
    Note: snapshot does not accept the global ``--output text|json|jsonl|quiet``
    mode flag because ``--output`` here means the JPEG destination path; the
    convenience flags ``--json`` / ``--jsonl`` / ``--quiet`` cover the
    envelope-format dimension.
    """
    # Resolve the JSON/JSONL/quiet → OutputMode using the shared helper.
    # Snapshot omits the global ``--output text|json|jsonl|quiet`` knob
    # because its own ``--output <path>`` argument owns that flag name.
    try:
        output_mode = _resolve_output_mode(
            output_explicit=None,
            json_flag=json_flag,
            jsonl_flag=jsonl_flag,
            quiet_flag=quiet_flag,
        )
    except StructuredError as exc:
        exit_on_structured_error(exc, "text")

    if output_path == "-" and output_mode in ("json", "jsonl"):
        err = StructuredError(
            code=EXIT_USAGE_ERROR,
            message="--output - is mutually exclusive with --json / --jsonl",
            hint=(
                "JPEG bytes and JSON envelope share stdout; pick one. "
                "Drop --json/--jsonl to send the JPEG to stdout, or pass a "
                "file path to --output and keep the JSON envelope."
            ),
        )
        exit_on_structured_error(err, "text")

    # Reviewer feedback (C5): --output - + --quiet is undefined behaviour.
    # FR-14 says --quiet suppresses ALL stdout; pairing it with --output -
    # would either silently emit JPEG bytes (violating --quiet) or write
    # nothing at all (silently consuming the operator's snapshot). Reject
    # the combination explicitly.
    if output_path == "-" and output_mode == "quiet":
        err = StructuredError(
            code=EXIT_USAGE_ERROR,
            message=(
                "--output - is mutually exclusive with --quiet "
                "(the only output channel would be silenced)"
            ),
            hint=(
                "Pass --output <path> if you want to suppress envelope output, "
                "or drop --quiet to send the JPEG to stdout."
            ),
        )
        exit_on_structured_error(err, "text")

    camera = _fetch_camera(target, output_mode)
    creds = load_credentials_or_exit(output_mode)
    client = SdmClient(creds)

    try:
        jpeg_bytes, mechanism = _try_snapshot_tiers(client, camera)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    if output_path == "-":
        sys.stdout.buffer.write(jpeg_bytes)
        sys.stdout.buffer.flush()
        return

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(jpeg_bytes)
    emit(
        {
            "target": target,
            "output": output_path,
            "mechanism": mechanism,
            "bytes": len(jpeg_bytes),
        },
        output_mode,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _supported_verbs_for(camera: Camera) -> list[str]:
    """Compute the verb list for ``camera`` from its trait set + predicates.

    Algorithm:

    1. Start with the universal verbs (``info``, ``capabilities``).
    2. For each trait the camera has, union in the verbs listed under
       that trait in ``_TRAIT_TO_VERBS``.
    3. For each predicate verb in ``_PREDICATE_VERBS``, add the verb if
       the corresponding ``Camera`` field is non-null. SDM does not
       expose a documented trait for battery/signal state, so these
       verbs are gated on parsed-record presence rather than trait name.
    4. De-dupe and return.
    """
    verbs: set[str] = set(_UNIVERSAL_VERBS)
    for trait in camera.traits:
        verbs.update(_TRAIT_TO_VERBS.get(trait.name, ()))
    for verb, attr in _PREDICATE_VERBS:
        if getattr(camera, attr, None) is not None:
            verbs.add(verb)
    return list(verbs)


def _fetch_camera(target: str, output_mode: OutputMode) -> Camera:
    """Resolve ``target`` against config + SDM, returning the Camera record.

    Failure paths emit a structured error to stderr and ``sys.exit`` with
    the SRD-mapped code:

    - Config load failure → exit 6.
    - Credentials missing / chmod / refresh failure → exit 2.
    - Network error → exit 3.
    - SDM 404 (device removed from account) → exit 4.
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
        raise  # unreachable — for type-checkers


_GENERIC_DOORBELL_HINT = (
    "Run `nest-cli cam capabilities <target>` against your other "
    "cameras to find one with DoorbellChime."
)


def _doorbell_capable_hint(failing_target: str, output_mode: OutputMode) -> str:
    """Return the FR-CAM-16 hint enumerating doorbell-capable aliases.

    Resolves the operator's [aliases] against a single SDM ``devices.list``
    call (one HTTP roundtrip, not N+1) and reports which aliases point
    at cameras carrying ``DoorbellChime``. ``failing_target`` is excluded
    from the result. Falls back to a generic hint when config or
    credentials aren't usable, or when no aliases match.
    """
    try:
        config = load_config(default_config_path())
    except StructuredError:
        return _GENERIC_DOORBELL_HINT

    cam_aliases = filter_aliases_by_family(config.aliases, "cam")
    if not cam_aliases:
        return _GENERIC_DOORBELL_HINT

    creds = load_credentials_or_exit(output_mode)
    client = SdmClient(creds)
    try:
        cameras = client.list_devices(creds.google_cloud_project_id)
    except StructuredError:
        return _GENERIC_DOORBELL_HINT

    doorbell_target_ids = {c.target_id for c in cameras if c.has_trait(_TRAIT_DOORBELL_CHIME)}
    capable = sorted(
        alias_name
        for alias_name, alias_target in cam_aliases.items()
        if alias_name != failing_target
        and alias_target != failing_target
        and alias_target in doorbell_target_ids
    )

    if not capable:
        return (
            "No doorbell-capable cameras found in your config. The chime "
            "verb only works on devices carrying sdm.devices.traits.DoorbellChime."
        )
    return f"Doorbell-capable aliases in your config: {', '.join(capable)}."


# ---------------------------------------------------------------------------
# Snapshot helpers (FR-CAM-3..5)
# ---------------------------------------------------------------------------


# Per-tier per-request timeout for the ``executeCommand`` call AND the
# follow-up image GET. SRD §7.4 puts the per-operation default at 10s;
# snapshot is allowed to be a bit longer because the JPEG transfer
# itself may add latency on cellular cameras. 20s total keeps us
# inside the 30s budget the SRD mentions for snapshot in §7.1.
_SNAPSHOT_IMAGE_TIMEOUT_S = 20


def _try_snapshot_tiers(client: SdmClient, camera: Camera) -> tuple[bytes, str]:
    """Run the FR-CAM-4 fallback chain. Return (jpeg_bytes, mechanism).

    Mechanism is one of ``"camera_image"`` / ``"camera_event_image"``.
    Auth-rejection at any tier propagates immediately (no fallback) per
    FR-CAM-4a — ``StructuredError(code=EXIT_AUTH_ERROR)`` from the SDM
    client is re-raised as-is and the caller maps it to exit 2.

    Tier selection:

    - **Tier 1**: camera has ``CameraImage`` trait → call
      ``CameraImage.GenerateImage``, GET the returned URL.
    - **Tier 2**: camera has ``CameraEventImage`` trait AND a recent
      eventId is available (via ``_fetch_recent_event_id``) → call
      ``CameraEventImage.GenerateImage`` with the eventId param.
    - **Tier 3** (deferred): ffmpeg-from-RTSP. Not in v0.2.0.

    If neither tier is available, raise ``StructuredError(EXIT_UNSUPPORTED_FEATURE)``
    per FR-CAM-4b.
    """
    has_camera_image = camera.has_trait(_TRAIT_CAMERA_IMAGE)
    has_camera_event_image = camera.has_trait(_TRAIT_CAMERA_EVENT_IMAGE)

    # Reviewer feedback (C6): FR-CAM-4 says "advance on failure". The
    # prior implementation only advanced to tier 2 when the camera
    # *lacked* the CameraImage trait — a tier-1 5xx, malformed body, or
    # connection error did NOT trigger fallback. Wrap tier 1 in try /
    # except StructuredError and advance on any non-auth failure.
    # FR-CAM-4a still short-circuits on EXIT_AUTH_ERROR from any tier.
    tier1_error: StructuredError | None = None
    if has_camera_image:
        try:
            result = client.execute_command(camera.target_id, _SDM_CMD_CAMERA_IMAGE, {})
            url, token = _parse_image_url_and_token(result, mechanism="camera_image")
            return _download_snapshot_bytes(url, token), "camera_image"
        except StructuredError as exc:
            if exc.code == EXIT_AUTH_ERROR:  # FR-CAM-4a — never fall back on auth
                raise
            tier1_error = exc

    if has_camera_event_image:
        event_id = _fetch_recent_event_id(client, camera)
        if event_id is not None:
            try:
                result = client.execute_command(
                    camera.target_id,
                    _SDM_CMD_CAMERA_EVENT_IMAGE,
                    {"eventId": event_id},
                )
                url, token = _parse_image_url_and_token(result, mechanism="camera_event_image")
                return _download_snapshot_bytes(url, token), "camera_event_image"
            except StructuredError:
                # Tier 2 also failed — surface its error directly. Auth
                # rejections at tier 2 still propagate as exit 2 because
                # the SDM client raises EXIT_AUTH_ERROR; we re-raise the
                # exception unchanged.
                raise

    # Tier 2 wasn't available (no CameraEventImage trait OR no recent
    # event in window). If tier 1 failed earlier, surface that error;
    # otherwise the camera genuinely has no viable snapshot mechanism.
    if tier1_error is not None:
        raise tier1_error

    raise StructuredError(
        code=EXIT_UNSUPPORTED_FEATURE,
        message=(
            f"camera {camera.target_id!r} cannot snapshot: "
            "no CameraImage trait and no recent event in the 60s window"
        ),
        hint=(
            "Run `nest-cli cam capabilities <target>` to inspect the trait array. "
            "WebRTC-only cameras (most 2nd-gen battery hardware) only snapshot "
            "via CameraEventImage, which requires a recent motion/person/"
            "doorbell-press event in the last 60 seconds."
        ),
        details={
            "target_id": camera.target_id,
            "traits": [t.name for t in camera.traits],
        },
    )


def _fetch_recent_event_id(client: SdmClient, camera: Camera) -> str | None:
    """Return the most recent eventId for ``camera`` within the FR-CAM-4 60s window.

    v0.2.0 returns ``None`` unconditionally — Pub/Sub provisioning
    (``auth setup --pubsub``) is the Phase 2 stretch / Phase 5+
    deferral that wires the eventId source. Tests inject a value via
    monkeypatch on this function to exercise the tier-2 control flow.

    The seam exists deliberately so the verb's fallback shape is
    locked in now and only this one function needs replacement when
    the Pub/Sub source lands.
    """
    _ = (client, camera)  # explicit unused — both will be used in the future
    return None


def _parse_image_url_and_token(result: dict[str, Any], mechanism: str) -> tuple[str, str]:
    """Extract the ``{url, token}`` pair from an SDM GenerateImage result.

    Both ``CameraImage.GenerateImage`` and ``CameraEventImage.GenerateImage``
    return ``{"results": {"url": ..., "token": ...}}``. Missing or
    malformed fields raise a device-error so the verb fails cleanly with
    exit 1 rather than crashing.

    Reviewer feedback (C4): the raw ``result`` dict can carry a
    short-lived SDM auth token (or a URL with ``?auth=<token>``). Putting
    it in ``StructuredError.details`` exposes that token via stderr and
    risks leaking into bug reports. Surface only the *shape* of the
    response (sorted key list) instead of the values.
    """
    inner = result.get("results")
    if not isinstance(inner, dict):
        raise StructuredError(
            code=EXIT_DEVICE_ERROR,
            message=(
                f"SDM GenerateImage ({mechanism}) returned malformed result: "
                "missing 'results' object"
            ),
            details={"mechanism": mechanism, "result_keys": sorted(result.keys())},
        )
    url = inner.get("url")
    token = inner.get("token")
    if not isinstance(url, str) or not url:
        raise StructuredError(
            code=EXIT_DEVICE_ERROR,
            message=f"SDM GenerateImage ({mechanism}) result missing 'url'",
            details={"mechanism": mechanism, "result_keys": sorted(inner.keys())},
        )
    if not isinstance(token, str) or not token:
        raise StructuredError(
            code=EXIT_DEVICE_ERROR,
            message=f"SDM GenerateImage ({mechanism}) result missing 'token'",
            details={"mechanism": mechanism},
        )
    return url, token


def _download_snapshot_bytes(url: str, token: str) -> bytes:
    """GET ``url`` with the SDM-issued token and return the JPEG bytes.

    Per SDM's CameraImage / CameraEventImage docs, the issued token is
    passed as ``Authorization: Basic <token>``. Network failures map to
    exit 3 (network); HTTP non-2xx maps to exit 1 (device error) since
    the SDM token is short-lived and a non-2xx here means Google
    rejected our follow-up retrieval, which is a per-device condition.

    Reviewer feedback (C4): never put the raw URL in error output —
    SDM image URLs sometimes carry ``?auth=<token>`` in the query
    string. Strip the query string before any error path uses the URL.
    """
    redacted = url.split("?")[0] + ("?<redacted>" if "?" in url else "")
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Basic {token}"},
            timeout=_SNAPSHOT_IMAGE_TIMEOUT_S,
        )
    except requests.exceptions.ConnectionError as exc:
        raise StructuredError(
            code=EXIT_NETWORK_ERROR,
            message=f"network error fetching snapshot bytes: {exc}",
            hint="Check your internet connection and retry.",
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise StructuredError(
            code=EXIT_NETWORK_ERROR,
            message=(f"timed out fetching snapshot bytes after {_SNAPSHOT_IMAGE_TIMEOUT_S}s"),
            hint="Google's image-delivery endpoint is slow; retry shortly.",
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise StructuredError(
            code=EXIT_NETWORK_ERROR,
            message=f"unexpected requests error fetching snapshot bytes: {exc}",
        ) from exc

    if response.status_code != 200:
        raise StructuredError(
            code=EXIT_DEVICE_ERROR,
            message=(f"snapshot image fetch returned HTTP {response.status_code} for {redacted}"),
            details={"status_code": response.status_code, "url": redacted},
        )
    return response.content

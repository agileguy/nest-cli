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

from typing import Any

import click

from nest_cli.cli._shared import (
    exit_on_structured_error,
    filter_aliases_by_family,
    load_credentials_or_exit,
)
from nest_cli.cli.list_cmd import _probe_records
from nest_cli.config import default_config_path, load_config, resolve_alias
from nest_cli.errors import EXIT_UNSUPPORTED_FEATURE, StructuredError
from nest_cli.output import OutputMode, add_output_options, emit
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
    "sdm.devices.traits.DoorbellChime": ["chime"],
    # Future phases extend this table — Engineer B owns the stream / events rows:
    # "sdm.devices.traits.CameraLiveStream": ["stream", "stream-extend", "stream-stop"],
    # "sdm.devices.traits.CameraMotion": ["events"],
    # "sdm.devices.traits.CameraPerson": ["events"],
    # "sdm.devices.traits.CameraSound": ["events"],
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


def _doorbell_capable_hint(failing_target: str, output_mode: OutputMode) -> str:
    """Return the FR-CAM-16 hint enumerating doorbell-capable aliases.

    Walks the ``[aliases]`` table and queries each cam target to see
    which carry the ``DoorbellChime`` trait. Aliases that fail to fetch
    (404, network, etc.) are silently skipped so a single broken alias
    doesn't block the hint. ``failing_target`` is excluded from the
    result (we already know it isn't doorbell-capable). If no aliases
    are doorbell-capable (or there's no config), return a generic hint.
    """
    try:
        config = load_config(default_config_path())
    except StructuredError:
        return (
            "Run `nest-cli cam capabilities <target>` against your other "
            "cameras to find one with DoorbellChime."
        )

    cam_aliases = filter_aliases_by_family(config.aliases, "cam")
    if not cam_aliases:
        return (
            "Run `nest-cli cam capabilities <target>` against your other "
            "cameras to find one with DoorbellChime."
        )

    creds = load_credentials_or_exit(output_mode)
    client = SdmClient(creds)
    capable: list[str] = []
    for alias_name, alias_target in sorted(cam_aliases.items()):
        if alias_name == failing_target or alias_target == failing_target:
            continue
        try:
            cam = client.get_device(alias_target)
        except StructuredError:
            continue
        if cam.has_trait(_TRAIT_DOORBELL_CHIME):
            capable.append(alias_name)

    if not capable:
        return (
            "No doorbell-capable cameras found in your config. The chime "
            "verb only works on devices carrying sdm.devices.traits.DoorbellChime."
        )
    return f"Doorbell-capable aliases in your config: {', '.join(capable)}."

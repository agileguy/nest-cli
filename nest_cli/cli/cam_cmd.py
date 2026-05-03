"""``nest-cli cam`` subgroup — camera-side commands.

v0.1.0 implements three verbs from SRD §5.3:

- ``cam list`` (FR-CAM-1) — synonym for ``list --family cam``.
- ``cam info <target>`` (FR-CAM-2) — issue SDM ``devices.get`` and emit
  the §10.1 Camera record.
- ``cam capabilities <target>`` (FR-CAM-28) — emit the camera's traits
  array plus a derived ``supported_verbs`` field listing which
  ``nest-cli cam`` sub-verbs are supported on the device.

Future phases extend the subgroup with ``snapshot``, ``stream``,
``stream-extend``, ``stream-stop``, ``chime``, ``events``, ``battery``,
``signal``. The ``supported_verbs`` mapping table below grows as those
verbs land.

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

import click

from nest_cli.cli._shared import (
    exit_on_structured_error,
    filter_aliases_by_family,
    load_credentials_or_exit,
)
from nest_cli.cli.cam_stream_cmd import cam_stream, cam_stream_extend, cam_stream_stop
from nest_cli.cli.list_cmd import _probe_records
from nest_cli.config import default_config_path, load_config, resolve_alias
from nest_cli.errors import StructuredError
from nest_cli.output import OutputMode, add_output_options, emit
from nest_cli.sdm.client import SdmClient
from nest_cli.sdm.types import Camera

# ---------------------------------------------------------------------------
# Trait → supported-verb mapping (SRD §5.3, FR-CAM-28)
# ---------------------------------------------------------------------------
#
# Each entry maps an SDM trait name to the list of ``nest-cli cam`` verbs
# that depend on it. v0.1.0 lists only verbs that exist in v0.1.0; future
# phases extend the mapping as new verbs land. The SRD names the trait →
# verb relationships in §5.3.2 (snapshot), §5.3.3 (stream), §5.3.5
# (chime), §5.3.8 (events).
#
# Two verbs are universal — every camera supports them — and don't need
# a trait gate:
#
# - ``info``      — every camera responds to ``devices.get``
# - ``capabilities`` — local computation over the trait list
#
# These are added unconditionally to ``supported_verbs``.

_TRAIT_TO_VERBS: dict[str, list[str]] = {
    # Phase 2 stream verbs (Engineer B / FR-CAM-6..14).
    "sdm.devices.traits.CameraLiveStream": ["stream", "stream-extend", "stream-stop"],
    # Phase 2 events verbs (Engineer B / FR-CAM-19..25). Any of the
    # event-emitting traits enables the events verb.
    "sdm.devices.traits.CameraMotion": ["events"],
    "sdm.devices.traits.CameraPerson": ["events"],
    "sdm.devices.traits.CameraSound": ["events"],
    # NOTE: DoorbellChime emits doorbell-press events, but the trait is
    # also the chime-verb gate (Engineer A's work). To avoid a merge
    # collision on the same dict key, this table omits DoorbellChime
    # entirely; Engineer A's branch will land that key with
    # ``["chime"]`` and the PM merger will widen it to
    # ``["chime", "events"]`` at integration time.
    # Phase 2 snapshot / chime / battery / signal verbs land via
    # Engineer A's parallel work; their entries are intentionally not
    # included here so the two parallel branches don't collide on this
    # mapping.
}

# Verbs every camera has (no trait gate).
_UNIVERSAL_VERBS = ("info", "capabilities")


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


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _supported_verbs_for(camera: Camera) -> list[str]:
    """Compute the verb list for ``camera`` from its trait set.

    Algorithm:

    1. Start with the universal verbs (``info``, ``capabilities``).
    2. For each trait the camera has, union in the verbs listed under
       that trait in ``_TRAIT_TO_VERBS``.
    3. De-dupe and return.
    """
    verbs: set[str] = set(_UNIVERSAL_VERBS)
    for trait in camera.traits:
        verbs.update(_TRAIT_TO_VERBS.get(trait.name, ()))
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

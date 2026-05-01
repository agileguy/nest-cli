"""``nest-cli list`` and ``nest-cli discover`` commands.

Implements FR-1, FR-1a, FR-1b, FR-1c, FR-1d, FR-2, FR-2a (SRD §5.1).

Differences between the two verbs:

- ``list`` reads the local config (``[aliases]`` and ``[groups]``) and
  prints what's there. By default it does NOT call any remote API. With
  ``--probe``, each cam target is hit with a ``devices.get`` to populate
  the ``online`` field.
- ``discover`` always calls SDM and prints the live inventory. It
  ignores the config file's alias list — its purpose is to surface every
  device the operator's credentials grant access to, so the inventory
  can be copied into the config by hand.

For v0.1.0, ``--family wifi`` is recognized but exits 5 with a hint
pointing at FR-WIFI-0 — the wifi surface is gated behind
``--experimental-wifi`` and ships in Phase 3.
"""

from __future__ import annotations

from typing import Any

import click

from nest_cli.cli._shared import (
    exit_on_structured_error,
    family_for_target,
    filter_aliases_by_family,
    load_credentials_or_exit,
)
from nest_cli.config import default_config_path, load_config
from nest_cli.errors import (
    EXIT_UNSUPPORTED_FEATURE,
    StructuredError,
)
from nest_cli.output import OutputMode, add_output_options, emit
from nest_cli.sdm.client import SdmClient


@click.command("list")
@click.option(
    "--probe",
    is_flag=True,
    default=False,
    help="Probe each device for liveness; populates the 'online' field.",
)
@click.option(
    "--groups",
    "groups_flag",
    is_flag=True,
    default=False,
    help="List configured groups instead of aliases.",
)
@click.option(
    "--family",
    type=click.Choice(["cam", "wifi"]),
    default=None,
    help="Filter aliases to a specific family (v0.1.0: cam only).",
)
@click.option(
    "--online-only",
    is_flag=True,
    default=False,
    help="Implies --probe; emit only devices that responded.",
)
@add_output_options
def list_cmd(
    probe: bool,
    groups_flag: bool,
    family: str | None,
    online_only: bool,
    output_mode: OutputMode,
) -> None:
    """List aliases and groups from the local config.

    Implements FR-1, FR-1a, FR-1b, FR-1c, FR-1d.

    By default emits the configured aliases as a list of records
    ``{name, target, family}``. With ``--groups``, emits the configured
    groups as ``{name: [member_aliases]}``. With ``--probe`` or
    ``--online-only``, additionally calls SDM ``devices.get`` for each
    cam alias and adds an ``online`` field.
    """
    try:
        config = load_config(default_config_path())
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    if groups_flag:
        # FR-1b: groups view ignores --probe, --family, --online-only.
        emit({k: list(v) for k, v in config.groups.items()}, output_mode)
        return

    aliases = filter_aliases_by_family(config.aliases, family)
    records = [
        {"name": name, "target": target, "family": family_for_target(target)}
        for name, target in aliases.items()
    ]

    if probe or online_only:
        records = _probe_records(records, output_mode)
        if online_only:
            records = [r for r in records if r.get("online") is True]

    emit(records, output_mode)


@click.command("discover")
@click.option(
    "--family",
    type=click.Choice(["cam", "wifi"]),
    default="cam",
    show_default=True,
    help="Family to probe Google for (v0.1.0: cam only).",
)
@add_output_options
def discover_cmd(family: str, output_mode: OutputMode) -> None:
    """Probe Google for the full set of devices the credentials can see.

    Implements FR-2, FR-2a. With ``--family cam`` (the default), the
    verb calls SDM ``devices.list`` and emits every visible device. With
    ``--family wifi`` (v0.1.0), exits 5 — the wifi surface is gated
    behind ``--experimental-wifi`` and ships in Phase 3.
    """
    if family == "wifi":
        exit_on_structured_error(
            StructuredError(
                code=EXIT_UNSUPPORTED_FEATURE,
                message="discover --family wifi requires the experimental wifi surface",
                hint=(
                    "The wifi surface is gated behind --experimental-wifi and "
                    "ships in Phase 3 (FR-WIFI-0)."
                ),
            ),
            output_mode,
        )

    creds = load_credentials_or_exit(output_mode)
    client = SdmClient(creds)
    try:
        cameras = client.list_devices(creds.google_cloud_project_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    if not cameras:
        # FR-3: zero-result with no error exits 0 with empty output and a
        # single INFO log line on stderr.
        click.echo("no devices found", err=True)

    emit(cameras, output_mode)


def _probe_records(
    records: list[dict[str, Any]],
    output_mode: OutputMode,
) -> list[dict[str, Any]]:
    """Probe each cam record for liveness; populate ``online`` field.

    On credentials failure, emits a structured error to stderr and
    exits with the auth-mapped code rather than partially-probing.
    """
    cam_targets = [r["target"] for r in records if r["family"] == "cam"]
    if not cam_targets:
        return records

    creds = load_credentials_or_exit(output_mode)
    client = SdmClient(creds)
    enriched: list[dict[str, Any]] = []
    for record in records:
        if record["family"] != "cam":
            enriched.append(record)
            continue
        try:
            client.get_device(record["target"])
            online = True
        except StructuredError:
            online = False
        enriched.append({**record, "online": online})
    return enriched

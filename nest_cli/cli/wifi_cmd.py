"""``nest-cli wifi`` subgroup — Foyer-backed wifi commands (experimental).

Phase 3 ships read-only ``wifi list`` verbs (FR-WIFI-1..3):

- ``wifi list groups``        — every mesh group on the operator's account.
- ``wifi list points <group>``— every router/point in the named group.
- ``wifi list clients <group>``— every connected station in the named group.

Action verbs (pause / unpause / prioritize / group-assign — FR-WIFI-4..7),
the speedtest, reboot, network-info, and point-health verbs land in a
follow-up commit (Engineer B).

Experimental gate
-----------------

Per FR-WIFI-0, every ``wifi`` sub-verb requires ``--experimental-wifi``
on each invocation. The gate is enforced by ``_experimental_wifi_gate``;
without the flag, the verb exits 64 with a hint pointing at SRD §3.2.3.
The flag is intentionally not settable in config (SRD Decision 13) — the
per-invocation friction is the point.

Family error envelope
---------------------

Wifi verbs emit ``StructuredError`` with ``family="wifi"`` so the SRD
§11.3 envelope carries the discriminator. Operators piping JSONL through
``jq 'select(.family == "wifi")'`` filter cleanly. Cam-side verbs ship
without ``family`` for v0.1.0 / v0.2.x back-compat (documented deviation
in ARCHITECTURE.md).
"""

from __future__ import annotations

import click

from nest_cli.auth.wifi_credentials import (
    WifiCredentialError,
    default_wifi_credentials_path,
    load_wifi_credentials,
)
from nest_cli.cli._shared import exit_on_structured_error
from nest_cli.errors import (
    EXIT_USAGE_ERROR,
    StructuredError,
)
from nest_cli.output import OutputMode, add_output_options, emit
from nest_cli.wifi.client import FoyerClient

# Hint pointing operators at the SRD section that explains the wifi
# experimental-flag posture. Same string as the auth wifi verbs use, so
# operators see consistent guidance regardless of which verb they ran.
_EXPERIMENTAL_WIFI_HINT = (
    "Pass --experimental-wifi to acknowledge SRD §3.2.3 — the wifi side "
    "wraps single-maintainer reverse-engineered libraries that break when "
    "Google rotates Foyer endpoints. The flag's friction is the feature."
)


def _experimental_wifi_gate_or_exit(
    experimental_wifi: bool, output_mode: OutputMode, *, verb: str
) -> None:
    """Exit 64 with FR-WIFI-0 hint unless ``--experimental-wifi`` was passed.

    SRD §11.2 also names exit 5 for this case, but FR-WIFI-0 is the
    more specific requirement (says exit 64). We follow the FR-WIFI-0
    wording — ARCHITECTURE.md notes the §11.2 vs FR-WIFI-0 resolution.
    """
    if experimental_wifi:
        return
    exit_on_structured_error(
        StructuredError(
            code=EXIT_USAGE_ERROR,
            message=(
                f"`wifi {verb}` requires --experimental-wifi (FR-WIFI-0). "
                "The wifi side wraps reverse-engineered single-maintainer "
                "libraries that break when Google rotates Foyer endpoints; "
                "every invocation must explicitly opt in."
            ),
            hint=_EXPERIMENTAL_WIFI_HINT,
            family="wifi",
        ),
        output_mode,
    )


def _load_wifi_creds_or_exit(output_mode: OutputMode) -> str:
    """Load the wifi master token from credentials-wifi.json or exit cleanly.

    Returns just the master token (the FoyerClient only needs that;
    the email is operator metadata for ``auth status``). Failure paths
    surface as ``StructuredError(family="wifi")``.
    """
    creds_path = default_wifi_credentials_path()
    try:
        creds = load_wifi_credentials(creds_path)
    except WifiCredentialError as exc:
        exit_on_structured_error(
            StructuredError(
                code=exc.exit_code,
                message=str(exc),
                hint=exc.hint,
                family="wifi",
            ),
            output_mode,
        )
    return creds.master_token


# ---------------------------------------------------------------------------
# Click groups
# ---------------------------------------------------------------------------


wifi_group = click.Group(
    name="wifi",
    help=(
        "Nest Wi-Fi commands (Foyer, EXPERIMENTAL). Every sub-verb requires "
        "--experimental-wifi per invocation. See SRD §3.2.3 for rationale."
    ),
)

wifi_list_group = click.Group(
    name="list",
    help="List Wi-Fi groups, points, or clients.",
)
wifi_group.add_command(wifi_list_group)


# ---------------------------------------------------------------------------
# wifi list groups (FR-WIFI-1)
# ---------------------------------------------------------------------------


@wifi_list_group.command("groups")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_list_groups(experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Emit every Wi-Fi mesh group the operator's account owns.

    Implements FR-WIFI-1. Output is one §10.6 WifiGroup record per group.
    Empty inventory exits 0 with empty output (FR-3 mirror).
    """
    _experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="list groups")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        groups = client.list_groups()
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit([g.model_dump(mode="json") for g in groups], output_mode)


# ---------------------------------------------------------------------------
# wifi list points <group> (FR-WIFI-2)
# ---------------------------------------------------------------------------


@wifi_list_group.command("points")
@click.argument("group_id", metavar="<group-id>")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_list_points(group_id: str, experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Emit every router/point in the named group (FR-WIFI-2).

    Group not found → exit 4 (family=wifi). Output is one §10.7
    WifiPoint record per point in deterministic id-ascending order.
    """
    _experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="list points")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        points = client.list_points(group_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit([p.model_dump(mode="json") for p in points], output_mode)


# ---------------------------------------------------------------------------
# wifi list clients <group> (FR-WIFI-3)
# ---------------------------------------------------------------------------


@wifi_list_group.command("clients")
@click.argument("group_id", metavar="<group-id>")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_list_clients(group_id: str, experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Emit every connected client in the named group (FR-WIFI-3).

    Group not found → exit 4 (family=wifi). Output is one §10.8
    WifiClient record per station in deterministic id-ascending order.
    The ``paused``, ``priority_until``, ``band``, and ``group_assignment``
    fields are normalized from the upstream Foyer payload.
    """
    _experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="list clients")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        clients = client.list_clients(group_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit([c.model_dump(mode="json") for c in clients], output_mode)


# Re-export helpers for the test modules — keeps the public surface
# (which is just ``wifi_group``) clean while still letting tests poke
# at the gate / cred loader without importing private names.
__all__ = ["wifi_group"]

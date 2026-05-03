"""``nest-cli wifi`` subgroup — Foyer-backed wifi commands (experimental).

Phase 3A shipped read-only ``wifi list`` verbs (FR-WIFI-1..3):

- ``wifi list groups``        — every mesh group on the operator's account.
- ``wifi list points <group>``— every router/point in the named group.
- ``wifi list clients <group>``— every connected station in the named group.

Phase 3B (this module's additions) — per-client action verbs (FR-WIFI-4..7):

- ``wifi pause <client-id>``         — pause a single client.
- ``wifi unpause <client-id>``       — unpause a single client.
- ``wifi prioritize <client-id> --duration <minutes>``
                                     — boost a single client (1..240 min).
- ``wifi group-assign <client-id> --group <family|parental|guest|none>``
                                     — assign a client to a Foyer group.
                                       Currently exits 5 — upstream
                                       googlewifi gap; see ``set_station_group``
                                       in ``nest_cli/wifi/client.py``.

The speedtest, reboot, network-info, and point-health verbs land in
Phase 3.1.

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
from nest_cli.cli._shared import (
    exit_on_structured_error,
    experimental_wifi_gate_or_exit,
)
from nest_cli.errors import StructuredError
from nest_cli.output import OutputMode, add_output_options, emit
from nest_cli.wifi.client import FoyerClient


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
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi list groups")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        groups = client.list_groups()
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(groups, output_mode)


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
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi list points")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        points = client.list_points(group_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(points, output_mode)


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
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi list clients")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        clients = client.list_clients(group_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(clients, output_mode)


# ---------------------------------------------------------------------------
# wifi pause <client-id> (FR-WIFI-4)
# ---------------------------------------------------------------------------


@wifi_group.command("pause")
@click.argument("client_id", metavar="<client-id>")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_pause(client_id: str, experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Pause a single client by station id (FR-WIFI-4).

    Idempotent — pausing an already-paused client returns OK with no
    error. Unknown client_id → exit 4 (family=wifi). Output on success
    is ``{"client_id": ..., "action": "pause", "result": "ok"}``.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi pause")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        client.pause_station(client_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit({"client_id": client_id, "action": "pause", "result": "ok"}, output_mode)


# ---------------------------------------------------------------------------
# wifi unpause <client-id> (FR-WIFI-5)
# ---------------------------------------------------------------------------


@wifi_group.command("unpause")
@click.argument("client_id", metavar="<client-id>")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_unpause(client_id: str, experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Unpause a single client by station id (FR-WIFI-5).

    Idempotent — unpausing an already-unpaused client returns OK.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi unpause")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        client.unpause_station(client_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit({"client_id": client_id, "action": "unpause", "result": "ok"}, output_mode)


# ---------------------------------------------------------------------------
# wifi prioritize <client-id> --duration <minutes> (FR-WIFI-6)
# ---------------------------------------------------------------------------


@wifi_group.command("prioritize")
@click.argument("client_id", metavar="<client-id>")
@click.option(
    "--duration",
    "duration_minutes",
    type=click.IntRange(1, 240),
    default=60,
    show_default=True,
    help="Boost duration in minutes. Min 1, max 240 (Foyer-imposed).",
)
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_prioritize(
    client_id: str,
    duration_minutes: int,
    experimental_wifi: bool,
    output_mode: OutputMode,
) -> None:
    """Prioritize a single client for ``--duration`` minutes (FR-WIFI-6).

    Default duration is 60 minutes. Foyer's underlying API takes hours;
    the FoyerClient ceil-converts minutes → hours (so 45 min still
    requests a 1-hour boost rather than rounding to zero).
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi prioritize")
    master_token = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(master_token=master_token)
        client.prioritize_station(client_id, duration_minutes)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(
        {
            "client_id": client_id,
            "action": "prioritize",
            "duration_minutes": duration_minutes,
            "result": "ok",
        },
        output_mode,
    )


# ---------------------------------------------------------------------------
# wifi group-assign <client-id> --group <family|parental|guest|none> (FR-WIFI-7)
# ---------------------------------------------------------------------------


@wifi_group.command("group-assign")
@click.argument("client_id", metavar="<client-id>")
@click.option(
    "--group",
    "group",
    type=click.Choice(
        ["family", "parental", "guest", "none"],
        case_sensitive=False,
    ),
    required=True,
    help="Group to assign the client to. ``none`` removes the assignment.",
)
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_group_assign(
    client_id: str,
    group: str,
    experimental_wifi: bool,
    output_mode: OutputMode,
) -> None:
    """Assign a client to a Foyer group (FR-WIFI-7).

    Phase 3B status: the upstream ``googlewifi`` library does not
    currently expose a group-assign method. This verb wires through to
    the FoyerClient, which raises exit 5 (unsupported_feature, family=wifi)
    with a hint pointing at the upstream gap. The CLI surface is shipped
    so operator scripts can target the verb today; once upstream lands
    a ``set_station_group`` method, this verb starts succeeding without
    any operator-visible interface change.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi group-assign")
    master_token = _load_wifi_creds_or_exit(output_mode)
    # Click's case_sensitive=False already lowercased the group value.
    requested_group: str | None = None if group == "none" else group
    try:
        client = FoyerClient(master_token=master_token)
        client.set_station_group(client_id, requested_group)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(
        {
            "client_id": client_id,
            "action": "group-assign",
            "group": requested_group,
            "result": "ok",
        },
        output_mode,
    )


# Re-export helpers for the test modules — keeps the public surface
# (which is just ``wifi_group``) clean while still letting tests poke
# at the gate / cred loader without importing private names.
__all__ = ["wifi_group"]

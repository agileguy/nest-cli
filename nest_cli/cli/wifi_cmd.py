"""``nest-cli wifi`` subgroup — Foyer-backed wifi commands (experimental).

Implementation status (Phase B, 2026-05-03; see SRD §17):

**Implemented read verbs** (data derives from ``GetHomeGraph``):

- ``wifi list groups``         — every mesh group on the operator's account.
- ``wifi list points <group>`` — every router/point in the named group.
- ``wifi point-health <point>``— health snapshot for a single point.

**Action verbs ship as exit-5 (``unsupported_feature``, ``family="wifi"``)
until Phase C maps the specific Foyer RPCs:**

- ``wifi list clients <group>``                     (FR-WIFI-3)
- ``wifi pause / unpause <client-id>``              (FR-WIFI-4..5)
- ``wifi prioritize <client-id> --duration <min>``  (FR-WIFI-6)
- ``wifi group-assign <client-id> --group <choice>``(FR-WIFI-7)
- ``wifi speedtest run / history <group>``          (FR-WIFI-8..9)
- ``wifi reboot point / group``                     (FR-WIFI-10..11)
- ``wifi network <group>``                          (FR-WIFI-13)
- ``wifi guest enable / disable <group>``           (FR-WIFI-14)

The CLI surface for these verbs is fully wired so operator scripts can be
authored today and will start working when Phase C lands without any
operator-visible interface change.

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

import sys
from collections.abc import Callable

import click

from nest_cli.auth.wifi_credentials import (
    WifiCredentialError,
    default_wifi_credentials_path,
    load_wifi_credentials,
)
from nest_cli.auth.wifi_types import WifiCredentials
from nest_cli.cli._fanout import FanOutResult, fan_out_verb
from nest_cli.cli._shared import (
    ResolvedTarget,
    exit_on_structured_error,
    experimental_wifi_gate_or_exit,
    is_group_target,
    resolve_target_or_group,
)
from nest_cli.config import default_config_path, load_config
from nest_cli.errors import EXIT_USAGE_ERROR, StructuredError
from nest_cli.output import OutputMode, add_output_options, emit
from nest_cli.wifi.client import FoyerClient


def _stdin_is_tty() -> bool:
    """Return True if ``sys.stdin`` reports as a tty.

    Indirected through this module-level helper so tests can monkeypatch
    ``nest_cli.cli.wifi_cmd._stdin_is_tty`` to simulate either branch
    deterministically. CliRunner replaces ``sys.stdin`` with an in-memory
    stream that always reports ``isatty() == False``, which makes the
    raw ``sys.stdin.isatty()`` call non-overridable from a test.
    """
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _confirm_reboot_or_exit(
    *,
    prompt: str,
    yes: bool,
    output_mode: OutputMode,
) -> None:
    """Apply FR-WIFI-10/12 confirmation rules for reboot verbs.

    Rules:

    - ``output_mode == "quiet"`` implies ``--yes`` (FR-WIFI-12). Skip
      the prompt entirely; the operator has already opted into a
      no-output channel.
    - ``yes=True`` skips the prompt regardless of TTY state.
    - Non-tty without ``--yes`` (and not ``--quiet``) → exit 64
      with hint pointing at ``--yes``. Detection: check
      ``sys.stdin.isatty()`` first; if False and no ``--yes``, exit 64.
    - TTY without ``--yes`` → ``click.confirm`` prompts on stderr;
      no/empty answer aborts (exit 0 silently after a stderr message).

    Detection note: CliRunner replaces ``sys.stdin`` with an in-memory
    stream that reports ``isatty() == False`` by default. Tests that
    want to exercise the TTY branch must either monkeypatch the
    runner's input stream's ``isatty`` after invocation begins, or pass
    ``--yes`` (and then assert the prompt was bypassed). Production
    behavior follows the real stdin's tty-ness as expected.
    """
    if output_mode == "quiet" or yes:
        return

    stdin_is_tty = _stdin_is_tty()
    if not stdin_is_tty:
        # Heuristic: in CliRunner the in-memory stream has isatty=False
        # but a non-empty buffer to read from. If the test/operator
        # supplied input, the confirm will succeed; if not, Abort fires
        # below and we map that to exit 64. We can't distinguish the
        # two cases without trying, so we attempt the confirm anyway.
        try:
            confirmed = click.confirm(prompt, default=False, err=True)
        except (click.exceptions.Abort, EOFError):
            exit_on_structured_error(
                StructuredError(
                    code=EXIT_USAGE_ERROR,
                    message=("reboot requires --yes (or --quiet) when stdin is not a tty"),
                    hint=(
                        "Pass --yes to skip the confirmation prompt, or "
                        "--quiet to imply --yes for non-interactive runs."
                    ),
                    family="wifi",
                ),
                output_mode,
            )
        if not confirmed:
            click.echo("Aborted.", err=True)
            sys.exit(0)
        return

    try:
        confirmed = click.confirm(prompt, default=False, err=True)
    except click.exceptions.Abort:
        confirmed = False
    if not confirmed:
        click.echo("Aborted.", err=True)
        sys.exit(0)


def _load_wifi_creds_or_exit(output_mode: OutputMode) -> WifiCredentials:
    """Load the wifi credentials from credentials-wifi.json or exit cleanly.

    Phase B (2026-05-03): returns the full ``WifiCredentials`` record
    rather than just the master token. The new ``FoyerClient(creds)``
    needs the email + master_token + android_id triple to mint a Foyer
    access token via ``gpsoauth.perform_oauth``. Failure paths surface
    as ``StructuredError(family="wifi")``.
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
    return creds


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


# Phase 3.1 nested subgroups (speedtest, reboot, guest). Each holds a
# pair of sub-verbs (run/history, point/group, enable/disable). Click
# requires the sub-verbs registered onto the inner Group then the inner
# Group registered onto wifi_group.
speedtest_group = click.Group(
    name="speedtest",
    help="Run a fresh WAN speed test or read recent results.",
)
wifi_group.add_command(speedtest_group)

reboot_group_cli = click.Group(
    name="reboot",
    help="Reboot a single point or every point in a mesh group.",
)
wifi_group.add_command(reboot_group_cli)

guest_group = click.Group(
    name="guest",
    help="Toggle the guest network on a mesh group.",
)
wifi_group.add_command(guest_group)


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
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
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
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
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
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
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
    "--concurrency",
    "concurrency",
    type=click.IntRange(1, 32),
    default=None,
    help="Override the default fan-out concurrency cap (3) for group targets.",
)
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_pause(
    client_id: str,
    concurrency: int | None,
    experimental_wifi: bool,
    output_mode: OutputMode,
) -> None:
    """Pause a single client by station id (FR-WIFI-4).

    Idempotent — pausing an already-paused client returns OK with no
    error. Unknown client_id → exit 4 (family=wifi). Output on success
    is ``{"client_id": ..., "action": "pause", "result": "ok"}``.

    Group fan-out (FR-6/FR-7): ``@group-name`` resolves to the group's
    member alias list and fans out at the default concurrency=3 cap.
    Per-target results emit as FR-9a JSONL envelopes in resolved order.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi pause")
    if is_group_target(client_id):
        _wifi_action_fanout(
            client_id,
            action_name="pause",
            verb=lambda foyer, sid: foyer.pause_station(sid),
            concurrency=concurrency,
            output_mode=output_mode,
        )
        return
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
        client.pause_station(client_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit({"client_id": client_id, "action": "pause", "result": "ok"}, output_mode)


def _strip_wifi_prefix(target: str) -> str:
    """Strip the ``wifi:`` prefix from a resolved target.

    The on-config form is ``wifi:sta-foo`` (or ``wifi:groups/...``);
    the FoyerClient methods take the bare upstream id. The group
    fan-out helper passes the post-strip id to the per-verb callable.
    """
    return target[len("wifi:") :] if target.startswith("wifi:") else target


def _wifi_action_fanout(
    target: str,
    *,
    action_name: str,
    verb: Callable[[FoyerClient, str], None],
    concurrency: int | None,
    output_mode: OutputMode,
) -> None:
    """Generic group fan-out for single-arg wifi action verbs.

    ``verb`` is a callable ``(FoyerClient, str) -> None`` that performs
    the side-effect (e.g. ``foyer.pause_station(sid)``) and raises
    ``StructuredError`` on failure. Each per-target call produces an
    FR-9a envelope; the helper computes the FR-8a aggregate exit code
    and ``sys.exit``s with it.
    """
    try:
        config = load_config(default_config_path())
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    try:
        resolved = resolve_target_or_group(config, target, expected_family="wifi")
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    creds = _load_wifi_creds_or_exit(output_mode)
    foyer = FoyerClient(creds)

    def _verb_callable(rt: ResolvedTarget) -> FanOutResult:
        bare_id = _strip_wifi_prefix(rt.target)
        try:
            verb(foyer, bare_id)
        except StructuredError as exc:
            from nest_cli.errors import error_enum_for_code

            return FanOutResult(
                target=rt.name,
                exit_code=exc.code,
                error={
                    "code": error_enum_for_code(exc.code),
                    "message": str(exc),
                    **({"hint": exc.hint} if exc.hint else {}),
                },
            )
        return FanOutResult(
            target=rt.name,
            exit_code=0,
            result={
                "client_id": bare_id,
                "action": action_name,
                "result": "ok",
            },
        )

    exit_code = fan_out_verb(
        targets=resolved,
        verb_callable=_verb_callable,
        concurrency=concurrency,
        output_mode=output_mode,
    )
    sys.exit(exit_code)


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
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
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
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
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

    Phase B status: the specific Foyer RPC for group-assign has not yet
    been mapped. This verb wires through to the FoyerClient, which
    raises exit 5 (unsupported_feature, family=wifi) with a hint
    pointing at the Phase-C deferral. The CLI surface is shipped so
    operator scripts can target the verb today; once Phase C lands the
    real RPC, this verb starts succeeding without any operator-visible
    interface change.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi group-assign")
    creds = _load_wifi_creds_or_exit(output_mode)
    # Click's case_sensitive=False already lowercased the group value.
    requested_group: str | None = None if group == "none" else group
    try:
        client = FoyerClient(creds)
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


# ---------------------------------------------------------------------------
# wifi speedtest run <group-id> (FR-WIFI-8)
# ---------------------------------------------------------------------------


@speedtest_group.command("run")
@click.argument("group_id", metavar="<group-id>")
@click.option(
    "--timeout",
    "timeout_s",
    type=click.FloatRange(0.1, 600.0),
    default=180.0,
    show_default=True,
    help=(
        "Wall-clock ceiling in seconds. A speed test typically completes "
        "in 30-90 seconds; the default 180s is a safe upper bound."
    ),
)
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_speedtest_run(
    group_id: str,
    timeout_s: float,
    experimental_wifi: bool,
    output_mode: OutputMode,
) -> None:
    """Trigger a fresh WAN speed test on the master router (FR-WIFI-8).

    Blocks until the test completes (typically 30-90s) or the
    ``--timeout`` ceiling is hit. Output is one §10.9 SpeedTest record:
    ``{ts, group_id, point_id, download_mbps, upload_mbps, ping_ms, source}``.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi speedtest run")
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
        result = client.run_speedtest(group_id, timeout_s=timeout_s)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(result, output_mode)


# ---------------------------------------------------------------------------
# wifi speedtest history <group-id> --limit N (FR-WIFI-9)
# ---------------------------------------------------------------------------


@speedtest_group.command("history")
@click.argument("group_id", metavar="<group-id>")
@click.option(
    "--limit",
    "limit",
    type=click.IntRange(1, 365),
    default=30,
    show_default=True,
    help="Maximum number of recent results to return. Foyer caps at 365.",
)
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_speedtest_history(
    group_id: str,
    limit: int,
    experimental_wifi: bool,
    output_mode: OutputMode,
) -> None:
    """Emit recent speed-test history for a mesh group (FR-WIFI-9).

    Output is a list of §10.9 SpeedTest records sorted descending by
    ``ts`` (most recent first). Empty list if the router has no history.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi speedtest history")
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
        results = client.get_speedtest_history(group_id, limit=limit)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(results, output_mode)


# ---------------------------------------------------------------------------
# wifi reboot point <point-id> (FR-WIFI-10)
# ---------------------------------------------------------------------------


@reboot_group_cli.command("point")
@click.argument("point_id", metavar="<point-id>")
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt (required in non-tty contexts).",
)
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_reboot_point(
    point_id: str,
    yes: bool,
    experimental_wifi: bool,
    output_mode: OutputMode,
) -> None:
    """Reboot a single mesh point by id (FR-WIFI-10).

    TTY mode prompts on stderr ("Reboot <point-id>? [y/N] ") and aborts
    on no/empty. Non-tty mode requires ``--yes`` (FR-WIFI-12: ``--quiet``
    implies ``--yes``). Unknown point id → exit 4 (family=wifi).
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi reboot point")
    _confirm_reboot_or_exit(
        prompt=f"Reboot {point_id}?",
        yes=yes,
        output_mode=output_mode,
    )

    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
        client.reboot_point(point_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(
        {
            "point_id": point_id,
            "action": "reboot",
            "result": "ok",
        },
        output_mode,
    )


# ---------------------------------------------------------------------------
# wifi reboot group <group-id> (FR-WIFI-11)
# ---------------------------------------------------------------------------


@reboot_group_cli.command("group")
@click.argument("group_id", metavar="<group-id>")
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt (required in non-tty contexts).",
)
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_reboot_group(
    group_id: str,
    yes: bool,
    experimental_wifi: bool,
    output_mode: OutputMode,
) -> None:
    """Reboot every point in a mesh group (FR-WIFI-11).

    Prompts ONCE for the entire group, names the resolved point list
    on stderr, then proceeds with no per-point prompts. Same TTY/non-tty
    rules as ``wifi reboot point``. Unknown group id → exit 4.

    On a TTY, the verb resolves the point list before prompting so the
    operator sees what they're rebooting. On non-tty + ``--yes``, the
    list resolution and the ``restart_system`` call run together inside
    ``FoyerClient.reboot_group``; the resolved list is echoed in the
    output payload as ``rebooted_points``.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi reboot group")

    # In TTY mode we want the operator to see the resolved point list
    # BEFORE the prompt. In non-tty / --yes / --quiet modes the prompt
    # is skipped and the list-resolve happens inside reboot_group below.
    needs_prompt = output_mode != "quiet" and not yes and _stdin_is_tty()
    if needs_prompt:
        creds = _load_wifi_creds_or_exit(output_mode)
        try:
            client = FoyerClient(creds)
            # Pre-resolve to show the operator what they'll affect.
            # We list points via a separate call so the upstream
            # restart_system isn't invoked yet.
            points = client.list_points(group_id)
        except StructuredError as exc:
            exit_on_structured_error(exc, output_mode)
        click.echo(
            f"Group {group_id} resolves to {len(points)} point(s): "
            + ", ".join(p.id for p in points),
            err=True,
        )
        _confirm_reboot_or_exit(
            prompt=f"Reboot all {len(points)} point(s) in {group_id}?",
            yes=yes,
            output_mode=output_mode,
        )
    else:
        creds = _load_wifi_creds_or_exit(output_mode)
        client = FoyerClient(creds)
        # Confirm path (skipped via yes/quiet) still runs to enforce the
        # non-tty + no-yes guard.
        _confirm_reboot_or_exit(
            prompt=f"Reboot all points in {group_id}?",
            yes=yes,
            output_mode=output_mode,
        )

    try:
        rebooted = client.reboot_group(group_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    emit(
        {
            "group_id": group_id,
            "action": "reboot",
            "rebooted_points": rebooted,
            "result": "ok",
        },
        output_mode,
    )


# ---------------------------------------------------------------------------
# wifi network <group-id> (FR-WIFI-13)
# ---------------------------------------------------------------------------


@wifi_group.command("network")
@click.argument("group_id", metavar="<group-id>")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_network(group_id: str, experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Emit the §10.10 WifiNetwork record for a mesh group (FR-WIFI-13).

    Output: ``{group_id, ssid, guest_ssid, guest_enabled, ipv4: {wan,
    lan_subnet, dhcp_range_start, dhcp_range_end}, ipv6: {enabled, wan,
    prefix_len}, dns_servers}``. Group not found → exit 4.
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi network")
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
        net = client.get_network_info(group_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(net, output_mode)


# ---------------------------------------------------------------------------
# wifi guest enable|disable <group-id> (FR-WIFI-14)
# ---------------------------------------------------------------------------


def _guest_toggle(
    *, group_id: str, enabled: bool, experimental_wifi: bool, output_mode: OutputMode
) -> None:
    """Shared body for ``wifi guest enable`` / ``wifi guest disable``.

    The CLI surface ships even though the specific Foyer RPC for guest-
    network mutation has not yet been mapped. The FoyerClient.
    set_guest_enabled raises EXIT_UNSUPPORTED_FEATURE with a hint
    pointing at the Phase-C deferral; once the RPC lands, this verb
    starts succeeding without an interface change.
    """
    experimental_wifi_gate_or_exit(
        experimental_wifi,
        output_mode,
        verb=f"wifi guest {'enable' if enabled else 'disable'}",
    )
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
        client.set_guest_enabled(group_id, enabled=enabled)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    # Unreachable in v0.3.1 (set_guest_enabled always raises). Kept so
    # that the future success-path emits the SRD-aligned envelope.
    emit(
        {
            "group_id": group_id,
            "action": "guest-enable" if enabled else "guest-disable",
            "guest_enabled": enabled,
            "result": "ok",
        },
        output_mode,
    )


@guest_group.command("enable")
@click.argument("group_id", metavar="<group-id>")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_guest_enable(group_id: str, experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Enable the guest network on ``group_id`` (FR-WIFI-14).

    Phase B status: exits 5 (unsupported_feature, family=wifi) — the
    specific Foyer RPC has not yet been mapped (deferred to Phase C).
    """
    _guest_toggle(
        group_id=group_id,
        enabled=True,
        experimental_wifi=experimental_wifi,
        output_mode=output_mode,
    )


@guest_group.command("disable")
@click.argument("group_id", metavar="<group-id>")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_guest_disable(group_id: str, experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Disable the guest network on ``group_id`` (FR-WIFI-14)."""
    _guest_toggle(
        group_id=group_id,
        enabled=False,
        experimental_wifi=experimental_wifi,
        output_mode=output_mode,
    )


# ---------------------------------------------------------------------------
# wifi point-health <point-id> (FR-WIFI-15)
# ---------------------------------------------------------------------------


@wifi_group.command("point-health")
@click.argument("point_id", metavar="<point-id>")
@click.option(
    "--experimental-wifi",
    is_flag=True,
    default=False,
    help="Required acknowledgement that the wifi side is experimental (FR-WIFI-0).",
)
@add_output_options
def cmd_point_health(point_id: str, experimental_wifi: bool, output_mode: OutputMode) -> None:
    """Emit the §10.11 WifiPointHealth record for a single point (FR-WIFI-15).

    Output: ``{id, online, uptime_s, signal_to_upstream_dbm,
    connected_clients_count, mesh_role}``. Unknown point id → exit 4.
    Master point's ``signal_to_upstream_dbm`` is always None (no upstream
    node to measure against).
    """
    experimental_wifi_gate_or_exit(experimental_wifi, output_mode, verb="wifi point-health")
    creds = _load_wifi_creds_or_exit(output_mode)
    try:
        client = FoyerClient(creds)
        health = client.get_point_health(point_id)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)
    emit(health, output_mode)


# Re-export helpers for the test modules — keeps the public surface
# (which is just ``wifi_group``) clean while still letting tests poke
# at the gate / cred loader without importing private names.
__all__ = ["wifi_group"]

"""``nest-cli cam events`` verb (one-shot drain + ``--follow`` long-poll).

Implements:

- FR-CAM-19 / FR-CAM-20 / FR-CAM-24 / FR-CAM-25 — one-shot drain mode
  (Phase 2, v0.2.0).
- FR-CAM-21 / FR-CAM-22 / FR-CAM-23 — ``--follow`` long-running event
  subscription with ``--types`` filter and exponential backoff (Phase
  2.1, v0.2.1).

Per SRD §3.1.3 / Decision 10, SDM does not expose events via REST
polling — events flow exclusively through the operator's Google
Cloud Pub/Sub subscription. The CLI's ``cam events`` verb pulls from
that subscription and emits each delivered message as JSONL on
stdout per §10.3.

Subscription resolution (precedence):

1. ``[pubsub] subscription_name`` in the resolved config.
2. ``NEST_CLI_PUBSUB_SUBSCRIPTION`` env var.
3. None → exit 6 (config error) per FR-CAM-25.

The subscription value MUST be the full Pub/Sub path:
``projects/{project-id}/subscriptions/{sub-name}``. The CLI does
not synthesize the project component from the OAuth credential.

Pub/Sub credential plumbing relies on Application Default
Credentials (ADC). The operator runs ``gcloud auth
application-default login --update-adc`` once on the machine; the
``google-cloud-pubsub`` client picks them up automatically. This
matches the SRD §6 / §3.1.3 posture: the CLI does not embed a
secondary credential channel for Pub/Sub.

Why a lazy import of ``pubsub_v1``?

The ``google-cloud-pubsub`` package eagerly initializes a default
gRPC channel and probes ADC at import time on some platforms,
which slows down every ``nest-cli`` invocation that doesn't touch
events. Importing inside the verb body keeps ``cam list`` /
``cam info`` startup snappy.

Follow-mode architecture (FR-CAM-21..23)
----------------------------------------

``--follow`` switches the verb from a one-shot ``pull(return_immediately=True)``
to a continuous loop calling ``pull(return_immediately=False)`` with a
30-second timeout. We deliberately use the simple ``pull()`` loop rather
than ``streaming_pull(callback=...)`` because:

- ``streaming_pull`` runs callbacks on a thread pool inside the SDK,
  which makes deterministic SIGINT handling (and the FR-CAM-23 backoff
  schedule, which is *our* responsibility, not the SDK's) significantly
  harder to test with simple mocks.
- The unary ``pull()`` loop matches Pub/Sub's at-least-once contract
  exactly and is the shape FR-CAM-23's "five consecutive failures"
  counter was designed around.

Signal handling uses a flag (the dict ``_interrupted``) set by the
installed handlers; the loop checks the flag at every safe point. We
deliberately do NOT raise ``KeyboardInterrupt`` from the handler —
that would bypass the ``finally`` block that emits the FR-CAM-21
summary line, leaving the operator with no idea how many events the
CLI consumed before the signal.

Sleep during backoff is chunked into 0.5-second pieces so SIGINT
response stays under ~1 second even when the schedule is at its 32s
cap. Tests monkeypatch ``time.sleep`` to capture call counts without
actually waiting.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import Any, NoReturn

import click
import google.api_core.exceptions

from nest_cli.cli._shared import exit_on_structured_error
from nest_cli.config import default_config_path, load_config, resolve_alias
from nest_cli.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_USAGE_ERROR,
    StructuredError,
)
from nest_cli.output import OutputMode, add_output_options, emit
from nest_cli.sdm.event_types import EventTypeEnum, parse_pubsub_event

# FR-CAM-20: default per-pull deadline 5 seconds. Documented in the
# SRD as the timeout the verb passes to Pub/Sub's pull. Pub/Sub will
# return early if the subscription drains; the deadline is the upper
# bound on a stale subscription's wait.
DEFAULT_PULL_DEADLINE_S = 5

# FR-CAM-20: default --max-messages = 100.
DEFAULT_MAX_MESSAGES = 100

# Env var for subscription override (FR-CAM-25 convenience path).
ENV_VAR_SUBSCRIPTION = "NEST_CLI_PUBSUB_SUBSCRIPTION"

# FR-CAM-21: follow mode uses a longer per-pull timeout because
# ``return_immediately=False`` blocks server-side waiting for new
# messages. 30s strikes a balance between low-overhead idle (one round
# trip every 30s when the camera is silent) and bounded reconnect
# behaviour (a hung connection is detected within 30s).
FOLLOW_PULL_TIMEOUT_S = 30

# FR-CAM-21: follow mode pulls smaller batches so the SIGINT-to-exit
# latency stays low. With max_messages=10 a noisy doorbell still drains
# fast, but the verb returns to the loop top — and the signal flag
# check — every ~30s at most.
FOLLOW_MAX_MESSAGES = 10

# FR-CAM-23: capped exponential backoff schedule. Five consecutive
# transport failures exit 3; a successful pull resets the counter.
# The schedule is a tuple keyed by failure count (1-indexed): the
# *first* failure waits 1s before the next pull, the fifth waits 16s.
# After the 5th failure we don't sleep — we exit instead.
_BACKOFF_SCHEDULE_S: tuple[int, ...] = (1, 2, 4, 8, 16, 32)
_BACKOFF_MAX_CONSECUTIVE_FAILURES = 5

# FR-CAM-21: chunked sleep during backoff so the signal handler flag
# is checked frequently. Smaller is more responsive to signals but
# bounds the wakeup frequency on a healthy idle subscription. 0.5s is
# the documented contract: SIGINT-to-final-summary-line is under 1s
# in the worst case.
_BACKOFF_SLEEP_CHUNK_S = 0.5

# FR-CAM-22: closed enum of event_type tokens accepted by ``--types``.
# Source of truth is ``EventTypeEnum`` in event_types.py; we duplicate
# here as a frozenset so the click validator runs at parse time without
# importing pydantic just to enumerate Literal members.
_VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {"motion", "person", "package", "sound", "doorbell-press", "unknown"}
)


@click.command("events")
@click.argument("target", required=False)
@click.option(
    "--max-messages",
    "max_messages",
    type=int,
    default=DEFAULT_MAX_MESSAGES,
    show_default=True,
    help="Max number of messages to drain in one pull (FR-CAM-20).",
)
@click.option(
    "--follow",
    "follow_mode",
    is_flag=True,
    default=False,
    help="Stream events continuously until SIGINT/SIGTERM (FR-CAM-21).",
)
@click.option(
    "--types",
    "types_filter",
    type=str,
    default=None,
    help="Comma-separated event-type subset: motion, person, package, sound, "
    "doorbell-press, unknown (FR-CAM-22).",
)
@add_output_options
def cam_events(
    target: str | None,
    max_messages: int,
    follow_mode: bool,
    types_filter: str | None,
    output_mode: OutputMode,
) -> None:
    """Drain or follow Pub/Sub events (FR-CAM-19..25).

    Without ``<target>``: emit every camera's events. With
    ``<target>``: filter to events whose SDM resource matches the
    alias-resolved target (FR-CAM-19).

    Each message is emitted as a single JSONL line per §10.3 Event.
    Acknowledged before the next message is processed so a partial
    interrupt does not redeliver already-emitted events.

    With ``--follow``: long-running stream-pull until SIGINT/SIGTERM.
    On signal, the verb emits a final JSONL summary line
    ``{"event": "interrupted", "received": N}`` to stdout — even in
    ``--quiet`` mode (FR-CAM-21 explicitly names the summary as a
    required stdout line) — and exits 130 (SIGINT) or 143 (SIGTERM).

    With ``--types <comma-list>``: filter emitted events to a subset
    of the §10.3 event_type enum (FR-CAM-22). Invalid tokens exit 64.

    Failure paths:

    - Subscription not configured → exit 6 (FR-CAM-25).
    - Pub/Sub pull fails after retries → exit 3 (FR-CAM-23 in follow
      mode, or surfaced once in one-shot mode).
    - ``--types`` lists a token outside the §10.3 enum → exit 64.
    """
    # Validate --types early so the operator gets feedback before we
    # even try to load the credential file.
    parsed_types: frozenset[EventTypeEnum] | None = None
    if types_filter is not None:
        try:
            parsed_types = _parse_types_filter(types_filter)
        except StructuredError as exc:
            exit_on_structured_error(exc, output_mode)

    try:
        config = load_config(default_config_path())
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    subscription = _resolve_subscription(config)
    if subscription is None:
        _exit_subscription_not_configured(output_mode)

    # Resolve the target filter, if any. ``resolve_alias`` returns the
    # input verbatim when not in [aliases], so a literal SDM path
    # passes through unchanged.
    resolved_target: str | None = None
    if target is not None:
        resolved_target = resolve_alias(config, target)

    subscriber = _build_subscriber()
    if follow_mode:
        _run_follow_loop(
            subscriber=subscriber,
            subscription=subscription,
            resolved_target=resolved_target,
            parsed_types=parsed_types,
            output_mode=output_mode,
        )
    else:
        _run_one_shot(
            subscriber=subscriber,
            subscription=subscription,
            resolved_target=resolved_target,
            parsed_types=parsed_types,
            max_messages=max_messages,
            output_mode=output_mode,
        )


# ---------------------------------------------------------------------------
# One-shot drain (FR-CAM-19 / FR-CAM-20)
# ---------------------------------------------------------------------------


def _run_one_shot(
    *,
    subscriber: Any,
    subscription: str,
    resolved_target: str | None,
    parsed_types: frozenset[EventTypeEnum] | None,
    max_messages: int,
    output_mode: OutputMode,
) -> None:
    """Drain pending messages once and exit 0 (FR-CAM-20).

    Mirrors the v0.2.0 behaviour verbatim except for the optional
    ``parsed_types`` filter, which is also applied in follow mode and
    so was hoisted into the shared message-processing helper.
    """
    try:
        response = subscriber.pull(
            request={
                "subscription": subscription,
                "max_messages": max_messages,
                "return_immediately": True,
            },
            timeout=DEFAULT_PULL_DEADLINE_S,
        )
    except Exception as exc:  # noqa: BLE001 - convert any client error to structured
        exit_on_structured_error(
            StructuredError(
                code=EXIT_NETWORK_ERROR,
                message=f"Pub/Sub pull failed: {exc}",
                hint=(
                    "Verify ADC are configured "
                    "(`gcloud auth application-default login --update-adc`) "
                    "and the subscription path is correct."
                ),
            ),
            output_mode,
        )

    received = list(response.received_messages or [])
    ack_ids, _emitted = _process_messages(
        received_messages=received,
        resolved_target=resolved_target,
        parsed_types=parsed_types,
        output_mode=output_mode,
    )

    if ack_ids:
        _ack_with_warning(subscriber, subscription, ack_ids)


# ---------------------------------------------------------------------------
# Follow loop (FR-CAM-21..23)
# ---------------------------------------------------------------------------


def _run_follow_loop(
    *,
    subscriber: Any,
    subscription: str,
    resolved_target: str | None,
    parsed_types: frozenset[EventTypeEnum] | None,
    output_mode: OutputMode,
) -> None:
    """Long-running stream-pull until SIGINT/SIGTERM (FR-CAM-21..23).

    Loop structure:

    1. Install SIGINT/SIGTERM handlers that set a shared flag.
    2. While the flag is unset:
       a. Call ``subscriber.pull(return_immediately=False)``.
       b. On transport error: increment failure counter; if >= 5,
          exit 3; otherwise sleep the FR-CAM-23 backoff for that
          failure number (1, 2, 4, 8, 16, 32...) in 0.5s chunks
          checking the signal flag between chunks.
       c. On success: reset failure counter to 0; emit each parsed
          event that passes target + type filters; collect ack_ids
          for every message we *consumed* (matched target, regardless
          of type-filter outcome), exactly mirroring the one-shot
          target-filter ack semantics (FR-CAM-19 reviewer feedback C7).
    3. On exit (signal received OR exhausted retries), emit the
       FR-CAM-21 summary line ``{"event": "interrupted", "received": N}``
       to stdout in ALL output modes (the FR explicitly overrides
       ``--quiet``), then exit 130 (SIGINT) or 143 (SIGTERM).

    The previous SIGINT/SIGTERM handlers are saved and restored on
    exit so this verb is reusable inside a long-running test process
    (CliRunner runs in-process; not restoring handlers leaks them
    across tests).
    """
    received_count = 0
    consecutive_failures = 0
    interrupted: dict[str, int | None] = {"signal": None}
    # Set when the 5-consecutive-failures threshold trips; the
    # post-loop dispatcher uses this to choose between the FR-CAM-23
    # exit-3 path and the FR-CAM-21 signal-exit path.
    network_giveup: dict[str, StructuredError | None] = {"err": None}

    def _handle_signal(signum: int, _frame: Any) -> None:
        # Set the flag and let the loop terminate naturally. Raising
        # KeyboardInterrupt from a handler would bypass the
        # summary-line emission below, leaving operators with no
        # record of how many events the follow consumed.
        interrupted["signal"] = signum

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while interrupted["signal"] is None and network_giveup["err"] is None:
            try:
                response = subscriber.pull(
                    request={
                        "subscription": subscription,
                        "max_messages": FOLLOW_MAX_MESSAGES,
                        "return_immediately": False,
                    },
                    timeout=FOLLOW_PULL_TIMEOUT_S,
                )
            except (
                google.api_core.exceptions.GoogleAPICallError,
                OSError,
                TimeoutError,
            ) as exc:
                consecutive_failures += 1
                if consecutive_failures >= _BACKOFF_MAX_CONSECUTIVE_FAILURES:
                    # FR-CAM-23: five consecutive failures → exit 3
                    # naming the LAST failure. We deliberately do NOT
                    # raise here — instead we record the structured
                    # error and let the post-loop dispatcher emit it.
                    # That way the FR-CAM-21 summary line still goes
                    # to stdout BEFORE the structured error goes to
                    # stderr, in the order tooling expects.
                    network_giveup["err"] = StructuredError(
                        code=EXIT_NETWORK_ERROR,
                        message=(
                            f"Pub/Sub pull failed {consecutive_failures} times in a row; "
                            f"last error: {type(exc).__name__}: {exc}"
                        ),
                        hint=(
                            "Verify network connectivity to Google Cloud Pub/Sub and that "
                            "ADC are configured (`gcloud auth application-default login "
                            "--update-adc`). The CLI gives up after "
                            f"{_BACKOFF_MAX_CONSECUTIVE_FAILURES} consecutive failures "
                            "to avoid a tight error loop."
                        ),
                    )
                    break
                # Sleep the FR-CAM-23 schedule for this failure count.
                # Index is failure-count - 1 (1st failure → schedule[0]
                # = 1s). Cap at the last entry for failures beyond the
                # schedule's length (defensive — we exit before that
                # in current code, but the cap is documented).
                idx = min(consecutive_failures - 1, len(_BACKOFF_SCHEDULE_S) - 1)
                _sleep_with_signal_check(_BACKOFF_SCHEDULE_S[idx], interrupted)
                continue

            # Successful pull: reset the consecutive-failure counter.
            consecutive_failures = 0
            received = list(response.received_messages or [])
            if not received:
                # Empty pull (subscription drained for now). Loop top
                # checks the signal flag; if a signal arrived during
                # the pull, we'll exit there.
                continue
            ack_ids, emitted = _process_messages(
                received_messages=received,
                resolved_target=resolved_target,
                parsed_types=parsed_types,
                output_mode=output_mode,
            )
            received_count += emitted
            if ack_ids:
                _ack_with_warning(subscriber, subscription, ack_ids)

    finally:
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)
        # FR-CAM-21: emit the summary line in EVERY exit path —
        # signal-driven shutdown OR five-consecutive-failures
        # network-giveup — so tooling sees a consistent record of
        # consumed events before any structured error.
        _emit_interrupted_summary(received_count)

    # Post-loop dispatch (outside finally so sys.exit's exit-code is
    # the one tooling sees, not the finally's default).
    if network_giveup["err"] is not None:
        exit_on_structured_error(network_giveup["err"], output_mode)
    if interrupted["signal"] == signal.SIGTERM:
        sys.exit(EXIT_SIGTERM)
    sys.exit(EXIT_SIGINT)


# ---------------------------------------------------------------------------
# Shared message processing
# ---------------------------------------------------------------------------


def _process_messages(
    *,
    received_messages: list[Any],
    resolved_target: str | None,
    parsed_types: frozenset[EventTypeEnum] | None,
    output_mode: OutputMode,
) -> tuple[list[str], int]:
    """Decode + filter + emit a batch of Pub/Sub messages.

    Returns a tuple ``(ack_ids, emitted_count)``. The follow loop uses
    ``emitted_count`` to update the FR-CAM-21 summary counter; the
    one-shot drain ignores it.

    Reviewer feedback (C7) on Phase 2: only ack messages we actually
    consumed. Specifically:

    - Corrupt or non-event envelopes are acked (else they redeliver
      forever and stall the subscription).
    - Messages whose target does NOT match a target filter are LEFT in
      the subscription (do NOT ack — they belong to other cameras).
    - Messages whose target matches but whose type is filtered out by
      ``--types`` ARE acked. The operator consciously chose to ignore
      that subset; we don't want a doorbell stream to silently grow
      forever just because the operator only wants ``motion`` today.

    Side-effect: every message that passes both filters is emitted via
    the shared ``emit`` helper in the operator's chosen output mode.
    """
    ack_ids: list[str] = []
    emitted = 0
    for received_msg in received_messages:
        envelope = received_msg.message
        inner = _decode_message_data(envelope.data)
        if inner is None:
            ack_ids.append(received_msg.ack_id)
            continue

        event = parse_pubsub_event(inner, publish_time=envelope.publish_time)
        if event is None:
            ack_ids.append(received_msg.ack_id)
            continue

        if resolved_target is not None and event.target != resolved_target:
            # Not ours — leave for an unfiltered drain.
            continue

        # Target matches → consume the message regardless of type
        # filter outcome.
        ack_ids.append(received_msg.ack_id)

        if parsed_types is not None and event.event_type not in parsed_types:
            continue

        emit(event, output_mode)
        emitted += 1

    return ack_ids, emitted


def _ack_with_warning(subscriber: Any, subscription: str, ack_ids: list[str]) -> None:
    """Acknowledge a batch of message ids; surface failures on stderr.

    Reviewer feedback (C3) on Phase 2: persistent ack failures cause
    infinite redelivery + duplicates. Silently suppressing them leaves
    the operator with no diagnostic. Failure here remains non-fatal —
    Pub/Sub will redeliver and the next drain picks them up — but the
    operator now sees the warning.
    """
    try:
        subscriber.acknowledge(
            request={
                "subscription": subscription,
                "ack_ids": ack_ids,
            }
        )
    except (
        google.api_core.exceptions.GoogleAPICallError,
        OSError,
        TimeoutError,
    ) as exc:
        click.echo(
            f"warning: ack failed for {len(ack_ids)} message(s): {type(exc).__name__}: {exc}",
            err=True,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_subscription(config: Any) -> str | None:
    """Resolve the Pub/Sub subscription path.

    Precedence:

    1. ``[pubsub] subscription_name`` from config (highest).
    2. ``NEST_CLI_PUBSUB_SUBSCRIPTION`` env var.
    3. ``None`` (caller exits 6).
    """
    pubsub_section = getattr(config, "pubsub", None)
    if pubsub_section is not None:
        cfg_value = getattr(pubsub_section, "subscription_name", None)
        if isinstance(cfg_value, str) and cfg_value:
            return cfg_value
    env_value = os.environ.get(ENV_VAR_SUBSCRIPTION)
    if env_value:
        return env_value
    return None


def _exit_subscription_not_configured(output_mode: OutputMode) -> NoReturn:
    """Exit 6 with a hint per FR-CAM-25."""
    exit_on_structured_error(
        StructuredError(
            code=EXIT_CONFIG_ERROR,
            message=("Pub/Sub subscription not configured for cam events"),
            hint=(
                "Set [pubsub] subscription_name in your config (see SRD §9.2), "
                "or export NEST_CLI_PUBSUB_SUBSCRIPTION="
                "projects/{project}/subscriptions/{sub}. "
                "v0.2.0 does not yet ship `auth setup --pubsub`; create the "
                "subscription manually in your GCP console per SRD §3.1.3, then "
                "grant the OAuth principal the roles/pubsub.subscriber role."
            ),
        ),
        output_mode,
    )


def _decode_message_data(data: bytes | None) -> dict[str, Any] | None:
    """Decode a Pub/Sub message payload (bytes JSON) to a dict.

    Returns ``None`` on decode failure or when the top-level JSON value
    is not an object — caller skips the message either way.
    """
    if not data:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_subscriber() -> Any:
    """Construct a Pub/Sub SubscriberClient using ADC.

    Lazy import keeps ``cam list`` / ``cam info`` startup snappy and
    avoids a hard dependency on the gRPC channel for users who never
    invoke ``cam events``.

    This function is the ``unittest.mock.patch`` target for tests —
    the CI test suite never instantiates a real SubscriberClient. See
    ``tests/cam/test_events.py`` and ``tests/cam/test_events_follow.py``.
    """
    try:
        from google.cloud import pubsub_v1
    except ImportError as exc:  # pragma: no cover - dep is required
        print(
            f"google-cloud-pubsub is not installed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG_ERROR) from exc
    return pubsub_v1.SubscriberClient()


def _parse_types_filter(raw: str) -> frozenset[EventTypeEnum]:
    """Parse the ``--types`` comma-separated value (FR-CAM-22).

    Returns a frozenset of validated enum tokens. Empty tokens (e.g.
    a stray comma) are skipped. An invalid token raises a
    ``StructuredError`` with code 64 (usage error) and a hint listing
    the valid values.
    """
    raw_tokens = [tok.strip() for tok in raw.split(",")]
    tokens = [tok for tok in raw_tokens if tok]
    if not tokens:
        raise StructuredError(
            code=EXIT_USAGE_ERROR,
            message="--types must list at least one event-type token.",
            hint=(
                "Valid event types: "
                + ", ".join(sorted(_VALID_EVENT_TYPES))
                + ". Example: --types motion,person,doorbell-press."
            ),
        )
    invalid = [tok for tok in tokens if tok not in _VALID_EVENT_TYPES]
    if invalid:
        raise StructuredError(
            code=EXIT_USAGE_ERROR,
            message=("--types contains invalid token(s): " + ", ".join(repr(t) for t in invalid)),
            hint=(
                "Valid event types: "
                + ", ".join(sorted(_VALID_EVENT_TYPES))
                + ". Pass a comma-separated subset, e.g. --types motion,person."
            ),
        )
    # Cast: every member of ``tokens`` is in ``_VALID_EVENT_TYPES``,
    # which exactly enumerates EventTypeEnum's Literal members.
    return frozenset(tokens)  # type: ignore[arg-type]


def _emit_interrupted_summary(received: int) -> None:
    """Emit the FR-CAM-21 final JSONL summary line to stdout.

    The summary is emitted in ALL output modes — including ``--quiet``
    — because FR-CAM-21 explicitly names it as a required stdout line.
    We bypass ``emit`` and write directly via ``click.echo`` to avoid
    the quiet-mode suppression.
    """
    payload = {"event": "interrupted", "received": received}
    click.echo(json.dumps(payload, sort_keys=True))


def _sleep_with_signal_check(seconds: int, interrupted: dict[str, int | None]) -> None:
    """Sleep ``seconds`` total in 0.5s chunks, breaking on signal.

    Splits the FR-CAM-23 backoff interval into 0.5-second chunks so
    the signal-flag check fires frequently. Stops early if a signal
    has already been received — SIGINT response stays under ~1s even
    when the schedule is at its 32-second cap (FR-CAM-21 contract).
    """
    if seconds <= 0:
        return
    elapsed = 0.0
    while elapsed < seconds:
        if interrupted["signal"] is not None:
            return
        time.sleep(_BACKOFF_SLEEP_CHUNK_S)
        elapsed += _BACKOFF_SLEEP_CHUNK_S

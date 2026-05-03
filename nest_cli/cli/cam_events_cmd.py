"""``nest-cli cam events`` verb (one-shot drain mode).

Implements FR-CAM-19 / FR-CAM-20 / FR-CAM-24 / FR-CAM-25 (SRD §5.3.8).

Per SRD §3.1.3 / Decision 10, SDM does not expose events via REST
polling — events flow exclusively through the operator's Google
Cloud Pub/Sub subscription. The CLI's ``cam events`` verb pulls
from that subscription and emits each delivered message as JSONL on
stdout per §10.3.

This module ships **only the one-shot drain mode**. The
``--follow`` long-poll mode is Phase 2.1 (FR-CAM-21..23) and is
explicitly out of scope here.

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
"""

from __future__ import annotations

import os
import sys
from typing import Any

import click
import google.api_core.exceptions

from nest_cli.cli._shared import exit_on_structured_error
from nest_cli.config import default_config_path, load_config, resolve_alias
from nest_cli.errors import EXIT_CONFIG_ERROR, EXIT_NETWORK_ERROR, StructuredError
from nest_cli.output import OutputMode, add_output_options, emit
from nest_cli.sdm.event_types import parse_pubsub_event

# FR-CAM-20: default per-pull deadline 5 seconds. Documented in the
# SRD as the timeout the verb passes to Pub/Sub's pull. Pub/Sub will
# return early if the subscription drains; the deadline is the upper
# bound on a stale subscription's wait.
DEFAULT_PULL_DEADLINE_S = 5

# FR-CAM-20: default --max-messages = 100.
DEFAULT_MAX_MESSAGES = 100

# Env var for subscription override (FR-CAM-25 convenience path).
ENV_VAR_SUBSCRIPTION = "NEST_CLI_PUBSUB_SUBSCRIPTION"


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
@add_output_options
def cam_events(
    target: str | None,
    max_messages: int,
    output_mode: OutputMode,
) -> None:
    """Drain pending Pub/Sub events once and exit (FR-CAM-19 / FR-CAM-20).

    Without ``<target>``: emit every camera's events. With
    ``<target>``: filter to events whose SDM resource matches the
    alias-resolved target (FR-CAM-19).

    Each message is emitted as a single JSONL line per §10.3 Event.
    Acknowledged before the next message is processed so a partial
    interrupt does not redeliver already-emitted events.

    ``--follow`` is Phase 2.1 (FR-CAM-21..23) and not implemented in
    v0.2.0. The default behaviour is one-shot drain: pull pending
    messages once, ack them, exit 0.

    Failure paths:

    - Subscription not configured → exit 6 (FR-CAM-25).
    - Pub/Sub pull fails after retries → caught by the underlying
      client and surfaced as a structured error.
    """
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
    ack_ids: list[str] = []

    for received_msg in received:
        envelope = received_msg.message
        ack_ids.append(received_msg.ack_id)
        inner = _decode_message_data(envelope.data)
        if inner is None:
            # Skip non-JSON messages but still ack them — they're
            # corrupt or non-event messages we'd otherwise re-pull
            # forever.
            continue

        event = parse_pubsub_event(inner, publish_time=envelope.publish_time)
        if event is None:
            continue

        if resolved_target is not None and event.target != resolved_target:
            continue

        emit(event, output_mode)

    if ack_ids:
        # Reviewer feedback (C3): catch a narrower exception family and
        # surface failures on stderr. Persistent ack failures cause
        # infinite redelivery + duplicates; silently suppressing them
        # leaves the operator with no diagnostic. Failure here remains
        # non-fatal — Pub/Sub will redeliver and the next drain picks
        # them up — but the operator now sees the warning.
        try:
            subscriber.acknowledge(
                request={
                    "subscription": subscription,
                    "ack_ids": ack_ids,
                }
            )
        except (google.api_core.exceptions.GoogleAPICallError, OSError, TimeoutError) as exc:
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


def _exit_subscription_not_configured(output_mode: OutputMode) -> None:
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
        import json

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
    ``tests/cam/test_events.py``.
    """
    # Imported here so tests can patch this entire function without
    # touching the underlying google package.
    try:
        from google.cloud import pubsub_v1
    except ImportError as exc:  # pragma: no cover - dep is required
        print(
            f"google-cloud-pubsub is not installed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG_ERROR) from exc
    return pubsub_v1.SubscriberClient()

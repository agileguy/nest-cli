"""Tests for ``cam events --follow`` long-running event subscription.

Covers FR-CAM-21 (long-poll loop + clean SIGINT/SIGTERM exit), FR-CAM-22
(``--types`` filter), and FR-CAM-23 (capped exponential backoff with
five-consecutive-failures exit).

Pub/Sub is mocked at the ``SubscriberClient`` boundary via
``unittest.mock.patch``; CI never hits Google Pub/Sub. Sleeps during the
backoff schedule are intercepted via ``monkeypatch`` on
``nest_cli.cli.cam_events_cmd.time.sleep`` so tests don't actually wait.

Test layout mirrors ``tests/cam/test_events.py`` for the one-shot drain;
the helpers ``_make_received_message`` / ``_make_motion_event`` are
re-implemented here rather than imported because pytest treats sibling
test modules as opaque (no shared conftest fixture for these helpers
exists today).
"""

from __future__ import annotations

import json
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import google.api_core.exceptions
import pytest
from click.testing import CliRunner

from nest_cli.auth.types import CamCredentials
from nest_cli.cli import cli as cli_root


def _write_creds(path: Path) -> None:
    creds = CamCredentials(
        version=1,
        type="oauth",
        google_cloud_project_id="proj",
        oauth_client_id="client-id-12345678",
        oauth_client_secret="client-secret",  # noqa: S106
        refresh_token="refresh-tok",  # noqa: S106
        access_token="access-tok",  # noqa: S106
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(creds.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    config_path = tmp_path / "config.toml"
    credentials_path = tmp_path / "credentials-cam.json"
    monkeypatch.setattr("nest_cli.config.default_config_path", lambda: config_path)
    monkeypatch.setattr("nest_cli.cli.cam_events_cmd.default_config_path", lambda: config_path)
    monkeypatch.setattr("nest_cli.cli._shared.default_credentials_path", lambda: credentials_path)
    monkeypatch.delenv("NEST_CLI_PUBSUB_SUBSCRIPTION", raising=False)

    def _no_refresh(creds: CamCredentials, path: Path, *, force: bool = False) -> CamCredentials:
        return creds

    monkeypatch.setattr("nest_cli.cli._shared.refresh_access_token_if_needed", _no_refresh)

    _write_creds(credentials_path)
    return {"config": config_path, "credentials": credentials_path}


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch ``time.sleep`` inside the verb to a no-op that records durations.

    The follow-loop sleeps in 0.5s chunks during backoff (FR-CAM-21
    SIGINT-response window). Tests assert on the *total* time per backoff
    cycle by summing the recorded chunk durations between successive
    pulls — see ``test_backoff_schedule``.
    """
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("nest_cli.cli.cam_events_cmd.time.sleep", _sleep)
    return sleeps


@pytest.fixture
def write_subscription_config(fake_paths: dict[str, Path]) -> dict[str, Path]:
    """Write a baseline config.toml with a Pub/Sub subscription configured."""
    fake_paths["config"].write_text(
        '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n',
        encoding="utf-8",
    )
    return fake_paths


# ---------------------------------------------------------------------------
# Pub/Sub envelope helpers
# ---------------------------------------------------------------------------


def _make_received_message(
    *,
    ack_id: str,
    inner_payload: dict[str, Any],
    publish_time: datetime,
) -> MagicMock:
    inner = MagicMock()
    inner.data = json.dumps(inner_payload).encode("utf-8")
    inner.publish_time = publish_time
    inner.attributes = {}
    received = MagicMock()
    received.ack_id = ack_id
    received.message = inner
    return received


_SDM_TYPE_BY_ENUM: dict[str, str] = {
    "motion": "sdm.devices.events.CameraMotion.Motion",
    "person": "sdm.devices.events.CameraPerson.Person",
    "sound": "sdm.devices.events.CameraSound.Sound",
    "doorbell-press": "sdm.devices.events.DoorbellChime.Chime",
}


def _make_event(
    *,
    target_id: str,
    event_type: str = "motion",
    event_id: str = "abc-123",
    publish_time: datetime | None = None,
) -> dict[str, Any]:
    """Build an SDM Pub/Sub inner payload for a given event_type enum value.

    Tests pass the SRD §10.3 enum (``motion`` / ``person`` / ...) and we
    look up the SDM event-type id. ``unknown`` uses an unmapped SDM id so
    the parser falls through to the ``unknown`` branch.
    """
    pt = publish_time or datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
    if event_type == "unknown":
        sdm_id = "sdm.devices.events.CameraClipPreview.ClipPreview"
    else:
        sdm_id = _SDM_TYPE_BY_ENUM[event_type]
    return {
        "eventId": "wrap-1",
        "timestamp": pt.isoformat().replace("+00:00", "Z"),
        "resourceUpdate": {
            "name": target_id,
            "events": {
                sdm_id: {
                    "eventSessionId": "session-xyz",
                    "eventId": event_id,
                }
            },
        },
        "userId": "user-abc",
        "resourceGroup": [target_id],
    }


def _build_pull_response(messages: list[MagicMock]) -> MagicMock:
    response = MagicMock()
    response.received_messages = messages
    return response


def _stop_after_n_pulls(stop_after: int, sentinel_response: MagicMock) -> Any:
    """Build a side_effect that stops the loop by raising SystemExit-via-signal.

    After ``stop_after`` successful pulls, set the interrupted flag (via
    a SIGINT side-effect on the next pull) so the test exits cleanly.
    Returns a closure suitable for ``subscriber.pull.side_effect``.
    """
    counter = {"n": 0}

    def _side_effect(*args: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        if counter["n"] > stop_after:
            # Send SIGINT to ourselves to break the loop on the next
            # iteration. The handler is in-process (CliRunner runs in
            # the same process), so this triggers the verb's installed
            # handler.
            os.kill(os.getpid(), signal.SIGINT)
            return _build_pull_response([])
        return sentinel_response

    return _side_effect


# ---------------------------------------------------------------------------
# Happy-path: three-message stream, signal stops loop, summary emitted
# ---------------------------------------------------------------------------


class TestFollowHappyPath:
    def test_three_messages_emitted_and_acked(
        self, write_subscription_config: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """One pull returns 3 messages → all 3 emitted + acked, then SIGINT.

        Final JSONL summary line says ``received: 3`` and exit code is 130.
        """
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target = "enterprises/proj/devices/aaa"

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            response = _build_pull_response(
                [
                    _make_received_message(
                        ack_id=f"ack-{i}",
                        inner_payload=_make_event(
                            target_id=target, event_id=f"e-{i}", publish_time=msg_time
                        ),
                        publish_time=msg_time,
                    )
                    for i in range(3)
                ]
            )
            subscriber.pull.side_effect = _stop_after_n_pulls(1, response)
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--follow", "--jsonl"])

            assert result.exit_code == 130, result.output + result.stderr
            stdout_lines = [ln for ln in result.output.splitlines() if ln.strip()]
            # 3 events + 1 summary line.
            assert len(stdout_lines) == 4
            summary = json.loads(stdout_lines[-1])
            assert summary == {"event": "interrupted", "received": 3}
            # All three ack ids passed.
            ack_calls = subscriber.acknowledge.call_args_list
            assert len(ack_calls) == 1
            args, kwargs = ack_calls[0]
            req = kwargs.get("request") or (args[0] if args else None)
            assert set(req["ack_ids"]) == {"ack-0", "ack-1", "ack-2"}


# ---------------------------------------------------------------------------
# SIGINT and SIGTERM — different exit codes, same summary line
# ---------------------------------------------------------------------------


class TestSigintMidPull:
    def test_sigint_exit_130_summary_received_two(
        self, write_subscription_config: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """Pull returns 2 messages, then SIGINT before next pull.

        Expected: 2 emitted JSONL lines, then summary ``received: 2``,
        exit code 130.
        """
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target = "enterprises/proj/devices/aaa"
        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            response = _build_pull_response(
                [
                    _make_received_message(
                        ack_id="a",
                        inner_payload=_make_event(target_id=target, publish_time=msg_time),
                        publish_time=msg_time,
                    ),
                    _make_received_message(
                        ack_id="b",
                        inner_payload=_make_event(
                            target_id=target, event_id="e-b", publish_time=msg_time
                        ),
                        publish_time=msg_time,
                    ),
                ]
            )
            subscriber.pull.side_effect = _stop_after_n_pulls(1, response)
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--follow", "--jsonl"])
            assert result.exit_code == 130
            stdout_lines = [ln for ln in result.output.splitlines() if ln.strip()]
            summary = json.loads(stdout_lines[-1])
            assert summary == {"event": "interrupted", "received": 2}


class TestSigtermAnalog:
    def test_sigterm_exit_143_summary_received_two(
        self, write_subscription_config: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """Pull returns 2 messages, then SIGTERM. Exit 143."""
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target = "enterprises/proj/devices/aaa"

        # Custom side-effect: raise SIGTERM not SIGINT.
        counter = {"n": 0}
        sentinel = _build_pull_response(
            [
                _make_received_message(
                    ack_id="a",
                    inner_payload=_make_event(target_id=target, publish_time=msg_time),
                    publish_time=msg_time,
                ),
                _make_received_message(
                    ack_id="b",
                    inner_payload=_make_event(
                        target_id=target, event_id="e-b", publish_time=msg_time
                    ),
                    publish_time=msg_time,
                ),
            ]
        )

        def _side_effect(*args: Any, **kwargs: Any) -> Any:
            counter["n"] += 1
            if counter["n"] > 1:
                os.kill(os.getpid(), signal.SIGTERM)
                return _build_pull_response([])
            return sentinel

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            subscriber.pull.side_effect = _side_effect
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--follow", "--jsonl"])
            assert result.exit_code == 143
            stdout_lines = [ln for ln in result.output.splitlines() if ln.strip()]
            summary = json.loads(stdout_lines[-1])
            assert summary == {"event": "interrupted", "received": 2}


# ---------------------------------------------------------------------------
# --types filter (FR-CAM-22)
# ---------------------------------------------------------------------------


class TestTypesFilter:
    def test_types_motion_person_drops_other_types(
        self, write_subscription_config: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """Stream of 5 mixed-type events; ``--types motion,person`` keeps 3."""
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target = "enterprises/proj/devices/aaa"
        types_in_order = ["motion", "person", "sound", "doorbell-press", "motion"]
        msgs = [
            _make_received_message(
                ack_id=f"ack-{i}",
                inner_payload=_make_event(
                    target_id=target,
                    event_type=t,
                    event_id=f"e-{i}",
                    publish_time=msg_time,
                ),
                publish_time=msg_time,
            )
            for i, t in enumerate(types_in_order)
        ]
        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            response = _build_pull_response(msgs)
            subscriber.pull.side_effect = _stop_after_n_pulls(1, response)
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(
                cli_root,
                ["cam", "events", "--follow", "--jsonl", "--types", "motion,person"],
            )
            assert result.exit_code == 130, result.output + result.stderr
            stdout_lines = [ln for ln in result.output.splitlines() if ln.strip()]
            # 3 emitted (motion, person, motion) + 1 summary line.
            assert len(stdout_lines) == 4
            emitted_types = [json.loads(ln)["event_type"] for ln in stdout_lines[:-1]]
            assert emitted_types == ["motion", "person", "motion"]
            summary = json.loads(stdout_lines[-1])
            assert summary["received"] == 3
            # All five must still be acked — we consumed them from the
            # subscription, just chose not to emit non-matching types.
            ack_calls = subscriber.acknowledge.call_args_list
            assert len(ack_calls) == 1
            args, kwargs = ack_calls[0]
            req = kwargs.get("request") or (args[0] if args else None)
            assert set(req["ack_ids"]) == {f"ack-{i}" for i in range(5)}


class TestTypesInvalidExits64:
    def test_invalid_type_token_exits_usage_error(
        self, write_subscription_config: dict[str, Path]
    ) -> None:
        """``--types bogus`` exits 64 with hint listing valid values."""
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["cam", "events", "--follow", "--json", "--types", "motion,bogus"],
        )
        assert result.exit_code == 64, result.output + result.stderr
        envelope = json.loads(result.stderr)
        assert envelope["exit_code"] == 64
        assert envelope["error"] == "usage_error"
        # Hint lists valid event types.
        hint = envelope.get("hint", "")
        for valid in ("motion", "person", "package", "sound", "doorbell-press", "unknown"):
            assert valid in hint


# ---------------------------------------------------------------------------
# Backoff schedule (FR-CAM-23)
# ---------------------------------------------------------------------------


class TestBackoffSchedule:
    def test_backoff_durations_progress_1_2_4_8(
        self, write_subscription_config: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """Pull raises 4 transport errors then succeeds.

        After the 4 failures, the captured sleep durations should sum to
        ``1 + 2 + 4 + 8 = 15`` seconds (sleeping in 0.5s chunks ⇒ 30
        chunks). Every chunk is 0.5s; we assert the sum, not the chunking.
        """
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target = "enterprises/proj/devices/aaa"
        success_response = _build_pull_response(
            [
                _make_received_message(
                    ack_id="a-1",
                    inner_payload=_make_event(target_id=target, publish_time=msg_time),
                    publish_time=msg_time,
                ),
            ]
        )

        # Build the side_effect: fail 4 times, succeed once, then SIGINT
        # so the loop terminates.
        counter = {"n": 0}
        err = google.api_core.exceptions.ServiceUnavailable("transient")

        def _side_effect(*args: Any, **kwargs: Any) -> Any:
            counter["n"] += 1
            if counter["n"] <= 4:
                raise err
            if counter["n"] == 5:
                return success_response
            os.kill(os.getpid(), signal.SIGINT)
            return _build_pull_response([])

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            subscriber.pull.side_effect = _side_effect
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--follow", "--jsonl"])
            assert result.exit_code == 130, result.output + result.stderr
            # Sum recorded sleeps. Each backoff slept 0.5s chunks; total
            # across the 4 failed cycles = 1+2+4+8 = 15s (modulo float
            # tolerance from chunking).
            total = sum(fast_sleep)
            assert abs(total - 15.0) < 1e-6, f"expected ~15s of backoff, got {total}"


class TestFiveConsecutiveFailuresExit3:
    def test_fifth_consecutive_failure_exits_3_with_last_error(
        self, write_subscription_config: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """Five consecutive transport errors → exit 3 (network), structured.

        FR-CAM-23: "Five consecutive failures exit 3" → fail on the
        fifth, not the sixth. The structured-error message must name the
        last failure's exception type.
        """
        last_msg = "final transport blowup"

        def _side_effect(*args: Any, **kwargs: Any) -> Any:
            # The 5th call raises a distinguishable error so we can verify
            # it's the one named in the exit envelope.
            if _side_effect.calls < 4:
                _side_effect.calls += 1
                raise google.api_core.exceptions.ServiceUnavailable("earlier transient")
            _side_effect.calls += 1
            raise google.api_core.exceptions.DeadlineExceeded(last_msg)

        _side_effect.calls = 0  # type: ignore[attr-defined]

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            subscriber.pull.side_effect = _side_effect
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--follow", "--json"])
            assert result.exit_code == 3, result.output + result.stderr
            envelope = json.loads(result.stderr)
            assert envelope["exit_code"] == 3
            assert envelope["error"] == "network_error"
            # Structured error names the last failure.
            assert "DeadlineExceeded" in envelope["message"] or last_msg in envelope["message"]


class TestSuccessResetsCounter:
    def test_three_failures_then_success_then_four_failures_does_not_exit(
        self, write_subscription_config: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """3 failures → success → 4 failures should not exit.

        The successful pull must reset the consecutive-failure counter.
        After 4 more failures (total 7 failures, but counter is 4), we
        need to terminate the loop somehow — send SIGINT after 4 post-
        success failures.
        """
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target = "enterprises/proj/devices/aaa"
        success = _build_pull_response(
            [
                _make_received_message(
                    ack_id="ok",
                    inner_payload=_make_event(target_id=target, publish_time=msg_time),
                    publish_time=msg_time,
                )
            ]
        )

        err = google.api_core.exceptions.ServiceUnavailable("oops")

        # Sequence: fail, fail, fail, succeed, fail, fail, fail, fail, [signal]
        events = ["fail", "fail", "fail", "succ", "fail", "fail", "fail", "fail", "stop"]
        counter = {"n": 0}

        def _side_effect(*args: Any, **kwargs: Any) -> Any:
            i = counter["n"]
            counter["n"] += 1
            ev = events[i] if i < len(events) else "stop"
            if ev == "fail":
                raise err
            if ev == "succ":
                return success
            # stop
            os.kill(os.getpid(), signal.SIGINT)
            return _build_pull_response([])

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            subscriber.pull.side_effect = _side_effect
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--follow", "--jsonl"])
            # Must exit on signal (130), NOT on 5-consecutive-failures (3).
            assert result.exit_code == 130, (
                f"expected 130 (signal exit) — counter must reset on success; "
                f"got {result.exit_code}: {result.output + result.stderr}"
            )
            stdout_lines = [ln for ln in result.output.splitlines() if ln.strip()]
            summary = json.loads(stdout_lines[-1])
            assert summary == {"event": "interrupted", "received": 1}


# ---------------------------------------------------------------------------
# Type filter combined with target filter
# ---------------------------------------------------------------------------


class TestTypeAndTargetCombined:
    def test_target_and_type_filter_emit_one_ack_one(
        self, fake_paths: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """Stream contains 4 messages; only 1 matches both target + type.

        Only that one is emitted. Per the existing target-filter contract
        (test_events.py::TestTargetFilterDoesNotAckOtherCameras /
        FR-CAM-19 + reviewer feedback C7), events filtered out by
        *target* are NOT acked — they belong to other cameras and a
        subsequent unfiltered drain should pick them up. Events filtered
        out by *type* (but matching the target) ARE acked — the operator
        consciously asked for a subset of types.
        """
        fake_paths["config"].write_text(
            '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n'
            '[aliases]\nfront-door = "enterprises/proj/devices/front-id"\n',
            encoding="utf-8",
        )
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        front = "enterprises/proj/devices/front-id"
        back = "enterprises/proj/devices/back-id"

        msgs = [
            # match target, match type → emit + ack
            _make_received_message(
                ack_id="front-motion",
                inner_payload=_make_event(
                    target_id=front, event_type="motion", publish_time=msg_time
                ),
                publish_time=msg_time,
            ),
            # match target, miss type → ack but don't emit
            _make_received_message(
                ack_id="front-sound",
                inner_payload=_make_event(
                    target_id=front, event_type="sound", event_id="e2", publish_time=msg_time
                ),
                publish_time=msg_time,
            ),
            # miss target → don't emit, don't ack
            _make_received_message(
                ack_id="back-motion",
                inner_payload=_make_event(
                    target_id=back, event_type="motion", event_id="e3", publish_time=msg_time
                ),
                publish_time=msg_time,
            ),
            # miss target → don't emit, don't ack
            _make_received_message(
                ack_id="back-person",
                inner_payload=_make_event(
                    target_id=back, event_type="person", event_id="e4", publish_time=msg_time
                ),
                publish_time=msg_time,
            ),
        ]

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            response = _build_pull_response(msgs)
            subscriber.pull.side_effect = _stop_after_n_pulls(1, response)
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(
                cli_root,
                [
                    "cam",
                    "events",
                    "front-door",
                    "--follow",
                    "--jsonl",
                    "--types",
                    "motion",
                ],
            )
            assert result.exit_code == 130
            stdout_lines = [ln for ln in result.output.splitlines() if ln.strip()]
            assert len(stdout_lines) == 2  # 1 event + summary
            payload = json.loads(stdout_lines[0])
            assert payload["target"] == front
            assert payload["event_type"] == "motion"
            summary = json.loads(stdout_lines[-1])
            assert summary["received"] == 1
            # Acks: front-motion (matched), front-sound (target-matched
            # but type-filtered). Back-* not acked.
            ack_calls = subscriber.acknowledge.call_args_list
            assert len(ack_calls) == 1
            args, kwargs = ack_calls[0]
            req = kwargs.get("request") or (args[0] if args else None)
            assert set(req["ack_ids"]) == {"front-motion", "front-sound"}


# ---------------------------------------------------------------------------
# Final summary line emitted in --quiet mode (FR-CAM-21 override)
# ---------------------------------------------------------------------------


class TestSummaryLineOverridesQuiet:
    def test_summary_line_emitted_under_quiet(
        self, write_subscription_config: dict[str, Path], fast_sleep: list[float]
    ) -> None:
        """``--quiet`` suppresses event records but NOT the interrupted summary.

        FR-CAM-21 explicitly names the summary as a required JSONL line
        on stdout, regardless of output mode. Document the override
        contract via this test.
        """
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target = "enterprises/proj/devices/aaa"
        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            response = _build_pull_response(
                [
                    _make_received_message(
                        ack_id="a",
                        inner_payload=_make_event(target_id=target, publish_time=msg_time),
                        publish_time=msg_time,
                    ),
                ]
            )
            subscriber.pull.side_effect = _stop_after_n_pulls(1, response)
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--follow", "--quiet"])
            assert result.exit_code == 130
            stdout_lines = [ln for ln in result.output.splitlines() if ln.strip()]
            # Only the summary line — no event JSONL.
            assert len(stdout_lines) == 1
            summary = json.loads(stdout_lines[0])
            assert summary == {"event": "interrupted", "received": 1}

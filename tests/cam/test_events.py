"""Tests for ``nest_cli.cli.cam_events_cmd`` — ``cam events`` verb.

Covers FR-CAM-19, FR-CAM-20, FR-CAM-24, FR-CAM-25 (one-shot drain
mode). ``--follow`` is Phase 2.1 and explicitly out of scope.

Pub/Sub mocked at the ``SubscriberClient`` boundary via
``unittest.mock.patch``; CI never hits Google Pub/Sub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# Pub/Sub envelope helpers
# ---------------------------------------------------------------------------


def _make_received_message(
    *,
    ack_id: str,
    inner_payload: dict[str, Any],
    publish_time: datetime,
) -> MagicMock:
    """Build a fake Pub/Sub ``ReceivedMessage`` matching the SDK shape.

    The Google client returns ``PullResponse.received_messages`` where
    each item has ``ack_id`` and ``message``. ``message.data`` is bytes
    (JSON encoded by the publisher); ``message.publish_time`` is a
    datetime. We build a MagicMock that satisfies the attributes the
    verb uses.
    """
    inner = MagicMock()
    inner.data = json.dumps(inner_payload).encode("utf-8")
    inner.publish_time = publish_time
    inner.attributes = {}

    received = MagicMock()
    received.ack_id = ack_id
    received.message = inner
    return received


def _make_motion_event(
    *,
    target_id: str,
    event_id: str = "abc-123",
    publish_time: datetime | None = None,
) -> dict[str, Any]:
    """Build the SDM-style inner payload for a motion event.

    Matches the shape Google's Pub/Sub topic publishes: a top-level
    ``timestamp``, a ``resourceUpdate`` with ``name`` (full SDM path)
    and ``events`` (dict keyed by event-type id).
    """
    pt = publish_time or datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
    return {
        "eventId": "wrap-1",
        "timestamp": pt.isoformat().replace("+00:00", "Z"),
        "resourceUpdate": {
            "name": target_id,
            "events": {
                "sdm.devices.events.CameraMotion.Motion": {
                    "eventSessionId": "session-xyz",
                    "eventId": event_id,
                }
            },
        },
        "userId": "user-abc",
        "resourceGroup": [target_id],
    }


# ---------------------------------------------------------------------------
# Subscription source resolution
# ---------------------------------------------------------------------------


class TestSubscriptionMissing:
    def test_no_config_no_env_exits_6(self, fake_paths: dict[str, Path]) -> None:
        # FR-CAM-25: missing subscription → exit 6 with hint.
        runner = CliRunner()
        result = runner.invoke(cli_root, ["cam", "events", "--json"])
        assert result.exit_code == 6
        # Hint should mention the manual setup path.
        combined = result.stderr + result.output
        assert "subscription" in combined.lower()


class TestSubscriptionFromConfig:
    def test_reads_subscription_from_config(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n',
            encoding="utf-8",
        )
        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            pull_response = MagicMock()
            pull_response.received_messages = []
            subscriber.pull.return_value = pull_response
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--jsonl"])
            assert result.exit_code == 0, result.output + result.stderr
            # Subscriber.pull called with the configured subscription.
            args, kwargs = subscriber.pull.call_args
            req = kwargs.get("request") or (args[0] if args else None)
            assert req is not None
            assert req["subscription"] == "projects/proj/subscriptions/sdm-events"


class TestSubscriptionFromEnv:
    def test_falls_back_to_env_var(
        self, fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEST_CLI_PUBSUB_SUBSCRIPTION", "projects/proj/subscriptions/from-env")
        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            pull_response = MagicMock()
            pull_response.received_messages = []
            subscriber.pull.return_value = pull_response
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--jsonl"])
            assert result.exit_code == 0
            args, kwargs = subscriber.pull.call_args
            req = kwargs.get("request") or (args[0] if args else None)
            assert req["subscription"] == "projects/proj/subscriptions/from-env"


# ---------------------------------------------------------------------------
# Drain happy path + record shape (FR-CAM-24)
# ---------------------------------------------------------------------------


class TestDrainEmitsEventRecords:
    def test_jsonl_per_message_with_srd_shape(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n'
            '[aliases]\nfront-door = "enterprises/proj/devices/doorbell-1"\n',
            encoding="utf-8",
        )
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target_id = "enterprises/proj/devices/doorbell-1"

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            pull_response = MagicMock()
            pull_response.received_messages = [
                _make_received_message(
                    ack_id="ack-1",
                    inner_payload=_make_motion_event(target_id=target_id, publish_time=msg_time),
                    publish_time=msg_time,
                ),
            ]
            subscriber.pull.return_value = pull_response
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--jsonl"])
            assert result.exit_code == 0, result.output + result.stderr
            lines = [ln for ln in result.output.splitlines() if ln.strip()]
            assert len(lines) == 1
            payload = json.loads(lines[0])
            # SRD §10.3 shape.
            assert payload["target"] == target_id
            assert payload["event_type"] == "motion"
            assert payload["has_image"] is True
            assert payload["image_eligibility_window_s"] >= 0
            assert payload["source"] == "pubsub"
            # ts is RFC 3339 UTC Z.
            assert payload["ts"].endswith("Z")
            # ack called with our ack-id.
            ack_calls = subscriber.acknowledge.call_args_list
            assert len(ack_calls) >= 1
            args, kwargs = ack_calls[0]
            req = kwargs.get("request") or (args[0] if args else None)
            assert req is not None
            assert "ack-1" in req["ack_ids"]


class TestNoTargetEmitsAll:
    def test_no_target_emits_every_message(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n',
            encoding="utf-8",
        )
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target_a = "enterprises/proj/devices/aaa"
        target_b = "enterprises/proj/devices/bbb"

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            pull_response = MagicMock()
            pull_response.received_messages = [
                _make_received_message(
                    ack_id="a-1",
                    inner_payload=_make_motion_event(target_id=target_a, publish_time=msg_time),
                    publish_time=msg_time,
                ),
                _make_received_message(
                    ack_id="b-1",
                    inner_payload=_make_motion_event(target_id=target_b, publish_time=msg_time),
                    publish_time=msg_time,
                ),
            ]
            subscriber.pull.return_value = pull_response
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--jsonl"])
            assert result.exit_code == 0
            lines = [ln for ln in result.output.splitlines() if ln.strip()]
            assert len(lines) == 2


class TestTargetFilters:
    def test_target_filters_to_one_camera(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n'
            '[aliases]\nfront-door = "enterprises/proj/devices/aaa"\n',
            encoding="utf-8",
        )
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        target_a = "enterprises/proj/devices/aaa"
        target_b = "enterprises/proj/devices/bbb"

        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            pull_response = MagicMock()
            pull_response.received_messages = [
                _make_received_message(
                    ack_id="a-1",
                    inner_payload=_make_motion_event(target_id=target_a, publish_time=msg_time),
                    publish_time=msg_time,
                ),
                _make_received_message(
                    ack_id="b-1",
                    inner_payload=_make_motion_event(target_id=target_b, publish_time=msg_time),
                    publish_time=msg_time,
                ),
            ]
            subscriber.pull.return_value = pull_response
            mk.return_value = subscriber
            runner = CliRunner()
            # Use the alias front-door which resolves to target_a.
            result = runner.invoke(cli_root, ["cam", "events", "front-door", "--jsonl"])
            assert result.exit_code == 0
            lines = [ln for ln in result.output.splitlines() if ln.strip()]
            assert len(lines) == 1
            payload = json.loads(lines[0])
            assert payload["target"] == target_a


class TestMaxMessages:
    def test_max_messages_overrides_default(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n',
            encoding="utf-8",
        )
        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            pull_response = MagicMock()
            pull_response.received_messages = []
            subscriber.pull.return_value = pull_response
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--jsonl", "--max-messages", "7"])
            assert result.exit_code == 0
            args, kwargs = subscriber.pull.call_args
            req = kwargs.get("request") or (args[0] if args else None)
            assert req["max_messages"] == 7


class TestPullFailureExitCode:
    def test_pull_failure_exits_3_via_constant(self, fake_paths: dict[str, Path]) -> None:
        """Reviewer feedback (C1): exit 3 must come from EXIT_NETWORK_ERROR constant.

        Regression: prior to the fix the verb hard-coded ``code=3``; this
        test asserts the exit code is 3 (the constant's value) AND that
        the JSON envelope's ``error`` enum is the SRD-mapped
        ``network_error`` for that code, which only happens if the code
        is wired through ``error_enum_for_code``.
        """
        fake_paths["config"].write_text(
            '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n',
            encoding="utf-8",
        )
        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            subscriber.pull.side_effect = RuntimeError("pubsub broke")
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--json"])
            assert result.exit_code == 3
            envelope = json.loads(result.stderr)
            assert envelope["exit_code"] == 3
            assert envelope["error"] == "network_error"


class TestHasImageDetection:
    def test_no_event_id_means_has_image_false(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[pubsub]\nsubscription_name = "projects/proj/subscriptions/sdm-events"\n',
            encoding="utf-8",
        )
        target_id = "enterprises/proj/devices/aaa"
        msg_time = datetime(2026, 5, 3, 0, 30, 0, tzinfo=UTC)
        # Build a payload WITHOUT eventId in the events dict.
        no_image = {
            "eventId": "wrap-2",
            "timestamp": msg_time.isoformat().replace("+00:00", "Z"),
            "resourceUpdate": {
                "name": target_id,
                "events": {
                    "sdm.devices.events.CameraMotion.Motion": {
                        "eventSessionId": "session-no-image"
                        # No 'eventId' key.
                    }
                },
            },
        }
        with patch("nest_cli.cli.cam_events_cmd._build_subscriber") as mk:
            subscriber = MagicMock()
            pull_response = MagicMock()
            pull_response.received_messages = [
                _make_received_message(
                    ack_id="x-1",
                    inner_payload=no_image,
                    publish_time=msg_time,
                )
            ]
            subscriber.pull.return_value = pull_response
            mk.return_value = subscriber
            runner = CliRunner()
            result = runner.invoke(cli_root, ["cam", "events", "--jsonl"])
            assert result.exit_code == 0
            lines = [ln for ln in result.output.splitlines() if ln.strip()]
            payload = json.loads(lines[0])
            assert payload["has_image"] is False
            assert payload["image_eligibility_window_s"] == 0

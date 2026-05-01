"""Tests for ``nest_cli.errors`` — exit codes and StructuredError contract.

Covers SRD §11.1 (exit-code constants) and §11.2 (stderr envelope).
"""

from __future__ import annotations

import json

import pytest

from nest_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_UNSUPPORTED_FEATURE,
    EXIT_USAGE_ERROR,
    StructuredError,
    emit_structured_error_to_stderr,
    error_enum_for_code,
)


class TestExitCodeConstants:
    """SRD §11.1 — exit codes are stable integers other modules import."""

    def test_exit_codes_match_srd_table(self) -> None:
        assert EXIT_OK == 0
        assert EXIT_DEVICE_ERROR == 1
        assert EXIT_AUTH_ERROR == 2
        assert EXIT_NETWORK_ERROR == 3
        assert EXIT_NOT_FOUND == 4
        assert EXIT_UNSUPPORTED_FEATURE == 5
        assert EXIT_CONFIG_ERROR == 6
        assert EXIT_PARTIAL_FAILURE == 7
        assert EXIT_USAGE_ERROR == 64
        assert EXIT_SIGINT == 130
        assert EXIT_SIGTERM == 143


class TestErrorEnumForCode:
    """SRD §11.2 — closed enum mapping exit code → wire-format string."""

    @pytest.mark.parametrize(
        "code, expected",
        [
            (EXIT_DEVICE_ERROR, "device_error"),
            (EXIT_AUTH_ERROR, "auth_failed"),
            (EXIT_NETWORK_ERROR, "network_error"),
            (EXIT_NOT_FOUND, "not_found"),
            (EXIT_UNSUPPORTED_FEATURE, "unsupported_feature"),
            (EXIT_CONFIG_ERROR, "config_error"),
            (EXIT_PARTIAL_FAILURE, "partial_failure"),
            (EXIT_USAGE_ERROR, "usage_error"),
            (EXIT_SIGINT, "interrupted"),
            (EXIT_SIGTERM, "interrupted"),
        ],
    )
    def test_known_code_returns_enum(self, code: int, expected: str) -> None:
        assert error_enum_for_code(code) == expected

    def test_unknown_code_falls_back_to_device_error(self) -> None:
        # Defensive: should never be reached if callers use EXIT_* constants.
        assert error_enum_for_code(999) == "device_error"


class TestStructuredError:
    """The dataclass is a proper Python Exception with structured fields."""

    def test_constructs_with_required_fields(self) -> None:
        err = StructuredError(code=EXIT_AUTH_ERROR, message="missing creds")
        assert err.code == EXIT_AUTH_ERROR
        assert err.message == "missing creds"
        assert err.hint is None
        assert err.details is None

    def test_str_returns_message(self) -> None:
        err = StructuredError(code=EXIT_NOT_FOUND, message="alias not found")
        assert str(err) == "alias not found"

    def test_can_carry_optional_hint_and_details(self) -> None:
        err = StructuredError(
            code=EXIT_AUTH_ERROR,
            message="creds rejected",
            hint="run setup",
            details={"target": "front-door"},
        )
        assert err.hint == "run setup"
        assert err.details == {"target": "front-door"}

    def test_is_a_real_exception(self) -> None:
        with pytest.raises(StructuredError) as exc_info:
            raise StructuredError(code=EXIT_DEVICE_ERROR, message="boom")
        assert exc_info.value.code == EXIT_DEVICE_ERROR


class TestEmitStructuredErrorToStderr:
    """Stderr envelope: JSON in machine modes, line in text mode."""

    def test_json_mode_emits_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = StructuredError(
            code=EXIT_AUTH_ERROR,
            message="creds rejected",
            hint="run setup",
        )
        emit_structured_error_to_stderr(err, output_mode="json")
        captured = capsys.readouterr()
        assert captured.out == ""
        payload = json.loads(captured.err)
        assert payload == {
            "error": "auth_failed",
            "exit_code": EXIT_AUTH_ERROR,
            "message": "creds rejected",
            "hint": "run setup",
        }

    def test_jsonl_mode_emits_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = StructuredError(
            code=EXIT_NOT_FOUND,
            message="alias unknown",
        )
        emit_structured_error_to_stderr(err, output_mode="jsonl")
        captured = capsys.readouterr()
        payload = json.loads(captured.err)
        assert payload["error"] == "not_found"
        assert payload["exit_code"] == EXIT_NOT_FOUND
        assert payload["message"] == "alias unknown"
        assert "hint" not in payload

    def test_quiet_mode_still_emits_envelope_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # FR-14: --quiet only suppresses stdout; structured errors still
        # land on stderr (otherwise the operator sees nothing on failure).
        err = StructuredError(code=EXIT_DEVICE_ERROR, message="device down")
        emit_structured_error_to_stderr(err, output_mode="quiet")
        captured = capsys.readouterr()
        assert captured.out == ""
        payload = json.loads(captured.err)
        assert payload["error"] == "device_error"

    def test_text_mode_emits_human_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = StructuredError(
            code=EXIT_CONFIG_ERROR,
            message="invalid TOML",
            hint="fix the syntax",
        )
        emit_structured_error_to_stderr(err, output_mode="text")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "error: invalid TOML" in captured.err
        assert "hint: fix the syntax" in captured.err

    def test_json_mode_includes_details_when_present(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = StructuredError(
            code=EXIT_AUTH_ERROR,
            message="rejected",
            details={"credential": "oauth_refresh_token"},
        )
        emit_structured_error_to_stderr(err, output_mode="json")
        captured = capsys.readouterr()
        payload = json.loads(captured.err)
        assert payload["details"] == {"credential": "oauth_refresh_token"}

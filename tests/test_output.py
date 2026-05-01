"""Tests for ``nest_cli.output`` — output-mode formatters and decorator.

Covers FR-11..15 (SRD §5.8) and the mutually-exclusive flag rejection.
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner
from pydantic import BaseModel

from nest_cli.output import OutputMode, add_output_options, emit


class _SampleModel(BaseModel):
    """Trivial Pydantic model used to exercise the model-aware path."""

    name: str
    count: int


# ---------------------------------------------------------------------------
# emit() — per-mode behavior
# ---------------------------------------------------------------------------


class TestEmitJsonMode:
    def test_emits_pretty_json_for_dict(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit({"a": 1, "b": [2, 3]}, "json")
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload == {"a": 1, "b": [2, 3]}

    def test_emits_pretty_json_for_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit([{"x": 1}, {"x": 2}], "json")
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload == [{"x": 1}, {"x": 2}]

    def test_serializes_pydantic_model(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit(_SampleModel(name="cam", count=3), "json")
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload == {"name": "cam", "count": 3}


class TestEmitJsonlMode:
    def test_one_line_per_list_item(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit([{"x": 1}, {"x": 2}, {"x": 3}], "jsonl")
        captured = capsys.readouterr()
        lines = [line for line in captured.out.splitlines() if line]
        assert len(lines) == 3
        assert json.loads(lines[0]) == {"x": 1}
        assert json.loads(lines[2]) == {"x": 3}

    def test_single_line_for_non_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit({"a": 1}, "jsonl")
        captured = capsys.readouterr()
        lines = [line for line in captured.out.splitlines() if line]
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"a": 1}


class TestEmitQuietMode:
    def test_suppresses_all_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit([{"x": 1}, {"x": 2}], "quiet")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestEmitTextMode:
    def test_renders_dict_as_key_value_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit({"name": "cam", "count": 3}, "text")
        captured = capsys.readouterr()
        assert "name: cam" in captured.out
        assert "count: 3" in captured.out

    def test_renders_string_verbatim(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit("hello world", "text")
        captured = capsys.readouterr()
        assert captured.out.strip() == "hello world"

    def test_empty_list_produces_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit([], "text")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_renders_list_of_records_with_blank_separator(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        emit([{"a": 1}, {"a": 2}], "text")
        captured = capsys.readouterr()
        assert "a: 1" in captured.out
        assert "a: 2" in captured.out


# ---------------------------------------------------------------------------
# add_output_options decorator
# ---------------------------------------------------------------------------


def _make_test_command() -> click.Command:
    """Trivial Click command that echoes its resolved output_mode."""

    @click.command()
    @add_output_options
    def _cmd(output_mode: OutputMode) -> None:
        click.echo(f"mode={output_mode}")

    return _cmd


class TestAddOutputOptionsDecorator:
    def test_default_is_text(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), [])
        assert result.exit_code == 0
        assert "mode=text" in result.output

    def test_json_flag_sets_mode_to_json(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), ["--json"])
        assert result.exit_code == 0
        assert "mode=json" in result.output

    def test_jsonl_flag_sets_mode_to_jsonl(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), ["--jsonl"])
        assert result.exit_code == 0
        assert "mode=jsonl" in result.output

    def test_quiet_flag_sets_mode_to_quiet(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), ["--quiet"])
        assert result.exit_code == 0
        assert "mode=quiet" in result.output

    def test_explicit_output_choice_works(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), ["--output", "json"])
        assert result.exit_code == 0
        assert "mode=json" in result.output

    def test_json_and_jsonl_together_exits_64(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), ["--json", "--jsonl"])
        assert result.exit_code == 64
        # Conflict diagnostic lands on stderr.
        assert "conflict" in (result.stderr or "").lower()

    def test_quiet_and_json_together_exits_64(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), ["--quiet", "--json"])
        assert result.exit_code == 64

    def test_json_flag_plus_matching_output_is_allowed(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), ["--json", "--output", "json"])
        assert result.exit_code == 0
        assert "mode=json" in result.output

    def test_json_flag_plus_conflicting_output_exits_64(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_test_command(), ["--json", "--output", "jsonl"])
        assert result.exit_code == 64

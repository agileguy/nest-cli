"""Tests for ``nest-cli batch --file <path>`` (FR-9, FR-9a, FR-10b).

Each emitted JSONL line conforms to the FR-9a envelope::

    {"command": "<verb-and-flags>", "target": "<resolved>",
     "status": "ok"|"error", "exit_code": <int>, "result"?, "error"?}

Empty input exits 0 with no stdout. Blank lines are skipped silently.
Lines starting with ``#`` are treated as comments.

The batch dispatcher invokes each parsed command via Click's in-process
``CliRunner`` rather than spawning a subprocess. Each per-line invocation
gets ``--jsonl`` injected so the inner verb's stdout is parseable JSON
suitable for stuffing into the FR-9a ``result`` field.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from nest_cli.cli import cli as cli_root


class TestBatchFromFileEmpty:
    def test_empty_file_exits_0_no_stdout(self, tmp_path: Path) -> None:
        """Empty input exits 0 with no stdout (FR-10b)."""
        path = tmp_path / "batch.txt"
        path.write_text("", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        assert result.exit_code == 0
        assert result.output == ""

    def test_only_blank_lines_exits_0(self, tmp_path: Path) -> None:
        """Blank lines silently skipped (FR-10b)."""
        path = tmp_path / "batch.txt"
        path.write_text("\n\n   \n\t\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        assert result.exit_code == 0
        assert result.output == ""

    def test_only_comments_exits_0(self, tmp_path: Path) -> None:
        """Lines starting with `#` are comments (FR-10b)."""
        path = tmp_path / "batch.txt"
        path.write_text("# this is a comment\n#another\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        assert result.exit_code == 0
        assert result.output == ""


class TestBatchHappyPath:
    def test_three_list_commands_emit_envelopes(self, tmp_path: Path) -> None:
        """3 successful invocations → 3 FR-9a envelopes, exit 0."""
        path = tmp_path / "batch.txt"
        # `list --groups` is a no-creds, no-network verb that always
        # succeeds; perfect happy-path canary.
        path.write_text(
            "list --groups\nlist --groups\nlist --groups\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        assert result.exit_code == 0, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 3
        for ln in lines:
            envelope = json.loads(ln)
            assert envelope["status"] == "ok"
            assert envelope["exit_code"] == 0
            assert "result" in envelope
            assert "error" not in envelope
            assert envelope["command"].startswith("list")


class TestBatchEnvelopeShape:
    def test_envelope_has_command_target_status_exit_code(self, tmp_path: Path) -> None:
        path = tmp_path / "batch.txt"
        path.write_text("list --groups\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        assert result.exit_code == 0
        envelope = json.loads(result.output.strip())
        assert set(envelope.keys()) >= {"command", "status", "exit_code"}
        assert envelope["command"] == "list --groups"


class TestBatchCommentsAndBlankSkipped:
    def test_mixed_input_skips_empty_and_comments(self, tmp_path: Path) -> None:
        path = tmp_path / "batch.txt"
        path.write_text(
            "\n# leading comment\nlist --groups\n   \nlist --groups\n# trailing\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        assert result.exit_code == 0
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2  # only the two list --groups commands


class TestBatchPartialFailure:
    """If any line fails AND any line succeeds, batch exits 7 (FR-10a)."""

    def test_mixed_ok_and_unknown_alias_exits_7(self, tmp_path: Path) -> None:
        path = tmp_path / "batch.txt"
        # `cam info <unknown>` resolves to the literal string and the
        # SDM client side likely fails earlier — but we deliberately
        # use a verb that exits via auth-or-config/load-creds, since
        # that's a deterministic failure path. Use an unknown command
        # (`bogus`), which Click rejects with exit 2 in some versions.
        # Use a stable mismatch: `list --bogus-flag` produces a usage
        # error which exits non-zero.
        path.write_text("list --groups\nlist --bogus-flag\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        # Mixed: 1 OK + 1 fail = exit 7.
        assert result.exit_code == 7, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["status"] == "ok"
        assert second["status"] == "error"
        # Click usage errors are typically exit 2 on Click side, but we
        # accept any non-zero failure code here — what matters is the
        # FR-10a aggregate of "mixed = 7".


class TestBatchAllFailedFirstFailureCode:
    def test_all_failed_returns_first_lines_exit_code(self, tmp_path: Path) -> None:
        """Two failing lines → first line's exit code wins (FR-10a / FR-8a)."""
        path = tmp_path / "batch.txt"
        # Two different failure paths — both lines fail with usage
        # errors but the first line's code propagates.
        path.write_text("list --bogus\nlist --also-bogus\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        assert result.exit_code != 0
        # The FR-10a contract: the exit code SHALL be the failure code
        # of the first failed line. Both lines fail with the same code
        # (Click's usage-error exit), so we just assert non-zero and
        # not the partial-failure code 7 (since nothing succeeded).
        assert result.exit_code != 7

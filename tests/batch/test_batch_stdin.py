"""Tests for ``nest-cli batch --stdin`` (FR-10).

Mirror the file-mode tests but sourcing the command stream from stdin.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from nest_cli.cli import cli as cli_root


class TestBatchStdin:
    def test_stdin_three_commands_exit_0(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["batch", "--stdin"],
            input="list --groups\nlist --groups\n",
        )
        assert result.exit_code == 0, result.output + result.stderr
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2
        for ln in lines:
            envelope = json.loads(ln)
            assert envelope["status"] == "ok"

    def test_stdin_empty_exits_0_no_stdout(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--stdin"], input="")
        assert result.exit_code == 0
        assert result.output == ""

    def test_stdin_blank_and_comments_skipped(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["batch", "--stdin"],
            input="# header\n\nlist --groups\n# trailing\n",
        )
        assert result.exit_code == 0
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1


class TestBatchModeMutuallyExclusive:
    def test_neither_file_nor_stdin_exits_64(self) -> None:
        """Calling `batch` with neither --file nor --stdin is a usage error."""
        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch"])
        assert result.exit_code == 64, result.output + result.stderr

    def test_both_file_and_stdin_exits_64(self) -> None:
        """`batch --file foo --stdin` is a usage error (mutually exclusive)."""
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["batch", "--file", "/tmp/whatever", "--stdin"],
        )
        assert result.exit_code == 64

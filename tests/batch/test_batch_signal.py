"""Tests for FR-10c: SIGINT/SIGTERM handling during batch execution.

Per FR-10c, the batch verb SHALL:

1. Cease dispatching new sub-ops on SIGINT/SIGTERM.
2. Wait up to 2 seconds for in-flight sub-ops to complete.
3. Emit a final ``{"event":"interrupted","completed":N,"pending":M}``
   line on stdout.
4. Exit 130 (SIGINT) or 143 (SIGTERM).

We patch ``nest_cli.cli.batch_cmd._dispatch_line`` so the second call
sends a signal to ourselves, then the third call (which would have been
made if the loop didn't honor the interrupt) is verified to have NOT
happened. The summary line's ``completed`` counter and the exit code
are the assertion targets.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from nest_cli.cli import cli as cli_root


def _make_dispatch_with_signal_after(after_n_calls: int, signum: int) -> Any:
    """Return a stub dispatcher that sends ``signum`` after N calls."""
    counter = {"n": 0}

    def _stub(raw_line: str) -> dict[str, Any]:
        counter["n"] += 1
        env = {
            "command": raw_line,
            "exit_code": 0,
            "status": "ok",
            "result": {"line": counter["n"]},
        }
        if counter["n"] >= after_n_calls:
            # Send the signal to ourselves so the verb's installed
            # handler picks it up. The handler sets an in-process flag
            # that the loop checks at the next iteration top.
            os.kill(os.getpid(), signum)
        return env

    return _stub, counter


class TestSigintInterruptsBatch:
    def test_sigint_after_two_dispatches_emits_interrupted_summary(self, tmp_path: Path) -> None:
        """5-line batch + SIGINT-after-2 → completed:2, pending:3, exit 130."""
        path = tmp_path / "batch.txt"
        path.write_text(
            "list --groups\nlist --groups\nlist --groups\nlist --groups\nlist --groups\n",
            encoding="utf-8",
        )
        stub, counter = _make_dispatch_with_signal_after(2, signal.SIGINT)
        with patch("nest_cli.cli.batch_cmd._dispatch_line", side_effect=stub):
            runner = CliRunner()
            result = runner.invoke(cli_root, ["batch", "--file", str(path)])

        assert result.exit_code == 130, result.output + result.stderr
        # Exactly two dispatches happened (the third was suppressed).
        assert counter["n"] == 2
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        # 2 result envelopes + 1 interrupted summary line.
        assert len(lines) == 3
        summary = json.loads(lines[-1])
        assert summary == {"event": "interrupted", "completed": 2, "pending": 3}


class TestSigtermInterruptsBatch:
    def test_sigterm_after_one_dispatch_exits_143(self, tmp_path: Path) -> None:
        """3-line batch + SIGTERM-after-1 → completed:1, pending:2, exit 143."""
        path = tmp_path / "batch.txt"
        path.write_text(
            "list --groups\nlist --groups\nlist --groups\n",
            encoding="utf-8",
        )
        stub, counter = _make_dispatch_with_signal_after(1, signal.SIGTERM)
        with patch("nest_cli.cli.batch_cmd._dispatch_line", side_effect=stub):
            runner = CliRunner()
            result = runner.invoke(cli_root, ["batch", "--file", str(path)])

        assert result.exit_code == 143, result.output + result.stderr
        assert counter["n"] == 1
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2  # 1 result + 1 summary
        summary = json.loads(lines[-1])
        assert summary == {"event": "interrupted", "completed": 1, "pending": 2}


class TestSignalHandlersRestored:
    """Verify the handler save+restore so test isolation isn't leaky."""

    def test_handlers_restored_after_normal_exit(self, tmp_path: Path) -> None:
        """SIGINT/SIGTERM handlers restored after a clean batch run."""
        path = tmp_path / "batch.txt"
        path.write_text("list --groups\n", encoding="utf-8")

        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)

        runner = CliRunner()
        result = runner.invoke(cli_root, ["batch", "--file", str(path)])
        assert result.exit_code == 0

        # The verb installed handlers and removed them in finally.
        # Verify neither handler is the verb's anonymous closure.
        assert signal.getsignal(signal.SIGINT) is prev_sigint
        assert signal.getsignal(signal.SIGTERM) is prev_sigterm

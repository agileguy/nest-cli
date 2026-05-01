"""Phase 0 smoke tests — package importable, CLI entry point wired up."""

from click.testing import CliRunner

import nest_cli
from nest_cli.__main__ import main


def test_version_constant_matches_pyproject() -> None:
    """The package-level __version__ is the canonical value Phase 1 will bump."""
    assert nest_cli.__version__ == "0.0.1"


def test_version_flag_exits_clean() -> None:
    """`nest-cli --version` must exit 0 so CI can sanity-check the install."""
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.0.1" in result.output


def test_no_args_shows_help() -> None:
    """Invoking with no subcommand prints help instead of crashing."""
    runner = CliRunner()
    result = runner.invoke(main, [])
    assert result.exit_code == 0
    assert "nest-cli" in result.output

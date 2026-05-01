"""Tests for ``nest_cli.cli.config_cmd`` — config show/validate.

Covers FR-16c.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

# Import submodules explicitly so monkeypatch can resolve string paths
# like "nest_cli.cli.config_cmd.default_config_path". The package init
# re-exports the Click command object under the same name, which would
# otherwise shadow the submodule.
import nest_cli.cli.cam_cmd  # noqa: F401
import nest_cli.cli.config_cmd  # noqa: F401
import nest_cli.cli.list_cmd  # noqa: F401
from nest_cli.cli import cli as cli_root


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    config_path = tmp_path / "config.toml"

    def _fake_config_path() -> Path:
        return config_path

    monkeypatch.setattr("nest_cli.config.default_config_path", _fake_config_path)
    monkeypatch.setattr("nest_cli.cli.config_cmd.default_config_path", _fake_config_path)

    return {"config": config_path}


class TestConfigShow:
    def test_shows_empty_config_when_file_missing(self, fake_paths: dict[str, Path]) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "show", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["aliases"] == {}
        assert payload["groups"] == {}
        assert payload["exists"] is False

    def test_shows_resolved_config(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n'
            '[groups]\nall = ["front-door"]\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "show", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["aliases"] == {"front-door": "enterprises/proj/devices/abc"}
        assert payload["groups"] == {"all": ["front-door"]}
        assert payload["exists"] is True

    def test_text_mode_emits_toml(self, fake_paths: dict[str, Path]) -> None:
        """FR-16c: text-mode ``config show`` emits TOML, not key:value lines."""
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "show"])
        assert result.exit_code == 0
        # The output is a TOML document, not human-prose key/value lines.
        assert "[aliases]" in result.output
        assert 'front-door = "enterprises/proj/devices/abc"' in result.output

    def test_text_mode_round_trips_via_tomllib(self, fake_paths: dict[str, Path]) -> None:
        """The text-mode TOML output round-trips through ``tomllib.loads``.

        Covers tricky escapes (backslash and double-quote inside a value)
        so the serializer's basic-string handling stays honest.
        """
        original_aliases = {
            "plain": "enterprises/proj/devices/abc",
            "needs-quote": 'has "quote" inside',
            "needs-backslash": r"a\b\c",
        }
        original_groups = {
            "all": ["plain", "needs-quote"],
        }
        with fake_paths["config"].open("w", encoding="utf-8") as fh:
            fh.write("[aliases]\n")
            for k, v in original_aliases.items():
                escaped = v.replace("\\", "\\\\").replace('"', '\\"')
                fh.write(f'{k} = "{escaped}"\n')
            fh.write("[groups]\n")
            for k, members in original_groups.items():
                quoted = ", ".join(f'"{m}"' for m in members)
                fh.write(f"{k} = [{quoted}]\n")

        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "show"])
        assert result.exit_code == 0
        # Round-trip the emitted TOML back through tomllib.
        parsed = tomllib.loads(result.output)
        assert parsed["aliases"] == original_aliases
        assert parsed["groups"] == original_groups

    def test_text_mode_empty_config(self, fake_paths: dict[str, Path]) -> None:
        """An empty config file emits an empty (but well-formed) text payload."""
        # File doesn't exist — load_config returns an empty Config.
        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "show"])
        assert result.exit_code == 0
        # Empty config has no sections; TOML output is at most a trailing
        # newline.
        assert result.output.strip() == ""


class TestConfigValidate:
    def test_valid_config_exits_zero(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "validate", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["alias_count"] == 1

    def test_invalid_toml_exits_6(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            "this is [ not valid toml\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "validate", "--json"])
        assert result.exit_code == 6

    def test_unknown_section_exits_6(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            "[some_unknown_section]\nkey = 'v'\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "validate", "--json"])
        assert result.exit_code == 6

    def test_group_member_not_in_aliases_exits_6(self, fake_paths: dict[str, Path]) -> None:
        fake_paths["config"].write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n'
            '[groups]\ngrp = ["front-door", "missing-alias"]\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_root, ["config", "validate", "--json"])
        assert result.exit_code == 6

    def test_explicit_path_argument(self, tmp_path: Path) -> None:
        # Validate accepts a positional path argument independent of
        # the default-path resolution.
        target = tmp_path / "elsewhere.toml"
        target.write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(
            cli_root,
            ["config", "validate", str(target), "--json"],
        )
        assert result.exit_code == 0

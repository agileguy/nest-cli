"""Tests for ``nest_cli.config`` — TOML parser and Config schema.

Covers FR-16, FR-16a, FR-16b, FR-16c (SRD §5.9) and the unknown-section
exit-6 path (FR-67 / SRD §11.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nest_cli.config import (
    Config,
    default_config_path,
    is_known_alias,
    load_config,
    resolve_alias,
)
from nest_cli.errors import EXIT_CONFIG_ERROR, StructuredError


class TestDefaultConfigPath:
    def test_honors_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/some-xdg")
        assert default_config_path() == Path("/tmp/some-xdg/nest-cli/config.toml")

    def test_falls_back_to_home_dotconfig(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        path = default_config_path()
        assert path.name == "config.toml"
        assert path.parent.name == "nest-cli"
        assert path.parent.parent.name == ".config"


class TestLoadConfigHappyPath:
    def test_parses_aliases_section(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n'
            'kitchen-cam = "enterprises/proj/devices/def"\n',
            encoding="utf-8",
        )
        cfg = load_config(cfg_path)
        assert cfg.aliases == {
            "front-door": "enterprises/proj/devices/abc",
            "kitchen-cam": "enterprises/proj/devices/def",
        }

    def test_parses_groups_section(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[aliases]\na = "enterprises/p/devices/1"\nb = "enterprises/p/devices/2"\n'
            '\n[groups]\nall = ["a", "b"]\nupstairs = ["a"]\n',
            encoding="utf-8",
        )
        cfg = load_config(cfg_path)
        assert cfg.groups == {"all": ["a", "b"], "upstairs": ["a"]}

    def test_parses_both_sections_together(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[aliases]\nfront-door = "enterprises/proj/devices/abc"\n'
            '[groups]\ngroup-a = ["front-door"]\n',
            encoding="utf-8",
        )
        cfg = load_config(cfg_path)
        assert cfg.aliases["front-door"] == "enterprises/proj/devices/abc"
        assert cfg.groups["group-a"] == ["front-door"]

    def test_missing_file_returns_empty_config(self, tmp_path: Path) -> None:
        # FR-16b: default location missing → built-in defaults, NOT an error.
        cfg = load_config(tmp_path / "does-not-exist.toml")
        assert cfg.aliases == {}
        assert cfg.groups == {}


class TestLoadConfigErrors:
    def test_unknown_top_level_section_raises_exit6(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            "[some_unknown_section]\nkey = 'value'\n",
            encoding="utf-8",
        )
        with pytest.raises(StructuredError) as exc_info:
            load_config(cfg_path)
        assert exc_info.value.code == EXIT_CONFIG_ERROR
        msg = exc_info.value.message
        assert "some_unknown_section" in msg or "extra" in msg.lower()

    def test_invalid_toml_raises_exit6(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("this is not [ valid toml", encoding="utf-8")
        with pytest.raises(StructuredError) as exc_info:
            load_config(cfg_path)
        assert exc_info.value.code == EXIT_CONFIG_ERROR

    def test_wrong_alias_value_type_raises_exit6(self, tmp_path: Path) -> None:
        # aliases values must be strings; integer should fail validation.
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("[aliases]\nfoo = 42\n", encoding="utf-8")
        with pytest.raises(StructuredError) as exc_info:
            load_config(cfg_path)
        assert exc_info.value.code == EXIT_CONFIG_ERROR


class TestResolveAlias:
    def test_resolves_known_alias(self) -> None:
        cfg = Config(aliases={"front-door": "enterprises/p/devices/abc"})
        assert resolve_alias(cfg, "front-door") == "enterprises/p/devices/abc"

    def test_passes_through_unknown_input(self) -> None:
        cfg = Config(aliases={"front-door": "enterprises/p/devices/abc"})
        # Literal SDM path goes through unchanged.
        assert (
            resolve_alias(cfg, "enterprises/other-proj/devices/xyz")
            == "enterprises/other-proj/devices/xyz"
        )

    def test_is_known_alias_returns_bool(self) -> None:
        cfg = Config(aliases={"front-door": "enterprises/p/devices/abc"})
        assert is_known_alias(cfg, "front-door") is True
        assert is_known_alias(cfg, "back-door") is False

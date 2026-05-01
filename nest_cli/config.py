"""TOML config parser for nest-cli (SRD §5.9 / §9).

v0.1.0 surfaces a deliberately small subset of the SRD §9.2 schema:

- ``[aliases]`` — map of alias name → target identifier (SDM device path
  for cam, ``wifi:groups/...`` for wifi). Replaces the §9.2 nested
  ``[devices.<alias>]`` blocks for the v0.1.0 release; §9.2's richer
  nested form is on the roadmap for a later phase.
- ``[groups]`` — map of group name → list of alias names.

Unknown top-level sections raise ``StructuredError(code=EXIT_CONFIG_ERROR)``
per FR-67 / SRD §11.1.

Path resolution honors ``XDG_CONFIG_HOME`` (Linux convention) with a
``~/.config`` fallback (macOS / unset). The default file is
``$XDG_CONFIG_HOME/nest-cli/config.toml`` (or ``~/.config/nest-cli/config.toml``).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from nest_cli.errors import EXIT_CONFIG_ERROR, StructuredError


class Config(BaseModel):
    """Resolved nest-cli config (subset for v0.1.0).

    ``extra="forbid"`` ensures unknown top-level sections raise on
    validation, which the loader translates into exit 6.
    """

    model_config = ConfigDict(extra="forbid")

    aliases: dict[str, str] = {}
    groups: dict[str, list[str]] = {}


def default_config_path() -> Path:
    """Return the default config path honoring ``XDG_CONFIG_HOME``.

    - ``$XDG_CONFIG_HOME/nest-cli/config.toml`` if the env var is set.
    - ``~/.config/nest-cli/config.toml`` otherwise.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "nest-cli" / "config.toml"
    return Path.home() / ".config" / "nest-cli" / "config.toml"


def load_config(path: Path) -> Config:
    """Load and validate the TOML config at ``path``.

    Failure modes (mapped to SRD §11.1 exit codes):

    - File missing → return empty ``Config()`` (FR-16b — built-in defaults
      are not an error). A separate stderr INFO log is the caller's
      responsibility.
    - File present but TOML parse fails → exit 6.
    - File present but schema validation fails (unknown section, wrong
      type) → exit 6.
    """
    if not path.exists():
        return Config()

    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise StructuredError(
            code=EXIT_CONFIG_ERROR,
            message=f"could not read config at {path}: {exc}",
            hint="Check file exists and is readable, or pass --config <path>.",
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise StructuredError(
            code=EXIT_CONFIG_ERROR,
            message=f"config at {path} is not valid TOML: {exc}",
            hint="Run `nest-cli config validate` for line-level diagnostics.",
        ) from exc

    return _validate(raw, source=path)


def load_config_or_empty(path: Path | None) -> Config:
    """Convenience: load from ``path`` (or default) returning empty on miss."""
    target = path if path is not None else default_config_path()
    return load_config(target)


def _validate(raw: dict[str, Any], *, source: Path) -> Config:
    """Coerce a raw TOML dict into a ``Config``, mapping errors to exit 6."""
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        # Pull the first error so the message names a specific field.
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
        msg = first.get("msg", "invalid")
        raise StructuredError(
            code=EXIT_CONFIG_ERROR,
            message=f"config at {source} is invalid: {loc}: {msg}",
            hint=(
                "Check for unknown top-level sections (only [aliases] and "
                "[groups] are recognized in v0.1.0) and verify alias values "
                "are non-empty strings."
            ),
            details={"source": str(source), "field": loc, "reason": msg},
        ) from exc


def resolve_alias(config: Config, alias_or_target: str) -> str:
    """Return the resolved target string for ``alias_or_target``.

    If ``alias_or_target`` is a key in ``config.aliases``, the configured
    target is returned. Otherwise the input is returned verbatim — the
    operator may have passed a literal SDM device path. Unknown-alias
    detection happens at the verb layer (which has access to the alias
    list to provide hints).
    """
    return config.aliases.get(alias_or_target, alias_or_target)


def is_known_alias(config: Config, name: str) -> bool:
    """Return True if ``name`` is a configured alias key."""
    return name in config.aliases

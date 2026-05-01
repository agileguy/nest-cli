"""``nest-cli config`` subgroup — local config inspection.

Implements FR-16c (SRD §5.9):

- ``config show`` — print the resolved config (no live API calls).
- ``config validate [<path>]`` — load + validate a config file; exit 0
  on success, 6 on parse / schema failure.

Both verbs are read-only and never touch credentials. ``config show``
honors the standard output-mode flags so it's pipeable into ``jq``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from nest_cli.cli._shared import exit_on_structured_error
from nest_cli.config import Config, default_config_path, load_config
from nest_cli.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    StructuredError,
)
from nest_cli.output import OutputMode, add_output_options, emit

config_group = click.Group(
    name="config",
    help="Local config inspection (read-only). Implements FR-16c.",
)


def _toml_basic_string(value: str) -> str:
    """Quote ``value`` as a TOML basic string with backslash and ``"`` escaped.

    The v0.1.0 alias/group schema is restricted to plain strings — no
    multiline, no literal-string forms — so the basic-string variant
    (double-quoted with ``\\\\`` and ``\\"`` escapes) round-trips through
    ``tomllib.loads`` deterministically.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _config_to_toml(cfg: Config) -> str:
    """Serialize ``cfg`` to TOML matching the §9.2 schema.

    FR-16c requires text-mode ``config show`` to print the resolved config
    in TOML format. Sections are emitted in a stable order
    (``[aliases]`` then ``[groups]``) so the output is determinism-safe
    per FR-25 and round-trips through ``tomllib.loads`` (validated by
    ``tests/test_cli_config.py::TestConfigShow.test_text_mode_round_trips_via_tomllib``).
    """
    lines: list[str] = []
    if cfg.aliases:
        lines.append("[aliases]")
        for k, v in sorted(cfg.aliases.items()):
            lines.append(f"{k} = {_toml_basic_string(v)}")
        lines.append("")
    if cfg.groups:
        lines.append("[groups]")
        for k, members in sorted(cfg.groups.items()):
            quoted = ", ".join(_toml_basic_string(m) for m in members)
            lines.append(f"{k} = [{quoted}]")
    return "\n".join(lines).rstrip() + "\n"


@config_group.command("show")
@click.option(
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help="Path to a config TOML file. Defaults to the resolved location.",
)
@add_output_options
def config_show(config_path: str | None, output_mode: OutputMode) -> None:
    """Print the resolved config.

    Implements FR-16c (the ``config show`` half). Text mode prints the
    config in TOML format (per FR-16c) so the output round-trips back
    through ``tomllib``. JSON / JSONL / quiet modes use the canonical
    record dict so jq pipelines see a stable shape.

    Per FR-16d, the v0.1.0 schema (``[aliases]``, ``[groups]``) does
    not contain secrets, so no redaction is needed.
    """
    target_path = Path(config_path) if config_path else default_config_path()
    try:
        cfg = load_config(target_path)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    if output_mode == "text":
        click.echo(_config_to_toml(cfg), nl=False)
        return

    emit(
        {
            "path": str(target_path),
            "exists": target_path.exists(),
            "aliases": dict(cfg.aliases),
            "groups": {k: list(v) for k, v in cfg.groups.items()},
        },
        output_mode,
    )


@config_group.command("validate")
@click.argument(
    "config_path",
    type=click.Path(),
    required=False,
)
@add_output_options
def config_validate(config_path: str | None, output_mode: OutputMode) -> None:
    """Validate the config TOML; exit 0 if OK, 6 with structured error if not.

    Implements FR-16c (the ``config validate`` half). Validation covers:

    - File exists and is readable (or default path is missing — that's OK,
      built-in defaults are not an error per FR-16b).
    - File parses as TOML.
    - Top-level sections are recognized (``[aliases]``, ``[groups]``).
    - Alias values are non-empty strings.
    - Group values are lists of strings.
    """
    target_path = Path(config_path) if config_path else default_config_path()
    try:
        cfg = load_config(target_path)
    except StructuredError as exc:
        exit_on_structured_error(exc, output_mode)

    # Additional cross-reference checks: every member of every group
    # SHOULD resolve to a configured alias. Unknown-member references
    # raise a config error (exit 6) — silently ignoring would let typos
    # slide into runtime fan-out errors.
    for group_name, members in cfg.groups.items():
        for member in members:
            if member not in cfg.aliases:
                exit_on_structured_error(
                    StructuredError(
                        code=EXIT_CONFIG_ERROR,
                        message=(
                            f"group {group_name!r} references unknown alias "
                            f"{member!r}; add it to [aliases] or remove it from the group"
                        ),
                        details={"group": group_name, "missing_alias": member},
                    ),
                    output_mode,
                )

    emit(
        {
            "status": "ok",
            "path": str(target_path),
            "alias_count": len(cfg.aliases),
            "group_count": len(cfg.groups),
        },
        output_mode,
    )
    sys.exit(EXIT_OK)

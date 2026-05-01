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

import click

from nest_cli.config import default_config_path, load_config
from nest_cli.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    StructuredError,
    emit_structured_error_to_stderr,
)
from nest_cli.output import OutputMode, add_output_options, emit

config_group = click.Group(
    name="config",
    help="Local config inspection (read-only). Implements FR-16c.",
)


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

    Implements FR-16c (the ``config show`` half). Default output is
    JSON-rendered; pass ``--output text`` for a human-readable form.
    Per FR-16d, the v0.1.0 schema (``[aliases]``, ``[groups]``) does
    not contain secrets, so no redaction is needed.
    """
    from pathlib import Path

    target_path = Path(config_path) if config_path else default_config_path()
    try:
        cfg = load_config(target_path)
    except StructuredError as exc:
        emit_structured_error_to_stderr(exc, output_mode)
        sys.exit(exc.code)

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
    from pathlib import Path

    target_path = Path(config_path) if config_path else default_config_path()
    try:
        cfg = load_config(target_path)
    except StructuredError as exc:
        emit_structured_error_to_stderr(exc, output_mode)
        sys.exit(exc.code)

    # Additional cross-reference checks: every member of every group
    # SHOULD resolve to a configured alias. Unknown-member references
    # raise a config error (exit 6) — silently ignoring would let typos
    # slide into runtime fan-out errors.
    for group_name, members in cfg.groups.items():
        for member in members:
            if member not in cfg.aliases:
                err = StructuredError(
                    code=EXIT_CONFIG_ERROR,
                    message=(
                        f"group {group_name!r} references unknown alias "
                        f"{member!r}; add it to [aliases] or remove it from the group"
                    ),
                    details={"group": group_name, "missing_alias": member},
                )
                emit_structured_error_to_stderr(err, output_mode)
                sys.exit(err.code)

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

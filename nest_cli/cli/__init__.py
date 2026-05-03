"""Click root group ``cli`` for ``nest-cli``.

This package wires every subcommand under a single ``click.Group`` named
``cli``. The console script entry point in ``pyproject.toml`` and
``nest_cli/__main__.py`` both import this group as ``main``.

Subcommand layout:

- ``auth``     — OAuth (cam) + Foyer master token (wifi) credentials management.
- ``cam``      — camera commands (list/info/capabilities/snapshot/stream/...).
- ``config``   — config inspection (show/validate).
- ``list``     — alias/group listing (FR-1..1d).
- ``discover`` — live SDM device probe (FR-2/2a).
- ``wifi``     — Wi-Fi mesh commands (FR-WIFI-1..3, EXPERIMENTAL).
"""

from __future__ import annotations

import click

from nest_cli import __version__

# Import the list_cmd module (NOT the command object directly) and pull
# the commands out via attribute access. Importing under the same names
# as the submodules (``list_cmd``, ``discover_cmd`` is fine — they're
# different modules) would otherwise let the Click command object
# shadow the submodule on ``nest_cli.cli`` package attribute lookup,
# which breaks monkeypatch in test code.
from nest_cli.cli import list_cmd as _list_module
from nest_cli.cli.auth_cmd import auth_group
from nest_cli.cli.cam_cmd import cam_group
from nest_cli.cli.config_cmd import config_group
from nest_cli.cli.wifi_cmd import wifi_group

_list_cmd = _list_module.list_cmd
_discover_cmd = _list_module.discover_cmd


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="nest-cli")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """nest-cli — Google Nest cameras (SDM) and Nest Wi-Fi (experimental, Foyer).

    The cam surface (``cam``, ``list``, ``discover``) ships in v0.1.0.
    The wifi surface is gated behind ``--experimental-wifi`` and ships
    in v0.3.0 (Phase 3).
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


cli.add_command(auth_group)
cli.add_command(cam_group)
cli.add_command(config_group)
cli.add_command(_list_cmd)
cli.add_command(_discover_cmd)
cli.add_command(wifi_group)


__all__ = ["cli"]

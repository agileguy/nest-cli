"""Entry point for `python -m nest_cli` and the `nest-cli` console script."""

import click

from nest_cli import __version__


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="nest-cli")
@click.pass_context
def main(ctx: click.Context) -> None:
    """nest-cli — Google Nest cameras (SDM) and Nest Wi-Fi (experimental, Foyer).

    Phase 0 skeleton — Phase 1 wires up the real verbs.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


if __name__ == "__main__":
    main()

import sys
from pathlib import Path
from typing import Annotated

import typer

from slackattic import __version__
from slackattic.core.config import BOT_TOKEN_PREFIX, load_config
from slackattic.core.errors import ExitCode, SlackatticError
from slackattic.slack.client import SlackClient
from slackattic.sync.messages import sync_messages
from slackattic.sync.metadata import sync_metadata

app = typer.Typer(
    help="Daily archive and searchable viewer for free Slack workspaces.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Config file path. Defaults to ./slackattic.toml."),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"slackattic {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    config: ConfigOption = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    ctx.obj = config


def _fail(error: SlackatticError) -> typer.Exit:
    typer.echo(f"error: {error}", err=True)
    return typer.Exit(int(error.exit_code))


def _not_implemented(name: str) -> None:
    typer.echo(f"'{name}' is not implemented yet.", err=True)
    raise typer.Exit(int(ExitCode.NOT_IMPLEMENTED))


@app.command()
def sync(ctx: typer.Context) -> None:
    """Fetch new messages into the archive."""
    try:
        config = load_config(ctx.obj)
        client = SlackClient(config.token)
        meta = sync_metadata(client, config)
    except SlackatticError as e:
        raise _fail(e) from None
    for name, changed in (
        ("channels.json", meta.channels_changed),
        ("users.json", meta.users_changed),
    ):
        typer.echo(f"{name}: {'updated' if changed else 'unchanged'}")
    for cid in meta.missing:
        typer.echo(
            f"warning: channel {cid} is unknown, archived or the bot is not a member", err=True
        )

    names = {c["id"]: c["name"] for c in meta.selected}
    try:
        result = sync_messages(client, config.archive_dir, list(names), config.rescan_days)
    except SlackatticError as e:
        # Channels finished before the failure keep their saved progress
        raise _fail(e) from None
    for ch in result.channels:
        typer.echo(
            f"#{names[ch.channel_id]} ({ch.channel_id}): "
            f"fetched {ch.fetched}, new {ch.added}, updated {ch.updated}, "
            f"deleted {ch.deleted}, threads {ch.threads}"
        )
        for skipped in ch.skipped_threads:
            typer.echo(f"warning: skipped thread, will retry next run: {skipped}", err=True)
    for skipped in result.skipped:
        typer.echo(f"warning: skipped channel {skipped.channel}: {skipped}", err=True)


@app.command("import")
def import_() -> None:
    """Import an official Slack export zip."""
    _not_implemented("import")


@app.command()
def build() -> None:
    """Build the static viewer site."""
    _not_implemented("build")


@app.command()
def serve() -> None:
    """Serve the viewer site locally."""
    _not_implemented("serve")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check the configuration and the Slack connection."""
    try:
        config = load_config(ctx.obj)
        typer.echo(f"token:       {BOT_TOKEN_PREFIX}**** (set)")
        typer.echo(f"archive_dir: {config.archive_dir}")
        typer.echo(f"channels:    {', '.join(config.channels) or 'all joined channels'}")
        typer.echo("config OK")
        auth = SlackClient(config.token).auth_test()
    except SlackatticError as e:
        raise _fail(e) from None
    typer.echo(f"workspace:   {auth.get('team')} ({auth.get('url')})")
    typer.echo(f"bot user:    {auth.get('user')}")
    typer.echo("slack OK")


def main() -> None:
    # Never crash on characters the console encoding cannot show
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    app()

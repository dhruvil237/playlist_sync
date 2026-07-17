"""Main Click CLI for playlist-sync."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Optional, TypeVar

import click
from rich.console import Console
from rich.table import Table

from playlist_sync.cli.interactive import print_sync_summary, prompt_resolve
from playlist_sync.core.models import ConflictStrategy, Platform, SyncResult
from playlist_sync.core.syncer import Syncer
from playlist_sync.platforms.registry import get_platform_class, list_platforms
from playlist_sync.platforms.spotify import has_usable_saved_auth as has_usable_spotify_auth
from playlist_sync.platforms.ytmusic import HEADERS_FILE, OAUTH_FILE
from playlist_sync.storage.database import create_db
from playlist_sync.storage.token_store import delete_token, has_token

console = Console()
T = TypeVar("T")

PLATFORM_CHOICES = [p.value for p in Platform if p in [Platform.SPOTIFY, Platform.YTMUSIC]]


def _make_platform(name: str):  # type: ignore[no-untyped-def]
    platform = Platform(name)
    cls = get_platform_class(platform)
    return cls()


def _run_async(coro: Awaitable[T]) -> T:
    try:
        return asyncio.run(coro)
    except click.ClickException:
        raise
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _has_saved_auth(platform: Platform) -> bool:
    if platform == Platform.SPOTIFY:
        return has_usable_spotify_auth()
    if platform == Platform.YTMUSIC:
        return HEADERS_FILE.exists() or OAUTH_FILE.exists()
    return has_token(platform.value)


def _clear_saved_auth(platform: Platform) -> None:
    if platform == Platform.SPOTIFY:
        delete_token("spotify")
        delete_token("spotify_spdc")
        return
    if platform == Platform.YTMUSIC:
        HEADERS_FILE.unlink(missing_ok=True)
        OAUTH_FILE.unlink(missing_ok=True)
        return
    delete_token(platform.value)


@click.group()
@click.version_option(package_name="playlist-sync")
def cli() -> None:
    """playlist-sync — Universal playlist syncer across streaming platforms."""
    from dotenv import load_dotenv
    load_dotenv()


# ── auth ─────────────────────────────────────────────────────────────────────

@cli.group()
def auth() -> None:
    """Manage browser sessions and saved platform credentials."""


@auth.command("login")
@click.argument("platform", type=click.Choice(PLATFORM_CHOICES))
def auth_login(platform: str) -> None:
    """Open the platform's login flow and store browser/session credentials."""
    async def _run() -> None:
        adapter = _make_platform(platform)
        if platform == Platform.YTMUSIC.value and hasattr(adapter, "ensure_browser_auth"):
            await adapter.ensure_browser_auth(interactive=True)
        await adapter.authenticate()

    _run_async(_run())
    console.print(f"[green]Login complete for {platform}[/]")


@auth.command("logout")
@click.argument("platform", type=click.Choice(PLATFORM_CHOICES))
def auth_logout(platform: str) -> None:
    """Remove stored credentials for a platform."""
    _clear_saved_auth(Platform(platform))
    console.print(f"[yellow]Logged out of {platform}[/]")


@auth.command("status")
def auth_status() -> None:
    """Show whether credentials are stored locally for each platform."""
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Platform")
    table.add_column("Saved credentials")
    for p in list_platforms():
        status = "[green]present[/]" if _has_saved_auth(p) else "[red]missing[/]"
        table.add_row(p.value, status)
    console.print(table)


# ── playlists ─────────────────────────────────────────────────────────────────

@cli.group()
def playlists() -> None:
    """List and inspect playlists."""


@playlists.command("list")
@click.argument("platform", type=click.Choice(PLATFORM_CHOICES))
def playlists_list(platform: str) -> None:
    """List all playlists on a platform."""
    async def _run() -> None:
        adapter = _make_platform(platform)
        await adapter.authenticate()
        pls = await adapter.get_playlists()
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Name")
        table.add_column("ID")
        table.add_column("Tracks", justify="right")
        for pl in pls:
            table.add_row(pl.name, pl.platform_id or "", str(len(pl.tracks)))
        console.print(table)

    _run_async(_run())


# ── sync ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("playlist_name")
@click.option("--from", "source", required=True, type=click.Choice(PLATFORM_CHOICES), help="Source platform")
@click.option("--to", "target", required=True, type=click.Choice(PLATFORM_CHOICES), help="Target platform")
@click.option("--dry-run", is_flag=True, default=False, help="Preview changes without writing")
@click.option("--no-ai", is_flag=True, default=False, help="Disable AI-powered matching")
@click.option("--ai-model", default=None, help="AI model name (default: gpt-4o-mini or AI_MODEL env var)")
@click.option("--ai-base-url", default=None, help="OpenAI-compatible base URL (e.g. http://localhost:11434/v1 for Ollama)")
@click.option("--no-interactive", is_flag=True, default=False, help="Skip interactive resolution prompts")
@click.option("--conflict", default="source_wins", type=click.Choice([c.value for c in ConflictStrategy]),
              help="Conflict resolution strategy")
@click.option("--workers", default=1, type=click.IntRange(1, 16), show_default=True,
              help="Number of concurrent match workers to use during track search and matching")
def sync(
    playlist_name: str,
    source: str,
    target: str,
    dry_run: bool,
    no_ai: bool,
    ai_model: Optional[str],
    ai_base_url: Optional[str],
    no_interactive: bool,
    conflict: str,
    workers: int,
) -> None:
    """Sync PLAYLIST_NAME from one platform to another.

    Example: playlist-sync sync "My Playlist" --from spotify --to ytmusic
    """
    async def _run() -> SyncResult:
        src_adapter = _make_platform(source)
        tgt_adapter = _make_platform(target)

        await src_adapter.authenticate()
        await tgt_adapter.authenticate()

        syncer = Syncer(
            src_adapter,
            tgt_adapter,
            conflict_strategy=ConflictStrategy(conflict),
            use_ai_matching=not no_ai,
            ai_model=ai_model,
            ai_base_url=ai_base_url,
            workers=workers,
        )

        def _on_progress(current: int, total: int, track: object) -> None:
            console.print(f"[dim][{current}/{total}][/] Matching {track} ...", end="\r")

        result = await syncer.sync_playlist(
            playlist_name,
            dry_run=dry_run,
            on_progress=_on_progress,
            on_resolve=None if no_interactive else prompt_resolve,
        )
        return result

    result = _run_async(_run())
    console.print()
    print_sync_summary(result)


@cli.command("sync-liked")
@click.option("--from", "source", required=True, type=click.Choice(PLATFORM_CHOICES))
@click.option("--to", "target", required=True, type=click.Choice(PLATFORM_CHOICES))
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--no-ai", is_flag=True, default=False)
@click.option("--no-interactive", is_flag=True, default=False)
def sync_liked(source: str, target: str, dry_run: bool, no_ai: bool, no_interactive: bool) -> None:
    """Sync liked/saved songs from one platform to another."""
    async def _run() -> SyncResult:
        src_adapter = _make_platform(source)
        tgt_adapter = _make_platform(target)
        await src_adapter.authenticate()
        await tgt_adapter.authenticate()

        syncer = Syncer(src_adapter, tgt_adapter, use_ai_matching=not no_ai)

        def _on_progress(current: int, total: int, track: object) -> None:
            console.print(f"[dim][{current}/{total}][/] {track} ...", end="\r")

        return await syncer.sync_liked_songs(
            dry_run=dry_run,
            on_progress=_on_progress,
            on_resolve=None if no_interactive else prompt_resolve,
        )

    result = _run_async(_run())
    console.print()
    print_sync_summary(result)


# ── history ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--limit", default=20, show_default=True, help="Number of recent runs to show")
def history(limit: int) -> None:
    """Show sync history."""
    from playlist_sync.storage.database import SyncRun

    session_factory = create_db()
    with session_factory() as session:
        runs = session.query(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit).all()

    if not runs:
        console.print("[dim]No sync history yet.[/]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Date", style="dim")
    table.add_column("Playlist")
    table.add_column("From → To")
    table.add_column("Matched", justify="right")
    table.add_column("Not found", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Dry run")

    for run in runs:
        rate_color = "green" if run.success_rate >= 0.8 else "yellow" if run.success_rate >= 0.5 else "red"
        table.add_row(
            run.started_at.strftime("%Y-%m-%d %H:%M"),
            run.playlist_name,
            f"{run.source_platform} → {run.target_platform}",
            str(run.matched),
            str(run.not_found),
            f"[{rate_color}]{run.success_rate:.0%}[/]",
            "yes" if run.dry_run else "",
        )

    console.print(table)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True)
@click.option("--reload", is_flag=True, default=False)
def serve(host: str, port: int, reload: bool) -> None:
    """Start the web UI (recommended — handles auth, sync, history in browser)."""
    from playlist_sync.web.app import run_server
    console.print(f"[bold]Starting playlist-sync UI at[/] http://{host}:{port}")
    console.print("[dim]Make sure your Spotify redirect URI is set to "
                  f"http://{host}:{port}/auth/spotify/callback[/]")
    run_server(host=host, port=port, reload=reload)


if __name__ == "__main__":
    cli()

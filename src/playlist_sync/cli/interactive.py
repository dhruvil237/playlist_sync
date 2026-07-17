"""Rich-based interactive prompts for ambiguous track resolution."""
from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from playlist_sync.core.models import MatchResult, Track

console = Console()


async def prompt_resolve(match: MatchResult) -> Optional[Track]:
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _prompt_resolve_sync, match)


def _prompt_resolve_sync(match: MatchResult) -> Optional[Track]:
    """
    Interactively ask the user to pick the best candidate for an ambiguous match.
    Returns the chosen Track, or None to skip.
    """
    src = match.source_track
    console.print()
    console.print(Panel(
        f"[bold yellow]Ambiguous match[/] (confidence: {match.confidence:.0%})\n"
        f"Source: [bold]{src.title}[/] — {src.artist_str}\n"
        f"Album: {src.album or 'unknown'}  |  "
        f"Duration: {(src.duration_ms or 0) // 1000}s",
        title="[bold red]Needs your input[/]",
        border_style="yellow",
    ))

    if not match.candidates:
        console.print("[dim]No candidates found.[/]")
        return None

    table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("Title")
    table.add_column("Artist")
    table.add_column("Album")
    table.add_column("Dur", justify="right")
    table.add_column("Score", justify="right")

    for i, (track, score) in enumerate(match.candidates[:8]):
        color = "green" if score >= 0.85 else "yellow" if score >= 0.60 else "red"
        table.add_row(
            str(i + 1),
            Text(track.title, style="bold"),
            track.artist_str,
            track.album or "",
            f"{(track.duration_ms or 0) // 1000}s",
            Text(f"{score:.0%}", style=color),
        )

    console.print(table)
    console.print("[dim]Enter number to pick, 's' to skip, or 'q' to quit interactive mode[/]")

    while True:
        raw = console.input("> ").strip().lower()
        if raw == "s":
            return None
        if raw == "q":
            raise KeyboardInterrupt
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(match.candidates):
                chosen, _ = match.candidates[idx]
                console.print(f"[green]Selected:[/] {chosen.title} — {chosen.artist_str}")
                return chosen
        console.print("[red]Invalid input. Try again.[/]")


def print_sync_summary(result) -> None:  # type: ignore[no-untyped-def]
    """Print a rich summary panel after a sync completes."""
    from playlist_sync.core.models import SyncResult

    r: SyncResult = result
    dry_tag = " [DRY RUN]" if r.dry_run else ""

    console.print()
    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()
    grid.add_row("[bold green]Newly matched[/]", str(len(r.matched)))
    grid.add_row("[bold yellow]Ambiguous[/]", str(len(r.ambiguous)))
    grid.add_row("[bold red]Not found[/]", str(len(r.not_found)))
    grid.add_row("[dim]Already in sync[/]", str(len(r.skipped)))
    grid.add_row("[bold]Total[/]", str(r.total))
    if r.needed_matching:
        grid.add_row("[bold]Match rate[/]", f"{r.success_rate:.0%} of {r.needed_matching} needing a match")
    else:
        grid.add_row("[bold]Match rate[/]", "everything already in sync")

    console.print(Panel(
        grid,
        title=f"[bold]Sync complete:{dry_tag} {r.playlist_name!r}[/]  "
              f"[dim]{r.source_platform.value} → {r.target_platform.value}[/]",
        border_style="green" if r.success_rate > 0.8 else "yellow",
    ))

    if r.not_found:
        console.print("\n[bold red]Unmatched tracks:[/]")
        for mr in r.not_found:
            console.print(f"  [dim]✗[/] {mr.source_track.title} — {mr.source_track.artist_str}")

    if r.errors:
        console.print("\n[bold red]Errors:[/]")
        for err in r.errors:
            console.print(f"  [red]{err}[/]")

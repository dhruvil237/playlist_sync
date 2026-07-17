"""Playlist snapshots: capture membership before writes, restore on demand."""
from __future__ import annotations

from typing import Callable, Optional

from playlist_sync.core.models import Platform, Playlist, Track
from playlist_sync.platforms.base import BasePlatform
from playlist_sync.storage.database import PlaylistSnapshot

SessionFactory = Callable  # returns a Session context manager, or None when disabled


def save_snapshot(session_factory: SessionFactory, playlist: Playlist, *, reason: str) -> Optional[int]:  # type: ignore[type-arg]
    """Persist the playlist's current membership. Returns the snapshot id."""
    if playlist.platform_id is None or playlist.platform is None:
        return None
    session_ctx = session_factory()
    if session_ctx is None:
        return None

    with session_ctx as session:
        snapshot = PlaylistSnapshot(
            platform=playlist.platform.value,
            playlist_id=playlist.platform_id,
            playlist_name=playlist.name,
            reason=reason,
            track_count=len(playlist.tracks),
            tracks=[
                {"id": t.platform_id, "title": t.title, "artists": t.artists}
                for t in playlist.tracks
            ],
        )
        session.add(snapshot)
        session.commit()
        return snapshot.id


def list_snapshots(
    session_factory: SessionFactory,  # type: ignore[type-arg]
    *,
    platform: Optional[Platform] = None,
    limit: int = 50,
) -> list[PlaylistSnapshot]:
    session_ctx = session_factory()
    if session_ctx is None:
        return []
    with session_ctx as session:
        query = session.query(PlaylistSnapshot).order_by(PlaylistSnapshot.taken_at.desc())
        if platform is not None:
            query = query.filter(PlaylistSnapshot.platform == platform.value)
        rows = query.limit(limit).all()
        session.expunge_all()
        return rows


def get_snapshot(session_factory: SessionFactory, snapshot_id: int) -> Optional[PlaylistSnapshot]:  # type: ignore[type-arg]
    session_ctx = session_factory()
    if session_ctx is None:
        return None
    with session_ctx as session:
        row = session.get(PlaylistSnapshot, snapshot_id)
        if row is not None:
            session.expunge(row)
        return row


async def restore_snapshot(
    session_factory: SessionFactory,  # type: ignore[type-arg]
    adapter: BasePlatform,
    snapshot: PlaylistSnapshot,
) -> tuple[int, int]:
    """Bring the playlist back to the snapshot's membership.

    Takes a pre_restore snapshot of the current state first, so restores are
    themselves undoable. Returns (added, removed) counts.
    """
    current = await adapter.get_playlist(snapshot.playlist_id)
    save_snapshot(session_factory, current, reason="pre_restore")

    snapshot_ids = [t["id"] for t in snapshot.tracks if t.get("id")]
    current_ids = {t.platform_id for t in current.tracks if t.platform_id}
    wanted_ids = set(snapshot_ids)

    to_remove = [tid for tid in current_ids if tid not in wanted_ids]
    to_add = [tid for tid in snapshot_ids if tid not in current_ids]

    if to_remove:
        await adapter.remove_tracks(snapshot.playlist_id, to_remove)
    if to_add:
        await adapter.add_tracks(snapshot.playlist_id, to_add)
    return len(to_add), len(to_remove)


def snapshot_tracks(snapshot: PlaylistSnapshot) -> list[Track]:
    platform = Platform(snapshot.platform)
    return [
        Track(
            title=t.get("title") or "",
            artists=list(t.get("artists") or []),
            platform_id=t.get("id"),
            platform=platform,
        )
        for t in snapshot.tracks
    ]

"""Review workflow: inspect and correct low-confidence track mappings.

Low-confidence and AI-rescued matches are the ones most likely to be wrong
versions. This module lists them and lets the user replace a mapping's target;
a subsequent reconcile+sync applies the correction to the playlists.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from playlist_sync.core.models import Platform, Track
from playlist_sync.storage.database import SyncTrackState, TrackMapping

SessionFactory = Callable  # returns a Session context manager, or None when disabled


def load_low_confidence_mappings(
    session_factory: SessionFactory,  # type: ignore[type-arg]
    source_platform: Platform,
    target_platform: Platform,
    *,
    threshold: float = 0.75,
    limit: int = 50,
) -> list[TrackMapping]:
    """Mappings below the confidence threshold, least confident first.

    Older mapping rows predate source metadata columns; backfill display info
    from sync history where possible.
    """
    session_ctx = session_factory()
    if session_ctx is None:
        return []
    with session_ctx as session:
        session.expire_on_commit = False  # rows are returned detached after commit
        rows = (
            session.query(TrackMapping)
            .filter(TrackMapping.source_platform == source_platform.value)
            .filter(TrackMapping.target_platform == target_platform.value)
            .filter(TrackMapping.confidence < threshold)
            .order_by(TrackMapping.confidence.asc())
            .limit(limit)
            .all()
        )
        for row in rows:
            if row.source_title:
                continue
            state = (
                session.query(SyncTrackState)
                .filter(SyncTrackState.source_track_key == row.source_track_key)
                .order_by(SyncTrackState.updated_at.desc())
                .first()
            )
            if state is not None:
                row.source_title = state.source_title
                row.source_artists = state.source_artists
        session.commit()
        session.expunge_all()
        return rows


def update_mapping_target(
    session_factory: SessionFactory,  # type: ignore[type-arg]
    mapping_id: int,
    chosen: Track,
) -> bool:
    """Point a mapping at a user-chosen target track (confidence 1.0)."""
    if not chosen.platform_id:
        return False
    session_ctx = session_factory()
    if session_ctx is None:
        return False
    with session_ctx as session:
        row = session.get(TrackMapping, mapping_id)
        if row is None:
            return False
        row.target_platform_id = chosen.platform_id
        row.target_title = chosen.title
        row.target_artists = "||".join(chosen.artists)
        row.target_album = chosen.album
        row.confidence = 1.0  # user-confirmed
        row.updated_at = datetime.utcnow()
        session.commit()
        return True


def mapping_source_track(mapping: TrackMapping, source_platform: Platform) -> Track:
    """Best-effort Track for a mapping's source side (for display and re-search)."""
    return Track(
        title=mapping.source_title or mapping.source_track_key,
        artists=[a for a in (mapping.source_artists or "").split(", ") if a],
        platform=source_platform,
        platform_id=mapping.source_track_key,
    )

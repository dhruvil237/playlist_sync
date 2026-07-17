"""Reconcile a target playlist against its source: find and remove stale entries.

Sync is add-only, so a target playlist accumulates drift over time: tracks
removed from the source, wrong-version picks from older matcher generations,
and same-song double-adds where different runs matched different videos.

The keep rules, in order:
  1. Any target entry that the current match cache maps from a track still on
     the source is kept — these are the picks the current matcher stands behind.
  2. Source tracks without a mapping keep one target entry with the same
     normalized title (per-title quota), so unmapped-but-synced tracks survive.
  3. Everything else is a removal candidate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from playlist_sync.core.models import Track
from playlist_sync.platforms.base import BasePlatform
from playlist_sync.storage.database import TrackMapping, create_db
from playlist_sync.storage.snapshots import save_snapshot


def normalize_title(title: str) -> str:
    """Loose title key: strips parentheticals (incl. fullwidth), feat credits,
    dash suffixes, and punctuation."""
    text = title.lower()
    text = re.sub(r"\(.*?\)|\[.*?\]|（.*?）|\bfeat\.?.*$|-.*$", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


@dataclass
class ReconcilePlan:
    playlist_name: str
    target_playlist_id: Optional[str]
    target_total: int = 0
    kept_mapped: int = 0
    removals: list[Track] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.removals


class Reconciler:
    def __init__(
        self,
        source: BasePlatform,
        target: BasePlatform,
        session_factory: Optional[Callable] = None,  # type: ignore[type-arg]
    ) -> None:
        self.source = source
        self.target = target
        self._session_factory = session_factory or create_db()

    def _source_track_key(self, track: Track) -> str:
        return track.platform_id or f"{track.title.lower()}::{track.artist_str.lower()}"

    def _load_mappings(self, keys: list[str]) -> dict[str, str]:
        session_ctx = self._session_factory()
        if session_ctx is None or not keys:
            return {}
        with session_ctx as session:
            rows = (
                session.query(TrackMapping)
                .filter(TrackMapping.source_platform == self.source.platform.value)
                .filter(TrackMapping.target_platform == self.target.platform.value)
                .filter(TrackMapping.source_track_key.in_(keys))
                .all()
            )
            return {row.source_track_key: row.target_platform_id for row in rows}

    async def plan(
        self,
        playlist_name: str,
        *,
        source_playlist_id: Optional[str] = None,
    ) -> ReconcilePlan:
        """Compute removals without touching anything."""
        if source_playlist_id:
            source_pl = await self.source.get_playlist(source_playlist_id)
        else:
            stub = await self.source.get_playlist_by_name(playlist_name)
            if stub is None or stub.platform_id is None:
                raise RuntimeError(f"Playlist {playlist_name!r} not found on {self.source.platform.value}")
            source_pl = await self.source.get_playlist(stub.platform_id)

        target_stub = await self.target.get_playlist_by_name(playlist_name)
        if target_stub is None or target_stub.platform_id is None:
            return ReconcilePlan(playlist_name=playlist_name, target_playlist_id=None)
        target_pl = await self.target.get_playlist(target_stub.platform_id)

        source_keys = [self._source_track_key(t) for t in source_pl.tracks]
        mappings = self._load_mappings(source_keys)

        keep_ids = {mappings[key] for key in source_keys if key in mappings}
        quota: dict[str, int] = {}
        for track, key in zip(source_pl.tracks, source_keys):
            if key not in mappings:
                title_key = normalize_title(track.title)
                quota[title_key] = quota.get(title_key, 0) + 1

        plan = ReconcilePlan(
            playlist_name=playlist_name,
            target_playlist_id=target_pl.platform_id,
            target_total=len(target_pl.tracks),
        )
        for track in target_pl.tracks:
            if track.platform_id and track.platform_id in keep_ids:
                plan.kept_mapped += 1
                continue
            title_key = normalize_title(track.title)
            if quota.get(title_key, 0) > 0:
                quota[title_key] -= 1
                continue
            plan.removals.append(track)
        return plan

    async def apply(self, plan: ReconcilePlan) -> int:
        """Snapshot the target, then remove the planned entries."""
        if plan.is_empty or plan.target_playlist_id is None:
            return 0

        target_pl = await self.target.get_playlist(plan.target_playlist_id)
        save_snapshot(self._session_factory, target_pl, reason="pre_reconcile")

        removal_ids = [t.platform_id for t in plan.removals if t.platform_id]
        await self.target.remove_tracks(plan.target_playlist_id, removal_ids)
        return len(removal_ids)

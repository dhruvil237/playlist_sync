"""Sync orchestrator: coordinates matching, interactive resolution, and writing results."""
from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable, Optional

from playlist_sync.core.matcher import TrackMatcher
from playlist_sync.core.models import (
    ConflictStrategy,
    MatchResult,
    MatchStatus,
    Platform,
    Playlist,
    SyncResult,
    Track,
)
from playlist_sync.platforms.base import BasePlatform
from playlist_sync.storage.database import SyncRun, SyncTrackState, UnmatchedTrack, create_db

# Callback types for progress reporting and interactive resolution
ProgressCallback = Callable[[int, int, Track], None]
ResolveCallback = Callable[[MatchResult], Awaitable[Optional[Track]]]


class Syncer:
    ADD_BATCH_SIZE = 25
    MATCH_BATCH_MULTIPLIER = 4

    """
    Orchestrates a playlist sync between two platform adapters.

    Usage:
        syncer = Syncer(source, target)
        result = await syncer.sync_playlist(
            "My Playlist",
            dry_run=False,
            on_progress=...,
            on_resolve=...,
        )
    """

    def __init__(
        self,
        source: BasePlatform,
        target: BasePlatform,
        conflict_strategy: ConflictStrategy = ConflictStrategy.SOURCE_WINS,
        use_ai_matching: bool = True,
        confidence_threshold: float = 0.85,
        ai_model: Optional[str] = None,
        ai_base_url: Optional[str] = None,
        ai_api_key: Optional[str] = None,
        workers: int = 1,
    ) -> None:
        self.source = source
        self.target = target
        self.conflict_strategy = conflict_strategy
        self.matcher = TrackMatcher(
            use_ai=use_ai_matching,
            ai_model=ai_model,
            ai_base_url=ai_base_url,
            ai_api_key=ai_api_key,
        )
        self.confidence_threshold = confidence_threshold
        self.workers = max(1, workers)
        self._session_factory = create_db()

    async def sync_playlist(
        self,
        playlist_name: str,
        *,
        source_playlist_id: Optional[str] = None,
        dry_run: bool = False,
        on_progress: Optional[ProgressCallback] = None,
        on_resolve: Optional[ResolveCallback] = None,
    ) -> SyncResult:
        """
        Sync a named playlist from source to target.

        Args:
            playlist_name: Name of the playlist to sync.
            source_playlist_id: Override auto-lookup with a known ID.
            dry_run: If True, compute matches but do not write to target.
            on_progress: Called after each track is processed.
            on_resolve: Called for ambiguous tracks; should return the chosen Track or None to skip.
        """
        result = SyncResult(
            source_platform=self.source.platform,
            target_platform=self.target.platform,
            playlist_name=playlist_name,
            dry_run=dry_run,
        )

        # 1. Fetch source playlist
        if source_playlist_id:
            source_pl = await self.source.get_playlist(source_playlist_id)
        else:
            source_pl = await self.source.get_playlist_by_name(playlist_name)
            if source_pl is None or source_pl.platform_id is None:
                result.errors.append(f"Playlist {playlist_name!r} not found on {self.source.platform.value}")
                return result
            source_pl = await self.source.get_playlist(source_pl.platform_id)

        run_id, cached_states = self._load_or_create_run(result, len(source_pl.tracks))
        result.run_id = run_id

        # 2. Fetch or create the target playlist (a dry run must not create anything)
        if dry_run:
            target_pl = await self.target.get_playlist_by_name(playlist_name)
        else:
            target_pl = await self.target.find_or_create_playlist(
                playlist_name,
                description=f"Synced from {self.source.platform.value} by playlist-sync",
            )

        # 3. Build set of tracks already on target (by title+artist) to avoid duplicates
        if target_pl is not None and target_pl.platform_id:
            existing_target_pl = await self.target.get_playlist(target_pl.platform_id)
        else:
            existing_target_pl = Playlist(name=playlist_name, platform=self.target.platform)
        existing_keys = {
            (t.title.lower(), t.artist_str.lower()) for t in existing_target_pl.tracks
        }
        existing_target_ids = {t.platform_id for t in existing_target_pl.tracks if t.platform_id}

        # 4. Match each track
        tracks_to_add: list[str] = []
        pending_state_keys: list[str] = []
        processed_count = 0
        pending_tracks: list[tuple[int, Track, str]] = []
        for i, track in enumerate(source_pl.tracks):
            track_key = self._source_track_key(track)
            cached_state = cached_states.get(track_key)
            if cached_state is not None:
                cached_match = self._restore_match_result(track, cached_state)
                self._append_result(result, cached_match)
                matched_track = cached_match.matched_track
                matched_track_id = matched_track.platform_id if matched_track else None
                if (
                    not dry_run
                    and cached_match.status in (MatchStatus.MATCHED, MatchStatus.MANUAL_OVERRIDE)
                    and matched_track_id
                    and not cached_state.applied
                    and matched_track_id not in existing_target_ids
                ):
                    tracks_to_add.append(matched_track_id)
                    pending_state_keys.append(track_key)
                elif matched_track_id:
                    existing_target_ids.add(matched_track_id)

                if not dry_run and len(tracks_to_add) >= self.ADD_BATCH_SIZE:
                    await self._flush_pending_tracks(
                        target_pl.platform_id,  # type: ignore[union-attr, arg-type]
                        tracks_to_add,
                        pending_state_keys,
                        existing_target_ids,
                        run_id,
                    )
                    tracks_to_add = []
                    pending_state_keys = []
                processed_count += 1
                if on_progress:
                    on_progress(processed_count, len(source_pl.tracks), track)
                continue

            # Skip already-present tracks
            if (track.title.lower(), track.artist_str.lower()) in existing_keys:
                skipped_match = MatchResult(source_track=track, matched_track=None, status=MatchStatus.SKIPPED)
                result.skipped.append(skipped_match)
                self._persist_track_state(run_id, i, skipped_match, applied=True)
                processed_count += 1
                if on_progress:
                    on_progress(processed_count, len(source_pl.tracks), track)
                continue

            pending_tracks.append((i, track, track_key))

        match_batch_size = max(self.ADD_BATCH_SIZE, self.workers * self.MATCH_BATCH_MULTIPLIER)
        for batch_start in range(0, len(pending_tracks), match_batch_size):
            batch = pending_tracks[batch_start:batch_start + match_batch_size]
            batch_tracks = [track for _, track, _ in batch]
            if self.workers > 1:
                batch_matches = await self.matcher.match_many(batch_tracks, self.target, workers=self.workers)
            else:
                batch_matches = [await self.matcher.match(track, self.target) for track in batch_tracks]

            for (i, track, track_key), match in zip(batch, batch_matches):
                if match.status == MatchStatus.MATCHED:
                    matched_id = match.matched_track.platform_id if match.matched_track else None
                    if matched_id and matched_id in existing_target_ids:
                        # The matched video is already on the target (under different
                        # metadata than the title+artist dedup could catch).
                        match.status = MatchStatus.SKIPPED
                        result.skipped.append(match)
                        self._persist_track_state(run_id, i, match, applied=True)
                    else:
                        result.matched.append(match)
                        if matched_id:
                            existing_target_ids.add(matched_id)  # also dedups within this run
                            tracks_to_add.append(matched_id)
                            pending_state_keys.append(track_key)
                        self._persist_track_state(run_id, i, match)

                elif match.status == MatchStatus.AMBIGUOUS:
                    if on_resolve:
                        chosen = await on_resolve(match)
                        if chosen and chosen.platform_id and chosen.platform_id in existing_target_ids:
                            match.matched_track = chosen
                            match.status = MatchStatus.SKIPPED
                            result.skipped.append(match)
                            self._persist_track_state(run_id, i, match, applied=True)
                        elif chosen and chosen.platform_id:
                            match.matched_track = chosen
                            match.status = MatchStatus.MANUAL_OVERRIDE
                            result.matched.append(match)
                            existing_target_ids.add(chosen.platform_id)
                            tracks_to_add.append(chosen.platform_id)
                            pending_state_keys.append(track_key)
                            self._persist_track_state(run_id, i, match)
                        else:
                            result.skipped.append(match)
                            self._persist_track_state(run_id, i, match, applied=True)
                    else:
                        result.ambiguous.append(match)
                        self._persist_track_state(run_id, i, match)

                else:  # NOT_FOUND
                    result.not_found.append(match)
                    self._persist_track_state(run_id, i, match, applied=True)

                if not dry_run and len(tracks_to_add) >= self.ADD_BATCH_SIZE:
                    await self._flush_pending_tracks(
                        target_pl.platform_id,  # type: ignore[union-attr, arg-type]
                        tracks_to_add,
                        pending_state_keys,
                        existing_target_ids,
                        run_id,
                    )
                    tracks_to_add = []
                    pending_state_keys = []

                processed_count += 1
                if on_progress:
                    on_progress(processed_count, len(source_pl.tracks), track)

        # 5. Write to target (unless dry run)
        if not dry_run and tracks_to_add:
            await self._flush_pending_tracks(
                target_pl.platform_id,  # type: ignore[arg-type]
                tracks_to_add,
                pending_state_keys,
                existing_target_ids,
                run_id,
            )

        result.finished_at = datetime.utcnow()
        self._persist_result(result)
        return result

    async def sync_liked_songs(
        self,
        *,
        dry_run: bool = False,
        on_progress: Optional[ProgressCallback] = None,
        on_resolve: Optional[ResolveCallback] = None,
    ) -> SyncResult:
        """Sync liked/saved songs from source to target library."""
        result = SyncResult(
            source_platform=self.source.platform,
            target_platform=self.target.platform,
            playlist_name="[Liked Songs]",
            dry_run=dry_run,
        )

        liked = await self.source.get_liked_songs()
        tracks_to_like: list[str] = []

        for i, track in enumerate(liked):
            if on_progress:
                on_progress(i + 1, len(liked), track)

            match = await self.matcher.match(track, self.target)

            if match.status == MatchStatus.MATCHED:
                result.matched.append(match)
                if match.matched_track and match.matched_track.platform_id:
                    tracks_to_like.append(match.matched_track.platform_id)
            elif match.status == MatchStatus.AMBIGUOUS and on_resolve:
                chosen = await on_resolve(match)
                if chosen and chosen.platform_id:
                    match.matched_track = chosen
                    match.status = MatchStatus.MANUAL_OVERRIDE
                    result.matched.append(match)
                    tracks_to_like.append(chosen.platform_id)
                else:
                    result.skipped.append(match)
            elif match.status == MatchStatus.AMBIGUOUS:
                result.ambiguous.append(match)
            else:
                result.not_found.append(match)

        if not dry_run and tracks_to_like:
            await self.target.like_tracks(tracks_to_like)

        result.finished_at = datetime.utcnow()
        self._persist_result(result)
        return result

    def _persist_result(self, result: SyncResult) -> None:
        session_ctx = self._session_factory()
        if session_ctx is None:
            return

        with session_ctx as session:
            run: SyncRun | None = None
            if result.run_id is not None:
                run = session.get(SyncRun, result.run_id)

            if run is None:
                run = SyncRun(
                    source_platform=result.source_platform.value,
                    target_platform=result.target_platform.value,
                    playlist_name=result.playlist_name,
                    dry_run=result.dry_run,
                    started_at=result.started_at,
                )
                session.add(run)
                session.flush()
                result.run_id = run.id

            run.total = result.total
            run.matched = len(result.matched)
            run.ambiguous = len(result.ambiguous)
            run.not_found = len(result.not_found)
            run.skipped = len(result.skipped)
            run.success_rate = result.success_rate
            run.errors = result.errors if result.errors else None
            run.finished_at = result.finished_at

            for mr in result.not_found + result.ambiguous + result.skipped:
                session.add(UnmatchedTrack(
                    sync_run_id=run.id,
                    title=mr.source_track.title,
                    artists=mr.source_track.artist_str,
                    album=mr.source_track.album,
                    platform=mr.source_track.platform.value if mr.source_track.platform else "",
                    status=mr.status.value,
                    confidence=mr.confidence,
                ))

            session.commit()

    def _source_track_key(self, track: Track) -> str:
        return track.platform_id or f"{track.title.lower()}::{track.artist_str.lower()}"

    def _load_or_create_run(self, result: SyncResult, total_tracks: int) -> tuple[Optional[int], dict[str, SyncTrackState]]:
        session_ctx = self._session_factory()
        if session_ctx is None:
            return None, {}

        with session_ctx as session:
            run = (
                session.query(SyncRun)
                .filter(SyncRun.source_platform == result.source_platform.value)
                .filter(SyncRun.target_platform == result.target_platform.value)
                .filter(SyncRun.playlist_name == result.playlist_name)
                .filter(SyncRun.dry_run == result.dry_run)
                .filter(SyncRun.finished_at.is_(None))
                .order_by(SyncRun.started_at.desc())
                .first()
            )

            if run is None:
                run = SyncRun(
                    source_platform=result.source_platform.value,
                    target_platform=result.target_platform.value,
                    playlist_name=result.playlist_name,
                    dry_run=result.dry_run,
                    started_at=result.started_at,
                    total=total_tracks,
                )
                session.add(run)
                session.flush()
                session.commit()
                return run.id, {}

            run.total = total_tracks
            session.commit()
            states = session.query(SyncTrackState).filter(SyncTrackState.sync_run_id == run.id).all()
            return run.id, {state.source_track_key: state for state in states}

    def _restore_match_result(self, source_track: Track, state: SyncTrackState) -> MatchResult:
        matched_track: Optional[Track] = None
        if state.matched_track_platform_id and state.matched_track_title and state.matched_track_artists:
            matched_track = Track(
                title=state.matched_track_title,
                artists=[artist for artist in state.matched_track_artists.split("||") if artist],
                album=state.matched_track_album,
                platform_id=state.matched_track_platform_id,
                platform=self.target.platform,
            )

        return MatchResult(
            source_track=source_track,
            matched_track=matched_track,
            status=MatchStatus(state.status),
            confidence=state.confidence,
        )

    def _append_result(self, result: SyncResult, match: MatchResult) -> None:
        if match.status in (MatchStatus.MATCHED, MatchStatus.MANUAL_OVERRIDE):
            result.matched.append(match)
        elif match.status == MatchStatus.AMBIGUOUS:
            result.ambiguous.append(match)
        elif match.status == MatchStatus.NOT_FOUND:
            result.not_found.append(match)
        else:
            result.skipped.append(match)

    def _persist_track_state(
        self,
        run_id: Optional[int],
        source_index: int,
        match: MatchResult,
        *,
        applied: bool = False,
    ) -> None:
        if run_id is None:
            return

        session_ctx = self._session_factory()
        if session_ctx is None:
            return

        source_track = match.source_track
        source_key = self._source_track_key(source_track)
        matched_track = match.matched_track
        matched_artists = "||".join(matched_track.artists) if matched_track is not None else None

        with session_ctx as session:
            state = (
                session.query(SyncTrackState)
                .filter(SyncTrackState.sync_run_id == run_id)
                .filter(SyncTrackState.source_track_key == source_key)
                .first()
            )
            if state is None:
                state = SyncTrackState(sync_run_id=run_id, source_track_key=source_key)
                session.add(state)

            state.source_track_platform_id = source_track.platform_id
            state.source_index = source_index
            state.source_title = source_track.title
            state.source_artists = source_track.artist_str
            state.source_album = source_track.album
            state.status = match.status.value
            state.confidence = match.confidence
            state.matched_track_platform_id = matched_track.platform_id if matched_track is not None else None
            state.matched_track_title = matched_track.title if matched_track is not None else None
            state.matched_track_artists = matched_artists
            state.matched_track_album = matched_track.album if matched_track is not None else None
            state.applied = applied
            state.updated_at = datetime.utcnow()
            session.commit()

    async def _flush_pending_tracks(
        self,
        playlist_id: str,
        track_ids: list[str],
        source_keys: list[str],
        existing_target_ids: set[str | None],
        run_id: Optional[int],
    ) -> None:
        await self.target.add_tracks(playlist_id, track_ids)
        existing_target_ids.update(track_ids)
        self._mark_track_states_applied(run_id, source_keys)

    def _mark_track_states_applied(self, run_id: Optional[int], source_keys: list[str]) -> None:
        if run_id is None or not source_keys:
            return

        session_ctx = self._session_factory()
        if session_ctx is None:
            return

        with session_ctx as session:
            (
                session.query(SyncTrackState)
                .filter(SyncTrackState.sync_run_id == run_id)
                .filter(SyncTrackState.source_track_key.in_(source_keys))
                .update(
                    {
                        SyncTrackState.applied: True,
                        SyncTrackState.updated_at: datetime.utcnow(),
                    },
                    synchronize_session=False,
                )
            )
            session.commit()

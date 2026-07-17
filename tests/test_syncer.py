"""Tests for sync orchestration behavior."""
from __future__ import annotations

import pytest

from playlist_sync.core.models import MatchResult, MatchStatus, Platform, Playlist, Track
from playlist_sync.core.syncer import Syncer
from playlist_sync.platforms.base import BasePlatform
from playlist_sync.storage.database import create_db


class FakePlatform(BasePlatform):
    def __init__(self, platform: Platform, playlists: list[Playlist] | None = None, *, fail_on_add: bool = False) -> None:
        self.platform = platform
        self._playlists = playlists or []
        self.added_track_ids: list[str] = []
        self.fail_on_add = fail_on_add

    async def authenticate(self) -> None:
        return None

    async def get_playlists(self) -> list[Playlist]:
        return self._playlists

    async def get_playlist(self, playlist_id: str) -> Playlist:
        for playlist in self._playlists:
            if playlist.platform_id == playlist_id:
                return playlist
        raise AssertionError(f"Unknown playlist id: {playlist_id}")

    async def search_track(self, query: str, limit: int = 5) -> list[Track]:
        return []

    async def create_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        playlist = Playlist(name=name, platform_id=f"new-{name}", platform=self.platform)
        self._playlists.append(playlist)
        return playlist

    async def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        if self.fail_on_add:
            raise RuntimeError("simulated add failure")
        self.added_track_ids.extend(track_ids)

    async def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        return None

    async def get_liked_songs(self) -> list[Track]:
        return []

    async def like_tracks(self, track_ids: list[str]) -> None:
        return None


class StubMatcher:
    def __init__(self, result: MatchResult) -> None:
        self.result = result

    async def match(self, source: Track, target_platform: BasePlatform) -> MatchResult:
        return self.result


class MappingMatcher:
    def __init__(self, mapping: dict[str, Track], *, fail_if_called: bool = False) -> None:
        self.mapping = mapping
        self.fail_if_called = fail_if_called
        self.calls = 0

    async def match(self, source: Track, target_platform: BasePlatform) -> MatchResult:
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("matcher should not be called when resuming from checkpoint")

        matched_track = self.mapping[source.platform_id]
        return MatchResult(
            source_track=source,
            matched_track=matched_track,
            status=MatchStatus.MATCHED,
            confidence=0.95,
        )


class BatchMatcher:
    def __init__(self, mapping: dict[str, Track]) -> None:
        self.mapping = mapping
        self.match_calls = 0
        self.match_many_calls = 0
        self.workers_seen: list[int] = []

    async def match(self, source: Track, target_platform: BasePlatform) -> MatchResult:
        self.match_calls += 1
        matched_track = self.mapping[source.platform_id]
        return MatchResult(
            source_track=source,
            matched_track=matched_track,
            status=MatchStatus.MATCHED,
            confidence=0.95,
        )

    async def match_many(self, sources: list[Track], target_platform: BasePlatform, *, workers: int = 1) -> list[MatchResult]:
        self.match_many_calls += 1
        self.workers_seen.append(workers)
        return [await self.match(source, target_platform) for source in sources]


async def test_sync_playlist_does_not_write_ambiguous_matches_without_resolver(tmp_path) -> None:
    source_track = Track(
        title="Song",
        artists=["Artist"],
        platform=Platform.SPOTIFY,
        platform_id="src-track",
    )
    matched_track = Track(
        title="Song",
        artists=["Artist"],
        platform=Platform.YTMUSIC,
        platform_id="candidate-track",
    )
    source_playlist = Playlist(
        name="Road Trip",
        tracks=[source_track],
        platform=Platform.SPOTIFY,
        platform_id="src-playlist",
    )
    target_playlist = Playlist(
        name="Road Trip",
        tracks=[],
        platform=Platform.YTMUSIC,
        platform_id="target-playlist",
    )

    source = FakePlatform(Platform.SPOTIFY, [source_playlist])
    target = FakePlatform(Platform.YTMUSIC, [target_playlist])
    syncer = Syncer(source, target)
    syncer._session_factory = lambda: None  # type: ignore[assignment]
    syncer._persist_result = lambda result: None  # type: ignore[method-assign]
    syncer.matcher = StubMatcher(
        MatchResult(
            source_track=source_track,
            matched_track=matched_track,
            status=MatchStatus.AMBIGUOUS,
            confidence=0.7,
            candidates=[(matched_track, 0.7)],
        )
    )

    result = await syncer.sync_playlist("Road Trip")

    assert len(result.ambiguous) == 1
    assert target.added_track_ids == []


async def test_sync_playlist_resumes_from_saved_track_states(tmp_path) -> None:
    source_tracks = [
        Track(title="Song One", artists=["Artist"], platform=Platform.SPOTIFY, platform_id="src-1"),
        Track(title="Song Two", artists=["Artist"], platform=Platform.SPOTIFY, platform_id="src-2"),
    ]
    matched_tracks = {
        "src-1": Track(title="Song One", artists=["Artist"], platform=Platform.YTMUSIC, platform_id="yt-1"),
        "src-2": Track(title="Song Two", artists=["Artist"], platform=Platform.YTMUSIC, platform_id="yt-2"),
    }
    source_playlist = Playlist(
        name="Road Trip",
        tracks=source_tracks,
        platform=Platform.SPOTIFY,
        platform_id="src-playlist",
    )
    target_playlist = Playlist(
        name="Road Trip",
        tracks=[],
        platform=Platform.YTMUSIC,
        platform_id="target-playlist",
    )

    db_factory = create_db(tmp_path / "history.db")
    source = FakePlatform(Platform.SPOTIFY, [source_playlist])
    failing_target = FakePlatform(Platform.YTMUSIC, [target_playlist], fail_on_add=True)
    first_syncer = Syncer(source, failing_target)
    first_syncer._session_factory = db_factory
    first_syncer.matcher = MappingMatcher(matched_tracks)

    with pytest.raises(RuntimeError, match="simulated add failure"):
        await first_syncer.sync_playlist("Road Trip")

    retry_target = FakePlatform(Platform.YTMUSIC, [target_playlist])
    second_syncer = Syncer(source, retry_target)
    second_syncer._session_factory = db_factory
    second_syncer.matcher = MappingMatcher(matched_tracks, fail_if_called=True)

    result = await second_syncer.sync_playlist("Road Trip")

    assert retry_target.added_track_ids == ["yt-1", "yt-2"]
    assert second_syncer.matcher.calls == 0
    assert len(result.matched) == 2


async def test_sync_playlist_uses_configured_workers_for_batch_matching(tmp_path) -> None:
    source_tracks = [
        Track(title="Song One", artists=["Artist"], platform=Platform.SPOTIFY, platform_id="src-1"),
        Track(title="Song Two", artists=["Artist"], platform=Platform.SPOTIFY, platform_id="src-2"),
    ]
    matched_tracks = {
        "src-1": Track(title="Song One", artists=["Artist"], platform=Platform.YTMUSIC, platform_id="yt-1"),
        "src-2": Track(title="Song Two", artists=["Artist"], platform=Platform.YTMUSIC, platform_id="yt-2"),
    }
    source_playlist = Playlist(
        name="Road Trip",
        tracks=source_tracks,
        platform=Platform.SPOTIFY,
        platform_id="src-playlist",
    )
    target_playlist = Playlist(
        name="Road Trip",
        tracks=[],
        platform=Platform.YTMUSIC,
        platform_id="target-playlist",
    )

    source = FakePlatform(Platform.SPOTIFY, [source_playlist])
    target = FakePlatform(Platform.YTMUSIC, [target_playlist])
    syncer = Syncer(source, target, workers=4)
    syncer._session_factory = lambda: None  # type: ignore[assignment]
    syncer._persist_result = lambda result: None  # type: ignore[method-assign]
    syncer.matcher = BatchMatcher(matched_tracks)

    result = await syncer.sync_playlist("Road Trip")

    assert target.added_track_ids == ["yt-1", "yt-2"]
    assert syncer.matcher.match_many_calls == 1
    assert syncer.matcher.workers_seen == [4]
    assert len(result.matched) == 2

async def test_dry_run_never_writes_to_target(tmp_path) -> None:
    # More tracks than ADD_BATCH_SIZE so the in-loop flush path is exercised too.
    count = Syncer.ADD_BATCH_SIZE + 5
    source_tracks = [
        Track(title=f"Song {i}", artists=["Artist"], platform=Platform.SPOTIFY, platform_id=f"src-{i}")
        for i in range(count)
    ]
    mapping = {
        f"src-{i}": Track(
            title=f"Song {i}", artists=["Artist"], platform=Platform.YTMUSIC, platform_id=f"yt-{i}"
        )
        for i in range(count)
    }
    source_playlist = Playlist(
        name="Road Trip",
        tracks=source_tracks,
        platform=Platform.SPOTIFY,
        platform_id="src-playlist",
    )

    source = FakePlatform(Platform.SPOTIFY, [source_playlist])
    target = FakePlatform(Platform.YTMUSIC, [])  # playlist does not exist on target
    syncer = Syncer(source, target)
    syncer._session_factory = lambda: None  # type: ignore[assignment]
    syncer._persist_result = lambda result: None  # type: ignore[method-assign]
    syncer.matcher = MappingMatcher(mapping)

    result = await syncer.sync_playlist("Road Trip", dry_run=True)

    assert len(result.matched) == count
    assert target.added_track_ids == []          # no tracks written
    assert target._playlists == []               # no playlist created


async def test_matched_track_already_on_target_by_id_is_skipped(tmp_path) -> None:
    source_track = Track(title="Song", artists=["Artist"], platform=Platform.SPOTIFY, platform_id="src-1")
    # Already on target under different metadata, so title+artist dedup misses it.
    already_there = Track(title="Song (Official Video)", artists=["Artist VEVO"],
                          platform=Platform.YTMUSIC, platform_id="yt-1")
    source_playlist = Playlist(name="Road Trip", tracks=[source_track],
                               platform=Platform.SPOTIFY, platform_id="src-playlist")
    target_playlist = Playlist(name="Road Trip", tracks=[already_there],
                               platform=Platform.YTMUSIC, platform_id="target-playlist")

    source = FakePlatform(Platform.SPOTIFY, [source_playlist])
    target = FakePlatform(Platform.YTMUSIC, [target_playlist])
    syncer = Syncer(source, target)
    syncer._session_factory = lambda: None  # type: ignore[assignment]
    syncer._persist_result = lambda result: None  # type: ignore[method-assign]
    # Matcher resolves the source track to the same video id that is already present.
    syncer.matcher = MappingMatcher({"src-1": Track(
        title="Song", artists=["Artist"], platform=Platform.YTMUSIC, platform_id="yt-1")})

    result = await syncer.sync_playlist("Road Trip")

    assert len(result.skipped) == 1
    assert result.matched == []
    assert target.added_track_ids == []


async def test_match_cache_reused_across_runs_and_playlists(tmp_path) -> None:
    source_tracks = [
        Track(title="Song One", artists=["Artist"], platform=Platform.SPOTIFY, platform_id="src-1"),
        Track(title="Song Two", artists=["Artist"], platform=Platform.SPOTIFY, platform_id="src-2"),
    ]
    matched_tracks = {
        "src-1": Track(title="Song One", artists=["Artist"], platform=Platform.YTMUSIC, platform_id="yt-1"),
        "src-2": Track(title="Song Two", artists=["Artist"], platform=Platform.YTMUSIC, platform_id="yt-2"),
    }
    db_factory = create_db(tmp_path / "history.db")

    first_source = FakePlatform(Platform.SPOTIFY, [Playlist(
        name="Road Trip", tracks=source_tracks, platform=Platform.SPOTIFY, platform_id="src-playlist")])
    first_target = FakePlatform(Platform.YTMUSIC, [Playlist(
        name="Road Trip", tracks=[], platform=Platform.YTMUSIC, platform_id="target-playlist")])
    first_syncer = Syncer(first_source, first_target)
    first_syncer._session_factory = db_factory
    first_syncer.matcher = MappingMatcher(matched_tracks)
    await first_syncer.sync_playlist("Road Trip")
    assert first_syncer.matcher.calls == 2

    # A DIFFERENT playlist containing the same tracks: matches come from the
    # global cache, so the matcher must never be called.
    second_source = FakePlatform(Platform.SPOTIFY, [Playlist(
        name="Gym Mix", tracks=source_tracks, platform=Platform.SPOTIFY, platform_id="src-playlist-2")])
    second_target = FakePlatform(Platform.YTMUSIC, [Playlist(
        name="Gym Mix", tracks=[], platform=Platform.YTMUSIC, platform_id="target-playlist-2")])
    second_syncer = Syncer(second_source, second_target)
    second_syncer._session_factory = db_factory
    second_syncer.matcher = MappingMatcher(matched_tracks, fail_if_called=True)

    result = await second_syncer.sync_playlist("Gym Mix")

    assert second_syncer.matcher.calls == 0
    assert len(result.matched) == 2
    assert second_target.added_track_ids == ["yt-1", "yt-2"]

    # And with the cache disabled, the matcher is consulted again.
    third_target = FakePlatform(Platform.YTMUSIC, [Playlist(
        name="Gym Mix", tracks=[], platform=Platform.YTMUSIC, platform_id="target-playlist-3")])
    third_syncer = Syncer(second_source, third_target, use_match_cache=False)
    third_syncer._session_factory = db_factory
    third_syncer.matcher = MappingMatcher(matched_tracks)
    await third_syncer.sync_playlist("Gym Mix")
    assert third_syncer.matcher.calls == 2

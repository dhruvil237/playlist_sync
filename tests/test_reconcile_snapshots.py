"""Tests for reconcile, snapshots, review, and honest reporting."""
from __future__ import annotations

import pytest

from playlist_sync.core.models import MatchResult, MatchStatus, Platform, Playlist, SyncResult, Track
from playlist_sync.core.reconciler import Reconciler, normalize_title
from playlist_sync.core.review import load_low_confidence_mappings, update_mapping_target
from playlist_sync.platforms.base import BasePlatform
from playlist_sync.storage.database import TrackMapping, create_db
from playlist_sync.storage.snapshots import get_snapshot, list_snapshots, restore_snapshot, save_snapshot


class FakePlatform(BasePlatform):
    def __init__(self, platform: Platform, playlists: list[Playlist]) -> None:
        self.platform = platform
        self._playlists = playlists
        self.removed: list[str] = []
        self.added: list[str] = []

    async def authenticate(self) -> None: ...
    async def get_playlists(self): return self._playlists

    async def get_playlist(self, playlist_id: str) -> Playlist:
        for pl in self._playlists:
            if pl.platform_id == playlist_id:
                return pl
        raise AssertionError(f"unknown playlist {playlist_id}")

    async def search_track(self, query: str, limit: int = 5): return []
    async def create_playlist(self, name, description="", public=False): raise NotImplementedError

    async def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        self.added.extend(track_ids)

    async def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        self.removed.extend(track_ids)
        for pl in self._playlists:
            if pl.platform_id == playlist_id:
                pl.tracks = [t for t in pl.tracks if t.platform_id not in track_ids]

    async def get_liked_songs(self): return []
    async def like_tracks(self, track_ids): ...


def sp(title: str, tid: str) -> Track:
    return Track(title=title, artists=["Artist"], platform=Platform.SPOTIFY, platform_id=tid)


def yt(title: str, tid: str) -> Track:
    return Track(title=title, artists=["Artist"], platform=Platform.YTMUSIC, platform_id=tid)


def _store_mapping(db_factory, source_key: str, target_id: str, confidence: float = 0.9) -> None:
    with db_factory() as session:
        session.add(TrackMapping(
            source_platform="spotify", source_track_key=source_key,
            source_title=f"title-{source_key}", source_artists="Artist",
            target_platform="ytmusic", target_platform_id=target_id,
            target_title="t", target_artists="Artist", confidence=confidence,
        ))
        session.commit()


async def test_reconcile_plan_keeps_mapped_and_quota_removes_rest(tmp_path) -> None:
    db_factory = create_db(tmp_path / "db.sqlite")
    source_pl = Playlist(name="Mix", platform=Platform.SPOTIFY, platform_id="sp-pl",
                         tracks=[sp("Song A", "sp-a"), sp("Song B", "sp-b")])
    target_pl = Playlist(name="Mix", platform=Platform.YTMUSIC, platform_id="yt-pl", tracks=[
        yt("Song A", "yt-a-good"),      # mapped → keep
        yt("Song A (Live)", "yt-a-old"),  # old wrong version → remove
        yt("Song B", "yt-b"),           # unmapped but title-quota → keep
        yt("Gone Song", "yt-gone"),     # no longer on source → remove
    ])
    _store_mapping(db_factory, "sp-a", "yt-a-good")

    source = FakePlatform(Platform.SPOTIFY, [source_pl])
    target = FakePlatform(Platform.YTMUSIC, [target_pl])
    plan = await Reconciler(source, target, db_factory).plan("Mix")

    removed_ids = {t.platform_id for t in plan.removals}
    assert removed_ids == {"yt-a-old", "yt-gone"}
    assert plan.kept_mapped == 1


async def test_reconcile_apply_snapshots_then_removes(tmp_path) -> None:
    db_factory = create_db(tmp_path / "db.sqlite")
    source_pl = Playlist(name="Mix", platform=Platform.SPOTIFY, platform_id="sp-pl",
                         tracks=[sp("Song A", "sp-a")])
    target_pl = Playlist(name="Mix", platform=Platform.YTMUSIC, platform_id="yt-pl",
                         tracks=[yt("Song A", "yt-a"), yt("Stale", "yt-stale")])
    _store_mapping(db_factory, "sp-a", "yt-a")

    source = FakePlatform(Platform.SPOTIFY, [source_pl])
    target = FakePlatform(Platform.YTMUSIC, [target_pl])
    reconciler = Reconciler(source, target, db_factory)
    plan = await reconciler.plan("Mix")
    removed = await reconciler.apply(plan)

    assert removed == 1
    assert target.removed == ["yt-stale"]
    snaps = list_snapshots(db_factory)
    assert len(snaps) == 1 and snaps[0].reason == "pre_reconcile"
    assert snaps[0].track_count == 2  # snapshot taken BEFORE removal


async def test_snapshot_restore_round_trip(tmp_path) -> None:
    db_factory = create_db(tmp_path / "db.sqlite")
    playlist = Playlist(name="Mix", platform=Platform.YTMUSIC, platform_id="yt-pl",
                        tracks=[yt("Keep", "yt-keep"), yt("Removed later", "yt-lost")])
    snap_id = save_snapshot(db_factory, playlist, reason="manual")
    assert snap_id is not None

    # Playlist drifts: one track lost, one foreign track added.
    playlist.tracks = [yt("Keep", "yt-keep"), yt("Intruder", "yt-intruder")]
    adapter = FakePlatform(Platform.YTMUSIC, [playlist])

    snap = get_snapshot(db_factory, snap_id)
    added, removed = await restore_snapshot(db_factory, adapter, snap)

    assert (added, removed) == (1, 1)
    assert adapter.added == ["yt-lost"]
    assert adapter.removed == ["yt-intruder"]
    # Restore itself snapshots the pre-restore state.
    reasons = {s.reason for s in list_snapshots(db_factory)}
    assert reasons == {"manual", "pre_restore"}


def test_review_load_and_remap(tmp_path) -> None:
    db_factory = create_db(tmp_path / "db.sqlite")
    _store_mapping(db_factory, "sp-low", "yt-wrong", confidence=0.6)
    _store_mapping(db_factory, "sp-high", "yt-fine", confidence=0.95)

    rows = load_low_confidence_mappings(db_factory, Platform.SPOTIFY, Platform.YTMUSIC, threshold=0.75)
    assert [r.source_track_key for r in rows] == ["sp-low"]

    corrected = yt("Right Version", "yt-right")
    assert update_mapping_target(db_factory, rows[0].id, corrected)
    with db_factory() as session:
        row = session.get(TrackMapping, rows[0].id)
        assert row.target_platform_id == "yt-right"
        assert row.confidence == 1.0


def test_success_rate_ignores_already_synced() -> None:
    def mr(status: MatchStatus) -> MatchResult:
        return MatchResult(source_track=sp("S", "x"), matched_track=None, status=status)

    result = SyncResult(source_platform=Platform.SPOTIFY, target_platform=Platform.YTMUSIC,
                        playlist_name="Mix")
    result.matched = [mr(MatchStatus.MATCHED)] * 3
    result.not_found = [mr(MatchStatus.NOT_FOUND)]
    result.skipped = [mr(MatchStatus.SKIPPED)] * 96
    assert result.success_rate == pytest.approx(0.75)  # 3 of 4 that needed matching

    fully_synced = SyncResult(source_platform=Platform.SPOTIFY, target_platform=Platform.YTMUSIC,
                              playlist_name="Mix")
    fully_synced.skipped = [mr(MatchStatus.SKIPPED)] * 10
    assert fully_synced.success_rate == 1.0

def test_normalize_title_strips_variants() -> None:
    assert normalize_title("Song (Live) [Remaster]") == "song"
    assert normalize_title("Song - 2011 Remaster") == "song"
    assert normalize_title("曲（新世紀）") == "曲"

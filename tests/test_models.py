"""Tests for core data models."""
from __future__ import annotations

from playlist_sync.core.models import MatchResult, MatchStatus, Platform, Playlist, SyncResult, Track


class TestTrack:
    def test_artist_str_joins_multiple_artists(self) -> None:
        t = Track(title="Song", artists=["Artist A", "Artist B"])
        assert t.artist_str == "Artist A, Artist B"

    def test_search_query(self) -> None:
        t = Track(title="Bohemian Rhapsody", artists=["Queen"])
        assert "Bohemian Rhapsody" in t.search_query
        assert "Queen" in t.search_query


class TestPlaylist:
    def test_repr(self) -> None:
        pl = Playlist(name="My Mix", tracks=[Track("T", ["A"]), Track("T2", ["B"])])
        assert "My Mix" in repr(pl)
        assert "2 tracks" in repr(pl)


class TestSyncResult:
    def _make_result(self) -> SyncResult:
        return SyncResult(
            source_platform=Platform.SPOTIFY,
            target_platform=Platform.YTMUSIC,
            playlist_name="Test",
        )

    def test_success_rate_empty(self) -> None:
        r = self._make_result()
        assert r.success_rate == 0.0

    def test_success_rate_calculation(self) -> None:
        r = self._make_result()
        t = Track("T", ["A"])
        r.matched = [MatchResult(source_track=t, matched_track=t, status=MatchStatus.MATCHED, confidence=0.9)]
        r.not_found = [MatchResult(source_track=t, matched_track=None, status=MatchStatus.NOT_FOUND)]
        assert r.success_rate == 0.5

    def test_total_counts_all_categories(self) -> None:
        r = self._make_result()
        t = Track("T", ["A"])
        mr = MatchResult(source_track=t, matched_track=None, status=MatchStatus.NOT_FOUND)
        r.matched = [mr]
        r.not_found = [mr, mr]
        r.skipped = [mr]
        assert r.total == 4

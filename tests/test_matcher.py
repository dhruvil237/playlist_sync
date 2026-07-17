"""Tests for the track matcher."""
from __future__ import annotations

import pytest

from playlist_sync.core.matcher import TrackMatcher, _normalize, _track_score
from playlist_sync.core.models import MatchStatus, Platform, Track


def make_track(title: str, artists: list[str], album: str | None = None, duration_ms: int | None = None) -> Track:
    return Track(title=title, artists=artists, album=album, duration_ms=duration_ms)


class TestNormalize:
    def test_lowercase(self) -> None:
        assert _normalize("Hello World") == "hello world"

    def test_strips_feat(self) -> None:
        result = _normalize("Song (feat. Artist)")
        assert "feat" not in result

    def test_strips_remaster(self) -> None:
        result = _normalize("Song - Remastered 2024")
        assert "remaster" not in result

    def test_strips_from_annotation(self) -> None:
        result = _normalize('Channa Mereya (From "Ae Dil Hai Mushkil")')
        assert result == "channa mereya"

    def test_strips_punctuation(self) -> None:
        result = _normalize("Hello, World!")
        assert "," not in result
        assert "!" not in result


class TestTrackScore:
    def test_identical_tracks_score_high(self) -> None:
        a = make_track("Bohemian Rhapsody", ["Queen"])
        b = make_track("Bohemian Rhapsody", ["Queen"])
        assert _track_score(a, b) >= 0.95

    def test_different_tracks_score_low(self) -> None:
        a = make_track("Bohemian Rhapsody", ["Queen"])
        b = make_track("Stairway to Heaven", ["Led Zeppelin"])
        # Score should be below the AMBIGUOUS_THRESHOLD (0.60) so they are classified NOT_FOUND
        assert _track_score(a, b) < 0.60

    def test_isrc_match_returns_1(self) -> None:
        a = make_track("Song", ["Artist"])
        b = make_track("Song", ["Artist"])
        a.isrc = "USRC12345678"
        b.isrc = "USRC12345678"
        assert _track_score(a, b) == 1.0

    def test_isrc_mismatch_doesnt_override(self) -> None:
        # When ISRCs differ, the result should NOT reach 1.0 via the ISRC shortcut
        # Use tracks with slightly mismatched title so natural score < 1.0
        a = make_track("Song Title", ["Artist Name"])
        b = make_track("Song Titlez", ["Artist Name"])
        a.isrc = "USRC12345678"
        b.isrc = "GBRC87654321"
        # ISRCs are different — no shortcut to 1.0; fuzzy score should be < 1.0
        assert _track_score(a, b) < 1.0

    def test_duration_penalty(self) -> None:
        a = make_track("Song", ["Artist"], duration_ms=180_000)
        b_close = make_track("Song", ["Artist"], duration_ms=182_000)
        b_far = make_track("Song", ["Artist"], duration_ms=250_000)
        assert _track_score(a, b_close) > _track_score(a, b_far)

    def test_remaster_variant_scores_high(self) -> None:
        a = make_track("Hotel California", ["Eagles"])
        b = make_track("Hotel California (2013 Remaster)", ["Eagles"])
        assert _track_score(a, b) >= 0.85

    def test_feat_variant_scores_high(self) -> None:
        a = make_track("Umbrella", ["Rihanna"])
        b = make_track("Umbrella (feat. JAY-Z)", ["Rihanna"])
        assert _track_score(a, b) >= 0.80

    def test_variant_penalty_prefers_original_over_remix(self) -> None:
        source = make_track("Tum Se Hi", ["Pritam", "Mohit Chauhan", "Irshad Kamil"])
        original = make_track("TUM SE HI", ["MOHIT CHAUHAN", "PRITAM", "IRSHAD KAMIL"])
        remix = make_track("TUM SE HI (REMIX)", ["MOHIT CHAUHAN", "PRITAM", "IRSHAD KAMIL", "DJ SUNIL"])

        assert _track_score(source, original) > _track_score(source, remix)
        assert _track_score(source, remix) < 0.85

    def test_artist_coverage_prefers_full_artist_match(self) -> None:
        source = make_track("Channa Mereya", ["Pritam", "Arijit Singh"])
        partial = make_track('Channa Mereya (From "Ae Dil Hai Mushkil")', ["Arijit Singh"])
        full = make_track('Channa Mereya (From "Ae Dil Hai Mushkil")', ["Pritam", "Arijit Singh"])

        assert _track_score(source, full) > _track_score(source, partial)


def _ambiguous_pair() -> tuple[Track, Track]:
    """A source/candidate pair whose fuzzy score lands in [0.60, 0.85)."""
    source = make_track("Tum Se Hi", ["Pritam", "Mohit Chauhan"])
    candidate = make_track("TUM SE HI (REMIX)", ["MOHIT CHAUHAN", "PRITAM", "DJ SUNIL"])
    candidate.platform_id = "yt123"
    candidate.platform = Platform.YTMUSIC
    score = _track_score(source, candidate)
    assert 0.60 <= score < 0.85, f"fixture drifted out of the ambiguous band: {score}"
    return source, candidate


class TestAmbiguousZoneAI:
    async def test_ai_consulted_across_full_ambiguous_band(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source, candidate = _ambiguous_pair()
        matcher = TrackMatcher(use_ai=True)

        async def fake_ask_ai(src: Track, candidates: list[tuple[Track, float]]) -> Track:
            return candidates[0][0]

        monkeypatch.setattr(matcher, "_ask_ai", fake_ask_ai)
        result = await matcher._match_from_candidates(source, [candidate])
        assert result.status == MatchStatus.MATCHED
        assert result.matched_track is candidate

    async def test_ambiguous_without_ai_stays_ambiguous(self) -> None:
        source, candidate = _ambiguous_pair()
        matcher = TrackMatcher(use_ai=False)
        result = await matcher._match_from_candidates(source, [candidate])
        assert result.status == MatchStatus.AMBIGUOUS

    async def test_ai_declining_falls_back_to_ambiguous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source, candidate = _ambiguous_pair()
        matcher = TrackMatcher(use_ai=True)

        async def fake_ask_ai(src: Track, candidates: list[tuple[Track, float]]) -> None:
            return None

        monkeypatch.setattr(matcher, "_ask_ai", fake_ask_ai)
        result = await matcher._match_from_candidates(source, [candidate])
        assert result.status == MatchStatus.AMBIGUOUS

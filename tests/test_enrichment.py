"""Tests for MusicBrainz enrichment and the not-found rescue pipeline."""
from __future__ import annotations

import pytest

import playlist_sync.core.enrichment as enrichment
from playlist_sync.core.matcher import TrackMatcher
from playlist_sync.core.models import MatchStatus, Platform, Track
from playlist_sync.platforms.base import BasePlatform


FAKE_MB_RESPONSE = {
    "recording-list": [
        {
            "ext:score": "100",
            "title": "カワキヲアメク",
            "artist-credit": [
                {"artist": {
                    "name": "美波",
                    "alias-list": [
                        {"alias": "373"},
                        {"alias": "Minami", "primary": "primary"},
                    ],
                }},
            ],
        },
        {
            "ext:score": "50",  # below MIN_RECORDING_SCORE — ignored
            "title": "Wrong Song",
            "artist-credit": [{"artist": {"name": "Someone"}}],
        },
    ]
}


def test_variant_pairs_substitutes_artist_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(enrichment.musicbrainzngs, "search_recordings",
                        lambda **kwargs: FAKE_MB_RESPONSE)
    pairs = enrichment._variant_pairs("カワキヲアメク", ["美波"], limit=3)
    assert ("カワキヲアメク", ["Minami"]) in pairs
    assert ("カワキヲアメク", ["373"]) in pairs
    # Primary aliases are tried before nickname-style ones.
    assert pairs.index(("カワキヲアメク", ["Minami"])) < pairs.index(("カワキヲアメク", ["373"]))
    # The original representation and low-score hits are excluded.
    assert ("カワキヲアメク", ["美波"]) not in pairs
    assert all(title != "Wrong Song" for title, _ in pairs)


def test_variant_pairs_survives_musicbrainz_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("MB down")
    monkeypatch.setattr(enrichment.musicbrainzngs, "search_recordings", _boom)
    assert enrichment._variant_pairs("Song", ["Artist"], limit=3) == []


class SearchOnlyPlatform(BasePlatform):
    platform = Platform.YTMUSIC

    def __init__(self, results: list[Track]) -> None:
        self.results = results
        self.queries: list[str] = []

    async def authenticate(self) -> None: ...
    async def get_playlists(self): return []
    async def get_playlist(self, playlist_id: str): raise NotImplementedError
    async def create_playlist(self, name, description="", public=False): raise NotImplementedError
    async def add_tracks(self, playlist_id, track_ids): ...
    async def remove_tracks(self, playlist_id, track_ids): ...
    async def get_liked_songs(self): return []
    async def like_tracks(self, track_ids): ...

    async def search_track(self, query: str, limit: int = 5) -> list[Track]:
        self.queries.append(query)
        return self.results


ROMANIZED_HIT = Track(
    title="カワキヲアメク - Kawakiwoameku", artists=["minami"],
    platform=Platform.YTMUSIC, platform_id="yt-kawaki",
)


async def test_alias_variant_rescues_cross_script_artist(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Track(title="カワキヲアメク", artists=["美波"], platform=Platform.SPOTIFY, platform_id="sp-1")
    platform = SearchOnlyPlatform([ROMANIZED_HIT])
    matcher = TrackMatcher(use_ai=False, use_musicbrainz=True)

    async def fake_variants(track: Track, **kwargs) -> list[Track]:
        return [Track(title="カワキヲアメク", artists=["Minami"])]

    monkeypatch.setattr(enrichment, "variant_tracks", fake_variants)
    result = await matcher.match(source, platform)

    assert result.status in (MatchStatus.MATCHED, MatchStatus.AMBIGUOUS)
    assert result.matched_track is ROMANIZED_HIT


async def test_ai_last_resort_rescues_romanized_title(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Track(title="廻廻奇譚", artists=["Eve"], platform=Platform.SPOTIFY, platform_id="sp-2")
    kaikai = Track(title="Kaikai Kitan", artists=["Eve"], platform=Platform.YTMUSIC, platform_id="yt-kaikai")
    platform = SearchOnlyPlatform([kaikai])
    matcher = TrackMatcher(use_ai=True, use_musicbrainz=False)

    async def fake_ask_ai(src: Track, candidates: list[tuple[Track, float]]) -> Track:
        return candidates[0][0]

    monkeypatch.setattr(matcher, "_ask_ai", fake_ask_ai)
    result = await matcher.match(source, platform)

    assert result.status == MatchStatus.MATCHED
    assert result.matched_track is kaikai


async def test_not_found_stays_not_found_without_rescuers() -> None:
    source = Track(title="廻廻奇譚", artists=["Eve"], platform=Platform.SPOTIFY, platform_id="sp-3")
    platform = SearchOnlyPlatform([Track(title="Unrelated", artists=["Nobody"],
                                         platform=Platform.YTMUSIC, platform_id="yt-x")])
    matcher = TrackMatcher(use_ai=False, use_musicbrainz=False)
    result = await matcher.match(source, platform)
    assert result.status == MatchStatus.NOT_FOUND

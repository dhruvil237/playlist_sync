"""Tests for browser-driven Spotify helpers."""
from __future__ import annotations

from playlist_sync.platforms.spotify import (
    _expected_spotify_playlist_track_count,
    _extract_playlist_id,
    _extract_playlist_id_from_labelledby,
    _extract_track_id,
    _has_spotify_auth_cookies,
    _parse_browser_playlists,
    _parse_browser_playlist_rows,
    _parse_browser_track_rows,
    _requires_spotify_login,
    _split_playlist_row_name,
    _should_open_spotify_playlists,
)


def test_extract_playlist_id_from_href() -> None:
    assert _extract_playlist_id("/playlist/37i9dQZF1DXcBWIGoYBM5M") == "37i9dQZF1DXcBWIGoYBM5M"


def test_extract_playlist_id_returns_none_for_invalid_href() -> None:
    assert _extract_playlist_id("/album/37i9dQZF1DXcBWIGoYBM5M") is None


def test_extract_playlist_id_from_labelledby() -> None:
    assert _extract_playlist_id_from_labelledby("listrow-title-spotify:playlist:2hWg5wwDdELUbTfjsXHbtE") == "2hWg5wwDdELUbTfjsXHbtE"


def test_extract_playlist_id_from_labelledby_rejects_non_playlist() -> None:
    assert _extract_playlist_id_from_labelledby("listrow-title-spotify:collection:tracks") is None


def test_extract_track_id_from_href() -> None:
    assert _extract_track_id("/track/3uL1IBFhg52VcQqOwAG01E") == "3uL1IBFhg52VcQqOwAG01E"


def test_extract_track_id_rejects_non_track_href() -> None:
    assert _extract_track_id("/album/4PmYasI57t8uJJAOt0zKud") is None


def test_requires_spotify_login_detects_logged_out_homepage() -> None:
    page_text = "Premium Support Download Sign up Log in"

    assert _requires_spotify_login(page_text, "https://open.spotify.com/") is True


def test_requires_spotify_login_accepts_logged_in_playlist_page() -> None:
    page_text = "Home Search Your Library My Playlist"

    assert _requires_spotify_login(page_text, "https://open.spotify.com/collection/playlists") is False


def test_has_spotify_auth_cookies_detects_logged_in_session() -> None:
    cookies = [{"name": "sp_t"}, {"name": "sp_dc"}]

    assert _has_spotify_auth_cookies(cookies) is True


def test_has_spotify_auth_cookies_rejects_anon_session() -> None:
    cookies = [{"name": "sp_t"}, {"name": "OptanonConsent"}]

    assert _has_spotify_auth_cookies(cookies) is False


def test_should_open_spotify_playlists_only_after_authentication() -> None:
    assert _should_open_spotify_playlists(False, "https://accounts.spotify.com/en/login") is False
    assert _should_open_spotify_playlists(True, "https://accounts.spotify.com/en/login") is True
    assert _should_open_spotify_playlists(True, "https://open.spotify.com/collection/playlists") is False


def test_parse_browser_playlists_filters_duplicates_and_invalid_entries() -> None:
    playlists = _parse_browser_playlists(
        [
            {"href": "/playlist/first123", "text": "Road Trip"},
            {"href": "/playlist/first123", "text": "Road Trip"},
            {"href": "/playlist/second456", "text": "Focus Mix"},
            {"href": "/album/not-a-playlist", "text": "Ignore"},
            {"href": "/playlist/blank789", "text": "  "},
        ]
    )

    assert [playlist.name for playlist in playlists] == ["Road Trip", "Focus Mix"]
    assert [playlist.platform_id for playlist in playlists] == ["first123", "second456"]


def test_parse_browser_playlist_rows_extracts_name_and_id() -> None:
    playlists = _parse_browser_playlist_rows(
        [
            {"labelledby": "listrow-title-spotify:collection:tracks", "text": "Liked SongsPlaylist • 528 songs"},
            {"labelledby": "listrow-title-spotify:playlist:2hWg5wwDdELUbTfjsXHbtE", "text": "Mainstream songsPlaylist • Dhruvilpatel"},
            {"labelledby": "listrow-title-spotify:playlist:78Ow5VRyXd40sDVWxj1dli", "text": "Best Genshin soundtracksPlaylist • Kristina Eklöw"},
            {"labelledby": "listrow-title-spotify:playlist:2hWg5wwDdELUbTfjsXHbtE", "text": "Mainstream songsPlaylist • Dhruvilpatel"},
        ]
    )

    assert [playlist.name for playlist in playlists] == ["Mainstream songs", "Best Genshin soundtracks"]
    assert [playlist.platform_id for playlist in playlists] == ["2hWg5wwDdELUbTfjsXHbtE", "78Ow5VRyXd40sDVWxj1dli"]


def test_split_playlist_row_name_removes_pinned_marker() -> None:
    assert _split_playlist_row_name("Liked SongsPinnedPlaylist • 528 songs") == "Liked Songs"


def test_parse_browser_track_rows_extracts_tracks() -> None:
    tracks = _parse_browser_track_rows(
        [
            {
                "track_href": "/track/3uL1IBFhg52VcQqOwAG01E",
                "title": "Mast Magan",
                "artists": ["Arijit Singh", "Chinmayi"],
                "album": "2 States",
            },
            {
                "track_href": "/album/not-a-track",
                "title": "Ignore",
                "artists": ["Artist"],
                "album": "Album",
            },
        ]
    )

    assert len(tracks) == 1
    assert tracks[0].title == "Mast Magan"
    assert tracks[0].artists == ["Arijit Singh", "Chinmayi"]
    assert tracks[0].album == "2 States"
    assert tracks[0].platform_id == "3uL1IBFhg52VcQqOwAG01E"


def test_expected_spotify_playlist_track_count_reads_header_count() -> None:
    assert _expected_spotify_playlist_track_count("Public Playlist Mainstream songs 6 saves 387 songs, over 24 hr") == 387
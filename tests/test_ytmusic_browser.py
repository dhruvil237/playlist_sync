"""Tests for YTMusic browser-session helpers."""
from __future__ import annotations

from playlist_sync.platforms.ytmusic import (
    _browser_row_eval_script,
    _expected_playlist_track_count,
    _infer_browser_track_artists,
    _extract_ytmusic_track_id,
    _parse_browser_track_rows,
    _parse_duration_ms,
    _extract_ytmusic_playlist_id,
    _parse_browser_playlists,
    _requires_ytmusic_login,
)


def test_requires_ytmusic_login_detects_signed_out_library_page() -> None:
    page_text = (
        "Sign in to create & share playlists, get personalized recommendations, and more. "
        "Looking for what you've liked? Sign in to listen to your liked tracks"
    )

    assert _requires_ytmusic_login(page_text, "https://music.youtube.com/library/playlists") is True


def test_requires_ytmusic_login_detects_google_service_login_url() -> None:
    assert _requires_ytmusic_login("Welcome", "https://accounts.google.com/ServiceLogin") is True


def test_requires_ytmusic_login_accepts_library_page_when_signed_in() -> None:
    page_text = "Home Explore Library Playlists Songs Albums Podcasts Artists"

    assert _requires_ytmusic_login(page_text, "https://music.youtube.com/library/playlists") is False


def test_extract_ytmusic_playlist_id_reads_list_param() -> None:
    assert (
        _extract_ytmusic_playlist_id("https://music.youtube.com/playlist?list=PLdtVu2M8zmBB750GR84jiHLZmpAMv_eN7")
        == "PLdtVu2M8zmBB750GR84jiHLZmpAMv_eN7"
    )


def test_extract_ytmusic_playlist_id_rejects_non_playlist_href() -> None:
    assert _extract_ytmusic_playlist_id("https://music.youtube.com/library/playlists") is None


def test_parse_browser_playlists_filters_empty_titles_and_duplicates() -> None:
    playlists = _parse_browser_playlists(
        [
            {"href": "https://music.youtube.com/playlist?list=LM", "text": ""},
            {"href": "https://music.youtube.com/playlist?list=LM", "text": "Liked Music"},
            {
                "href": "https://music.youtube.com/playlist?list=PLdtVu2M8zmBB750GR84jiHLZmpAMv_eN7",
                "text": "too many songs",
            },
            {
                "href": "https://music.youtube.com/playlist?list=PLdtVu2M8zmBB750GR84jiHLZmpAMv_eN7",
                "text": "too many songs",
            },
            {"href": "https://music.youtube.com/library/playlists", "text": "Ignore me"},
        ]
    )

    assert [playlist.name for playlist in playlists] == ["Liked Music", "too many songs"]
    assert [playlist.platform_id for playlist in playlists] == ["LM", "PLdtVu2M8zmBB750GR84jiHLZmpAMv_eN7"]


def test_extract_ytmusic_track_id_reads_watch_query_param() -> None:
    assert (
        _extract_ytmusic_track_id("https://music.youtube.com/watch?v=qkxRHUXdpVY&list=PLdtVu2M8zmBB750GR84jiHLZmpAMv_eN7")
        == "qkxRHUXdpVY"
    )


def test_extract_ytmusic_track_id_falls_back_to_podcast_path() -> None:
    assert _extract_ytmusic_track_id("https://music.youtube.com/podcast/yIrU21hoHys") == "yIrU21hoHys"


def test_parse_duration_ms_supports_minute_and_hour_formats() -> None:
    assert _parse_duration_ms("4:29") == 269000
    assert _parse_duration_ms("1:04:29") == 3869000


def test_parse_browser_track_rows_extracts_valid_tracks() -> None:
    tracks = _parse_browser_track_rows(
        [
            {
                "text": "Dil Leke Shaan & Shreya Ghoshal 3:38",
                "title": "Dil Leke",
                "title_href": "https://music.youtube.com/watch?v=9C008fKT5VM&list=PLdtVu2M8zmBB750GR84jiHLZmpAMv_eN7",
                "artists": ["Shaan", "Shreya Ghoshal"],
                "anchor_texts": ["Dil Leke", "Shaan", "Shreya Ghoshal"],
                "duration": "3:38",
            },
            {
                "text": "Tere Bin Atif Aslam Sep 14, 2018 5:06",
                "title": "Tere Bin",
                "title_href": "https://music.youtube.com/podcast/yIrU21hoHys",
                "artists": ["Atif Aslam"],
                "anchor_texts": ["Tere Bin", "Atif Aslam"],
                "duration": "5:06",
            },
            {
                "text": "Ignore Artist 4:00",
                "title": "Ignore",
                "title_href": "",
                "artists": ["Artist"],
                "anchor_texts": ["Ignore", "Artist"],
                "duration": "4:00",
            },
        ]
    )

    assert [track.title for track in tracks] == ["Dil Leke", "Tere Bin"]
    assert [track.platform_id for track in tracks] == ["9C008fKT5VM", "yIrU21hoHys"]
    assert [track.artists for track in tracks] == [["Shaan", "Shreya Ghoshal"], ["Atif Aslam"]]
    assert [track.duration_ms for track in tracks] == [218000, 306000]


def test_infer_browser_track_artists_falls_back_to_row_text() -> None:
    artists = _infer_browser_track_artists(
        "O Meri Laila - Trending Version Atif Aslam & Jyotica Tangri Trending Bollywood 1 Min Mix 1:24",
        "O Meri Laila - Trending Version",
        "1:24",
        [],
        ["O Meri Laila - Trending Version", "Trending Bollywood 1 Min Mix"],
    )

    assert artists == ["Atif Aslam", "Jyotica Tangri"]


def test_infer_browser_track_artists_strips_search_result_noise() -> None:
    artists = _infer_browser_track_artists(
        "DIL LEKE Song • SHAAN,SHREYA GHOSHAL, SAJID-WAJID, & ARUN BHAIRAV • 216M plays WANTED",
        "DIL LEKE",
        "",
        [],
        ["DIL LEKE", "WANTED"],
    )

    assert artists == ["SHAAN", "SHREYA GHOSHAL", "SAJID-WAJID", "ARUN BHAIRAV"]


def test_browser_row_eval_script_is_non_empty() -> None:
    script = _browser_row_eval_script()

    assert "primaryAnchor" in script
    assert "title_href" in script


def test_expected_playlist_track_count_reads_header_count() -> None:
    assert _expected_playlist_track_count("Public • 2025 3.8K views • 327 tracks • 24+ hours") == 327
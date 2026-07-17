"""Spotify platform adapter using spotipy."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from urllib.parse import quote, quote_plus
from typing import Any, Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from playwright.async_api import Page

from playlist_sync.core.models import Platform, Playlist, Track
from playlist_sync.platforms.base import BasePlatform
from playlist_sync.platforms.browser_session import PlaywrightPersistentSession
from playlist_sync.storage.token_store import delete_token, load_token, save_token

SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-read",
    "user-library-modify",
]

DEFAULT_REDIRECT_URI = os.environ.get(
    "SPOTIFY_REDIRECT_URI",
    f"http://127.0.0.1:{os.environ.get('DASHBOARD_PORT', '8765')}/auth/spotify/callback"
)
SPOTIFY_PLAYLISTS_URL = "https://open.spotify.com/collection/playlists"
SPOTIFY_LIKED_SONGS_URL = "https://open.spotify.com/collection/tracks"
SPOTIFY_LOGIN_URL = "https://accounts.spotify.com/en/login?continue=" + quote(SPOTIFY_PLAYLISTS_URL, safe="")
SPOTIFY_AUTH_COOKIE_NAMES = {"sp_dc", "sp_key"}
SPOTIFY_TRACK_SEARCH_INPUT_PLACEHOLDER = "Search for songs or episodes"


def _extract_playlist_id(href: str) -> str | None:
    match = re.search(r"/playlist/([A-Za-z0-9]+)", href)
    if match is None:
        return None
    return match.group(1)


def _extract_playlist_id_from_labelledby(value: str) -> str | None:
    match = re.search(r"spotify:playlist:([A-Za-z0-9]+)", value)
    if match is None:
        return None
    return match.group(1)


def _extract_track_id(href: str) -> str | None:
    match = re.search(r"/track/([A-Za-z0-9]+)", href)
    if match is None:
        return None
    return match.group(1)


def _requires_spotify_login(page_text: str, current_url: str) -> bool:
    lower_text = page_text.lower()
    return (
        "open.spotify.com/login" in current_url
        or "accounts.spotify.com" in current_url
        or ("log in" in lower_text and "sign up" in lower_text)
    )


def _has_spotify_auth_cookies(cookies: list[dict[str, Any]]) -> bool:
    return any(cookie.get("name") in SPOTIFY_AUTH_COOKIE_NAMES for cookie in cookies)


def _should_open_spotify_playlists(has_auth_cookies: bool, current_url: str) -> bool:
    return has_auth_cookies and current_url != SPOTIFY_PLAYLISTS_URL


def _parse_browser_playlists(items: list[dict[str, str]]) -> list[Playlist]:
    seen_ids: set[str] = set()
    playlists: list[Playlist] = []
    for item in items:
        href = item.get("href", "")
        playlist_id = _extract_playlist_id(href)
        name = item.get("text", "").strip()
        if not playlist_id or not name or playlist_id in seen_ids:
            continue
        seen_ids.add(playlist_id)
        playlists.append(
            Playlist(
                name=name,
                platform_id=playlist_id,
                platform=Platform.SPOTIFY,
            )
        )
    return playlists


def _parse_browser_playlist_rows(items: list[dict[str, str]]) -> list[Playlist]:
    seen_ids: set[str] = set()
    playlists: list[Playlist] = []
    for item in items:
        labelledby = item.get("labelledby", "")
        playlist_id = _extract_playlist_id_from_labelledby(labelledby)
        row_text = item.get("text", "").strip()
        if playlist_id is None or not row_text or playlist_id in seen_ids:
            continue

        name = _split_playlist_row_name(row_text)
        if not name:
            continue

        seen_ids.add(playlist_id)
        playlists.append(
            Playlist(
                name=name,
                platform_id=playlist_id,
                platform=Platform.SPOTIFY,
            )
        )

    return playlists


def _split_playlist_row_name(row_text: str) -> str:
    return row_text.split("Playlist •", 1)[0].replace("Pinned", "").strip()


def _parse_browser_track_rows(items: list[dict[str, Any]]) -> list[Track]:
    tracks: list[Track] = []
    for item in items:
        track_id = _extract_track_id(item.get("track_href", ""))
        title = item.get("title", "").strip()
        artists = [artist.strip() for artist in item.get("artists", []) if artist.strip()]
        if track_id is None or not title or not artists:
            continue

        tracks.append(
            Track(
                title=title,
                artists=artists,
                album=(item.get("album") or "").strip() or None,
                platform_id=track_id,
                platform=Platform.SPOTIFY,
            )
        )

    return tracks


def _expected_spotify_playlist_track_count(page_text: str) -> int | None:
    match = re.search(r"(\d+)\s+songs\b", page_text, re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


def _spotify_playlist_edit_label(name: str) -> str:
    return f"{name} – Edit details"


def parse_spotify_curl(curl: str) -> dict:  # type: ignore[type-arg]
    """Extract a Bearer access token from a 'Copy as cURL (bash)' snippet."""
    import re
    m = re.search(r"""-H\s+['"]authorization:\s+Bearer\s+([^'"]+)['"]""", curl, re.IGNORECASE)
    if not m:
        raise ValueError(
            "No 'Authorization: Bearer ...' header found. "
            "Make sure you copied a request to api.spotify.com, not a static asset."
        )
    token = m.group(1).strip()
    expires_at = time.time() + 3600
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        if "exp" in payload:
            expires_at = float(payload["exp"])
    except Exception:
        pass
    return {"access_token": token, "expires_at": expires_at}


class BearerAuthManager:
    """Minimal spotipy auth manager that returns a fixed Bearer token."""

    def __init__(self, token_info: dict) -> None:  # type: ignore[type-arg]
        self._token_info = token_info

    def get_access_token(self, as_dict: bool = True):  # type: ignore[no-untyped-def]
        if time.time() > self._token_info.get("expires_at", 0) - 30:
            raise RuntimeError(
                "Spotify token expired — please reconnect via the Connections page."
            )
        return self._token_info if as_dict else self._token_info["access_token"]

    def is_token_expired(self, token_info: dict) -> bool:  # type: ignore[type-arg]
        return time.time() > token_info.get("expires_at", 0) - 30


def validate_bearer_token(token_info: dict) -> None:  # type: ignore[type-arg]
    """Fail fast for bearer tokens that are expired or immediately rate limited."""
    auth_manager = BearerAuthManager(token_info)
    auth_manager.get_access_token()
    client = spotipy.Spotify(auth_manager=auth_manager, retries=0, requests_timeout=10)
    try:
        client.current_user()
    except spotipy.SpotifyException as exc:
        if exc.http_status == 429:
            raise RuntimeError(
                "Spotify's copied web-player token is being rate limited. "
                "Use Spotify OAuth from the Connections page instead of the cURL fallback."
            ) from exc
        raise RuntimeError(str(exc)) from exc


def _raise_spotify_runtime_error(exc: spotipy.SpotifyException) -> None:
    if exc.http_status == 429:
        if load_token("spotify_spdc") and not load_token("spotify"):
            raise RuntimeError(
                "Spotify's copied web-player token is being rate limited. "
                "Reconnect with Spotify OAuth from the Connections page."
            ) from exc
        raise RuntimeError("Spotify API rate limited this request. Please retry shortly.") from exc
    if exc.http_status == 403 and "Active premium subscription required" in str(exc):
        raise RuntimeError(
            "Spotify OAuth is connected, but Spotify rejected the app credentials: "
            "the owner of the Spotify app configured in .env needs an active Premium subscription. "
            "This is an account/app setup issue, not a playlist-sync code issue."
        ) from exc
    raise RuntimeError(str(exc)) from exc


def has_usable_saved_auth() -> bool:
    """Return whether Spotify has locally usable credentials.

    Expired bearer tokens are cleared eagerly so the rest of the app stops
    reporting them as connected.
    """
    token_data = load_token("spotify_spdc")
    if token_data and token_data.get("access_token"):
        try:
            BearerAuthManager(token_data).get_access_token()
            return True
        except RuntimeError:
            delete_token("spotify_spdc")

    return load_token("spotify") is not None


class SpotifyPlatform(BasePlatform):
    platform = Platform.SPOTIFY

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ) -> None:
        self.client_id = client_id or os.environ.get("SPOTIFY_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        self.redirect_uri = redirect_uri or os.environ.get("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI)
        self._client: Optional[spotipy.Spotify] = None
        self._browser_authenticated = False

    async def _check_browser_auth(self, *, interactive: bool) -> bool:
        async with PlaywrightPersistentSession(Platform.SPOTIFY, headless=not interactive) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            target_url = SPOTIFY_LOGIN_URL if interactive else SPOTIFY_PLAYLISTS_URL
            await page.goto(target_url, wait_until="domcontentloaded")

            if interactive:
                print("Spotify browser login required.")
                print("A browser window has been opened for the playlist-sync Spotify profile.")
                print("Log in there, then wait for this command to continue automatically.")
                await page.wait_for_timeout(2_000)
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    page_text = await page.locator("body").inner_text()
                    cookies = await context.cookies(["https://open.spotify.com", "https://accounts.spotify.com"])
                    has_auth_cookies = _has_spotify_auth_cookies(cookies)
                    if _should_open_spotify_playlists(has_auth_cookies, page.url):
                        await page.goto(SPOTIFY_PLAYLISTS_URL, wait_until="domcontentloaded")
                        await page.wait_for_timeout(2_000)
                        page_text = await page.locator("body").inner_text()

                    if has_auth_cookies and not _requires_spotify_login(page_text, page.url):
                        return True

                    await page.wait_for_timeout(2_000)
                return False

            await page.wait_for_timeout(3_000)
            page_text = await page.locator("body").inner_text()
            cookies = await context.cookies(["https://open.spotify.com", "https://accounts.spotify.com"])
            return _has_spotify_auth_cookies(cookies) and not _requires_spotify_login(page_text, page.url)

    async def _fetch_browser_playlists(self) -> list[Playlist]:
        async with PlaywrightPersistentSession(Platform.SPOTIFY, headless=True) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(SPOTIFY_PLAYLISTS_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3_000)
            page_text = await page.locator("body").inner_text()
            cookies = await context.cookies(["https://open.spotify.com", "https://accounts.spotify.com"])
            if _requires_spotify_login(page_text, page.url) or not _has_spotify_auth_cookies(cookies):
                raise RuntimeError(
                    "Spotify browser automation is enabled, but the playlist-sync Spotify browser profile is not logged in. "
                    "Run a Spotify command again and log in in the opened browser window when prompted."
                )
            items = await page.locator('[aria-label="Your Library"] [role="row"] [role="group"]').evaluate_all(
                "els => els.map(el => ({labelledby: el.getAttribute('aria-labelledby') || '', text: (el.textContent || '').trim()}))"
            )
            return _parse_browser_playlist_rows(items)

    async def _fetch_browser_playlist(self, playlist_id: str) -> Playlist:
        async with PlaywrightPersistentSession(Platform.SPOTIFY, headless=True) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"https://open.spotify.com/playlist/{playlist_id}", wait_until="domcontentloaded")
            await page.wait_for_timeout(5_000)
            page_text = await page.locator("body").inner_text()
            cookies = await context.cookies(["https://open.spotify.com", "https://accounts.spotify.com"])
            if _requires_spotify_login(page_text, page.url) or not _has_spotify_auth_cookies(cookies):
                raise RuntimeError(
                    "Spotify browser automation is enabled, but the playlist-sync Spotify browser profile is not logged in. "
                    "Run a Spotify command again and log in in the opened browser window when prompted."
                )

            search_box = page.get_by_placeholder(SPOTIFY_TRACK_SEARCH_INPUT_PLACEHOLDER)
            if await search_box.count():
                if await search_box.first.input_value():
                    await search_box.first.fill("")
                    await page.wait_for_timeout(1_500)

            await page.locator("main h1").first.wait_for(timeout=15_000)
            title = (await page.locator("main h1").first.inner_text()).strip()
            metadata_candidates = await page.locator("main span, main div").evaluate_all(
                "els => els.map(el => (el.textContent || '').replace(/\\s+/g, ' ').trim())"
                ".filter(Boolean)"
                ".filter(text => /\\d+\\s+songs\\b/i.test(text))"
                ".slice(0, 10)"
            )
            track_rows = await self._collect_browser_playlist_track_rows(
                page,
                _expected_spotify_playlist_track_count(" ".join(str(text) for text in metadata_candidates)),
            )

            return Playlist(
                name=title,
                platform_id=playlist_id,
                platform=Platform.SPOTIFY,
                tracks=_parse_browser_track_rows(track_rows),
            )

    async def _collect_browser_playlist_track_rows(
        self,
        page: Page,
        expected_count: int | None = None,
    ) -> list[dict[str, str]]:
        row_locator = page.locator('[data-testid="playlist-tracklist"] [data-testid="tracklist-row"]')
        row_eval_script = (
            "els => els.map(el => ({"
            "row_number: (el.querySelector('[aria-colindex=\"1\"] span')?.textContent || '').trim(),"
            "track_href: el.querySelector('[data-testid=\"internal-track-link\"]')?.getAttribute('href') || '',"
            "title: el.querySelector('[data-testid=\"internal-track-link\"]')?.textContent || '',"
            "artists: Array.from(el.querySelectorAll('a[href^=\"/artist/\"]')).map(a => a.textContent || ''),"
            "album: el.querySelector('a[href^=\"/album/\"]')?.textContent || ''"
            "}))"
        )

        seen_rows: dict[str, dict[str, str]] = {}

        viewport = await page.locator("main").bounding_box()
        if viewport is None:
            return list(seen_rows.values())

        async def _harvest() -> None:
            for row in await row_locator.evaluate_all(row_eval_script):
                row_number = str(row.get("row_number", "")).strip()
                track_href = str(row.get("track_href", "")).strip()
                title = str(row.get("title", "")).strip()
                if not track_href or not title:
                    continue
                key = row_number or f"{track_href}|{title}"
                seen_rows.setdefault(key, row)

        def _complete() -> bool:
            return expected_count is not None and len(seen_rows) >= expected_count

        async def _hover_list() -> None:
            await page.mouse.move(
                viewport["x"] + viewport["width"] / 2,
                viewport["y"] + min(250, max(100, viewport["height"] - 40)),
            )

        # The tracklist is virtualized and fast scrolling skips rows, so a single
        # sweep is lossy. Sweep top-to-bottom in small steps, and when the expected
        # count wasn't reached, jump back to the top and sweep again.
        for sweep in range(3):
            if sweep:
                await _hover_list()
                await page.mouse.wheel(0, -1_000_000)
                await page.wait_for_timeout(1_000)

            stable_iterations = 0
            for _ in range(40):
                await _harvest()
                if _complete():
                    return list(seen_rows.values())

                before_count = len(seen_rows)
                await _hover_list()
                await page.mouse.wheel(0, 1_200)
                await page.wait_for_timeout(600)
                await _harvest()

                if len(seen_rows) == before_count:
                    stable_iterations += 1
                    if stable_iterations >= 5:
                        break
                else:
                    stable_iterations = 0

            if _complete() or expected_count is None:
                break

        return list(seen_rows.values())

    async def _fetch_browser_search_tracks(self, query: str, limit: int = 5) -> list[Track]:
        async with PlaywrightPersistentSession(Platform.SPOTIFY, headless=True) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            search_url = f"https://open.spotify.com/search/{quote_plus(query)}/tracks"
            await page.goto(search_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(5_000)
            page_text = await page.locator("body").inner_text()
            cookies = await context.cookies(["https://open.spotify.com", "https://accounts.spotify.com"])
            if _requires_spotify_login(page_text, page.url) or not _has_spotify_auth_cookies(cookies):
                raise RuntimeError(
                    "Spotify browser automation is enabled, but the playlist-sync Spotify browser profile is not logged in. "
                    "Run a Spotify command again and log in in the opened browser window when prompted."
                )

            track_rows = await page.locator('main [data-testid="tracklist-row"]').evaluate_all(
                f'''els => els.slice(0, {limit}).map(el => ({{
                    track_href: el.querySelector('a[href^="/track/"]')?.getAttribute('href') || '',
                    title: el.querySelector('a[href^="/track/"]')?.textContent || '',
                    artists: Array.from(el.querySelectorAll('a[href^="/artist/"]')).map(a => a.textContent || ''),
                    album: el.querySelector('a[href^="/album/"]')?.textContent || ''
                }}))'''
            )

            return _parse_browser_track_rows(track_rows)

    async def _fetch_browser_liked_songs(self) -> list[Track]:
        async with PlaywrightPersistentSession(Platform.SPOTIFY, headless=True) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(SPOTIFY_LIKED_SONGS_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(5_000)
            page_text = await page.locator("body").inner_text()
            cookies = await context.cookies(["https://open.spotify.com", "https://accounts.spotify.com"])
            if _requires_spotify_login(page_text, page.url) or not _has_spotify_auth_cookies(cookies):
                raise RuntimeError(
                    "Spotify browser automation is enabled, but the playlist-sync Spotify browser profile is not logged in. "
                    "Run a Spotify command again and log in in the opened browser window when prompted."
                )

            track_rows = await page.locator('[data-testid="track-list"] [data-testid="tracklist-row"]').evaluate_all(
                "els => els.map(el => ({"
                "track_href: el.querySelector('[data-testid=\"internal-track-link\"]')?.getAttribute('href') || '',"
                "title: el.querySelector('[data-testid=\"internal-track-link\"]')?.textContent || '',"
                "artists: Array.from(el.querySelectorAll('a[href^=\"/artist/\"]')).map(a => a.textContent || ''),"
                "album: el.querySelector('a[href^=\"/album/\"]')?.textContent || ''"
                "}))"
            )

            return _parse_browser_track_rows(track_rows)

    async def _open_browser_page(self, url: str, *, wait_ms: int = 5_000) -> tuple[Any, Page]:
        context_manager = PlaywrightPersistentSession(Platform.SPOTIFY, headless=True)
        context = await context_manager.__aenter__()
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(wait_ms)
            page_text = await page.locator("body").inner_text()
            cookies = await context.cookies(["https://open.spotify.com", "https://accounts.spotify.com"])
            if _requires_spotify_login(page_text, page.url) or not _has_spotify_auth_cookies(cookies):
                raise RuntimeError(
                    "Spotify browser automation is enabled, but the playlist-sync Spotify browser profile is not logged in. "
                    "Run a Spotify command again and log in in the opened browser window when prompted."
                )
            return context_manager, page
        except Exception:
            await context_manager.__aexit__(None, None, None)
            raise

    async def _create_browser_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        context_manager, page = await self._open_browser_page(SPOTIFY_PLAYLISTS_URL, wait_ms=4_000)
        try:
            await page.get_by_role("button", name="Create").click()
            await page.get_by_role("menuitem", name="Playlist").click()
            await page.wait_for_timeout(5_000)

            current_name = (await page.locator("main h1").first.inner_text()).strip()
            await page.get_by_role("button", name=_spotify_playlist_edit_label(current_name)).click()
            await page.wait_for_timeout(1_500)

            modal = page.get_by_test_id("playlist-edit-details-modal")
            name_input = modal.get_by_test_id("playlist-edit-details-name-input")
            await name_input.fill(name)

            if description:
                await modal.get_by_test_id("playlist-edit-details-description-input").fill(description)

            await modal.get_by_test_id("playlist-edit-details-save-button").click()
            await page.locator("main h1").first.wait_for(timeout=15_000)
            await page.wait_for_timeout(3_000)
            persisted_name = (await page.locator("main h1").first.inner_text()).strip()
            playlist_id = _extract_playlist_id(page.url)
            if playlist_id is None:
                raise RuntimeError("Spotify created a playlist, but the playlist id could not be read from the browser URL.")
            if persisted_name != name:
                raise RuntimeError(
                    f"Spotify created playlist {playlist_id} but did not persist the requested name. "
                    f"Expected {name!r}, got {persisted_name!r}."
                )

            return Playlist(
                name=persisted_name,
                description=description,
                is_public=public,
                platform_id=playlist_id,
                platform=Platform.SPOTIFY,
            )
        finally:
            await context_manager.__aexit__(None, None, None)

    async def _fetch_browser_track_metadata(self, track_id: str) -> Track:
        context_manager, page = await self._open_browser_page(f"https://open.spotify.com/track/{track_id}")
        try:
            title = (await page.locator("main h1").first.inner_text()).strip()
            artists = [
                artist.strip()
                for artist in await page.locator('main a[href^="/artist/"]').evaluate_all(
                    "els => els.slice(0, 5).map(el => el.textContent || '')"
                )
                if artist.strip()
            ]
            if not title or not artists:
                raise RuntimeError(f"Spotify browser automation could not read metadata for track {track_id}.")

            return Track(
                title=title,
                artists=artists,
                platform_id=track_id,
                platform=Platform.SPOTIFY,
            )
        finally:
            await context_manager.__aexit__(None, None, None)

    async def _add_browser_track_to_playlist(self, page: Page, track: Track) -> None:
        query = f"{track.title} {' '.join(track.artists[:2])}".strip()
        search_box = page.get_by_placeholder(SPOTIFY_TRACK_SEARCH_INPUT_PLACEHOLDER)
        await search_box.fill(query)
        await page.wait_for_timeout(3_000)

        row = page.locator('main [data-testid="tracklist-row"]').filter(has_text=track.title).filter(
            has_text=track.artists[0]
        ).first
        await row.get_by_test_id("add-to-playlist-button").click()
        await page.wait_for_timeout(1_500)
        await search_box.fill("")
        await page.wait_for_timeout(1_000)

    async def _add_browser_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        context_manager, page = await self._open_browser_page(f"https://open.spotify.com/playlist/{playlist_id}", wait_ms=4_000)
        try:
            search_box = page.get_by_placeholder(SPOTIFY_TRACK_SEARCH_INPUT_PLACEHOLDER)
            if not await search_box.count():
                raise RuntimeError("Spotify browser playlist page did not expose the track search box needed to add songs.")

            for track_id in track_ids:
                track = await self._fetch_browser_track_metadata(track_id)
                await self._add_browser_track_to_playlist(page, track)
        finally:
            await context_manager.__aexit__(None, None, None)

    async def _remove_browser_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        context_manager, page = await self._open_browser_page(f"https://open.spotify.com/playlist/{playlist_id}", wait_ms=4_000)
        try:
            for track_id in track_ids:
                row = page.locator(
                    f'[data-testid="playlist-tracklist"] [data-testid="tracklist-row"] a[href="/track/{track_id}"]'
                ).first.locator('xpath=ancestor::*[@data-testid="tracklist-row"]')
                if not await row.count():
                    continue

                await row.get_by_test_id("more-button").click()
                await page.get_by_role("menuitem", name="Remove from this playlist").click()
                await page.wait_for_timeout(1_500)
        finally:
            await context_manager.__aexit__(None, None, None)

    async def _like_browser_track(self, track_id: str) -> None:
        track = await self._fetch_browser_track_metadata(track_id)
        context_manager, page = await self._open_browser_page(
            f"https://open.spotify.com/search/{quote_plus(track.search_query)}/tracks",
            wait_ms=5_000,
        )
        try:
            row = page.locator('main [data-testid="tracklist-row"]').locator(
                f'a[href="/track/{track_id}"]'
            ).first.locator('xpath=ancestor::*[@data-testid="tracklist-row"]')
            if not await row.count():
                raise RuntimeError(f"Spotify browser search could not find track {track_id} to add to Liked Songs.")

            await row.get_by_test_id("more-button").click()
            save_menu_item = page.get_by_role("menuitem", name="Save to your Liked Songs")
            if await save_menu_item.count():
                await save_menu_item.click()
                await page.wait_for_timeout(1_500)
            else:
                await page.keyboard.press("Escape")
        finally:
            await context_manager.__aexit__(None, None, None)

    async def _like_browser_tracks(self, track_ids: list[str]) -> None:
        for track_id in track_ids:
            await self._like_browser_track(track_id)

    async def authenticate(self) -> None:
        # Saved API credentials are preferred: faster and more robust than browser scraping.
        if await self._authenticate_with_saved_tokens():
            return

        if await self._check_browser_auth(interactive=False):
            self._browser_authenticated = True
            return

        if await self._check_browser_auth(interactive=True):
            self._browser_authenticated = True
            return

        raise RuntimeError(
            "Spotify browser login did not complete in time. Run the command again and finish login in the opened browser window."
        )

    async def _authenticate_with_saved_tokens(self) -> bool:
        """Try the saved bearer token, then cached OAuth. Returns False if neither is usable.

        A token that loads but is rejected by the API (e.g. Spotify's "Premium required"
        policy for dev apps, or immediate rate limiting) counts as unusable, so callers
        fall back to browser automation instead of failing on the first real request.
        """
        # ── Bearer token path (no developer app / no Premium required) ────────
        token_data = load_token("spotify_spdc")
        if token_data and token_data.get("access_token"):
            try:
                am = BearerAuthManager(token_data)
                am.get_access_token()  # raises if expired
                # retries=0: raise immediately on 429 instead of sleeping
                client = spotipy.Spotify(auth_manager=am, retries=0, requests_timeout=10)
                if await self._client_is_usable(client):
                    self._client = client
                    return True
            except RuntimeError:
                delete_token("spotify_spdc")

        # ── OAuth path (requires SPOTIFY_CLIENT_ID + SECRET) ──────────────────
        if not self.client_id or not self.client_secret:
            return False
        auth_manager = SpotifyOAuth(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            scope=" ".join(SCOPES),
            cache_handler=_FileCacheHandler(),
            open_browser=False,
        )
        cached = await asyncio.to_thread(auth_manager.get_cached_token)
        if cached and not auth_manager.is_token_expired(cached):
            client = spotipy.Spotify(auth_manager=auth_manager, retries=0, requests_timeout=10)
            if await self._client_is_usable(client):
                self._client = client
                return True
        return False

    @staticmethod
    async def _client_is_usable(client: spotipy.Spotify) -> bool:
        """Confirm the API actually accepts this client with a lightweight call."""
        try:
            await asyncio.wait_for(asyncio.to_thread(client.current_user), timeout=15)
            return True
        except Exception:
            return False

    @property
    def client(self) -> spotipy.Spotify:
        if self._client is None:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return self._client

    async def get_playlists(self) -> list[Playlist]:
        if self._browser_authenticated:
            return await self._fetch_browser_playlists()

        def _fetch() -> list[Playlist]:
            playlists: list[Playlist] = []
            try:
                results = self.client.current_user_playlists()
            except spotipy.SpotifyException as exc:
                _raise_spotify_runtime_error(exc)
            while results:
                for item in results["items"]:
                    playlists.append(self._parse_playlist_stub(item))
                results = self.client.next(results) if results["next"] else None
            return playlists
        return await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=20)

    async def get_playlist(self, playlist_id: str) -> Playlist:
        if self._browser_authenticated:
            return await self._fetch_browser_playlist(playlist_id)

        def _fetch() -> Playlist:
            data = self.client.playlist(playlist_id)
            pl = self._parse_playlist_stub(data)
            tracks: list[Track] = []
            results = self.client.playlist_tracks(playlist_id)
            while results:
                for item in results["items"]:
                    if item and item.get("track"):
                        tracks.append(self._parse_track(item["track"]))
                results = self.client.next(results) if results["next"] else None
            pl.tracks = tracks
            return pl
        return await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=60)

    async def search_track(self, query: str, limit: int = 5) -> list[Track]:
        if self._browser_authenticated:
            return await self._fetch_browser_search_tracks(query, limit=limit)

        def _fetch() -> list[Track]:
            results = self.client.search(q=query, type="track", limit=limit)
            return [self._parse_track(item) for item in results["tracks"]["items"]]
        return await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=10)

    async def create_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        if self._browser_authenticated:
            return await self._create_browser_playlist(name, description=description, public=public)

        def _create() -> Playlist:
            user_id = self.client.current_user()["id"]
            data = self.client.user_playlist_create(user=user_id, name=name, public=public, description=description)
            return self._parse_playlist_stub(data)
        return await asyncio.wait_for(asyncio.to_thread(_create), timeout=15)

    async def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        if self._browser_authenticated:
            await self._add_browser_tracks(playlist_id, track_ids)
            return

        uris = [f"spotify:track:{tid}" if not tid.startswith("spotify:") else tid for tid in track_ids]
        def _add() -> None:
            for i in range(0, len(uris), 100):
                self.client.playlist_add_items(playlist_id, uris[i:i + 100])
        await asyncio.wait_for(asyncio.to_thread(_add), timeout=30)

    async def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        if self._browser_authenticated:
            await self._remove_browser_tracks(playlist_id, track_ids)
            return

        uris = [f"spotify:track:{tid}" if not tid.startswith("spotify:") else tid for tid in track_ids]
        def _remove() -> None:
            for i in range(0, len(uris), 100):
                self.client.playlist_remove_all_occurrences_of_items(playlist_id, uris[i:i + 100])
        await asyncio.wait_for(asyncio.to_thread(_remove), timeout=30)

    async def get_liked_songs(self) -> list[Track]:
        if self._browser_authenticated:
            return await self._fetch_browser_liked_songs()

        def _fetch() -> list[Track]:
            tracks: list[Track] = []
            results = self.client.current_user_saved_tracks()
            while results:
                for item in results["items"]:
                    if item.get("track"):
                        tracks.append(self._parse_track(item["track"]))
                results = self.client.next(results) if results["next"] else None
            return tracks
        return await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=60)

    async def like_tracks(self, track_ids: list[str]) -> None:
        if self._browser_authenticated:
            await self._like_browser_tracks(track_ids)
            return

        def _like() -> None:
            for i in range(0, len(track_ids), 50):
                self.client.current_user_saved_tracks_add(track_ids[i:i + 50])
        await asyncio.wait_for(asyncio.to_thread(_like), timeout=30)

    def _parse_track(self, data: dict) -> Track:  # type: ignore[type-arg]
        return Track(
            title=data["name"],
            artists=[a["name"] for a in data.get("artists", [])],
            album=data.get("album", {}).get("name"),
            duration_ms=data.get("duration_ms"),
            isrc=data.get("external_ids", {}).get("isrc"),
            platform_id=data["id"],
            platform=Platform.SPOTIFY,
        )

    def _parse_playlist_stub(self, data: dict) -> Playlist:  # type: ignore[type-arg]
        return Playlist(
            name=data["name"],
            platform_id=data["id"],
            platform=Platform.SPOTIFY,
            description=data.get("description", ""),
            is_public=data.get("public", False),
            owner=data.get("owner", {}).get("display_name"),
        )


class _FileCacheHandler(spotipy.CacheHandler):
    def get_cached_token(self) -> Optional[dict]:  # type: ignore[type-arg]
        return load_token("spotify")

    def save_token_to_cache(self, token_info: dict) -> None:  # type: ignore[type-arg]
        save_token("spotify", token_info)

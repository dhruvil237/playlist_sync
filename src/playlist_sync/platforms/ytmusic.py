"""YouTube Music platform adapter using ytmusicapi."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import asyncio
from ytmusicapi import YTMusic
from ytmusicapi import setup_oauth

from playlist_sync.core.models import Platform, Playlist, Track
from playlist_sync.platforms.base import BasePlatform
from playlist_sync.platforms.browser_session import PlaywrightPersistentSession

CONFIG_DIR = Path.home() / ".config" / "playlist-sync"
OAUTH_FILE = CONFIG_DIR / "ytmusic_oauth.json"
HEADERS_FILE = CONFIG_DIR / "ytmusic_headers.json"
YTMUSIC_LIBRARY_PLAYLISTS_URL = "https://music.youtube.com/library/playlists"
YTMUSIC_LIKED_MUSIC_PLAYLIST_ID = "LM"


def _extract_ytmusic_playlist_id(href: str) -> str | None:
    if not href:
        return None
    parsed = urlparse(href)
    playlist_ids = parse_qs(parsed.query).get("list")
    if not playlist_ids:
        return None
    return playlist_ids[0] or None


def _extract_ytmusic_track_id(href: str) -> str | None:
    if not href:
        return None
    parsed = urlparse(href)
    video_ids = parse_qs(parsed.query).get("v")
    if video_ids and video_ids[0]:
        return video_ids[0]

    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return None
    if path_parts[0] in {"podcast", "watch"} and len(path_parts) >= 2:
        return path_parts[1]
    return None


def _parse_duration_ms(duration_text: str) -> int | None:
    if not duration_text or ":" not in duration_text:
        return None
    parts = duration_text.split(":")
    if not all(part.isdigit() for part in parts):
        return None

    total_seconds = 0
    for part in parts:
        total_seconds = total_seconds * 60 + int(part)
    return total_seconds * 1000


def _infer_browser_track_artists(
    row_text: str,
    title: str,
    duration: str,
    artists: list[str],
    anchor_texts: list[str],
) -> list[str]:
    if artists:
        return artists

    inferred = row_text.strip()
    if title and inferred.startswith(title):
        inferred = inferred[len(title):].strip()
    if duration and inferred.endswith(duration):
        inferred = inferred[: -len(duration)].strip()

    for anchor_text in anchor_texts:
        cleaned_anchor_text = anchor_text.strip()
        if not cleaned_anchor_text or cleaned_anchor_text == title:
            continue
        inferred = inferred.replace(cleaned_anchor_text, " ")

    inferred = re.sub(r"^(?:Song|Video|Artist|Playlist|Album|Episode|Podcast)\s*•\s*", "", inferred)
    inferred = re.sub(r"\s*•\s*[\d.,]+[A-Za-z]*\s+(?:plays|views|monthly audience)\b.*$", "", inferred)
    inferred = re.sub(r"\s+[\d.,]+[A-Za-z]*\s+(?:plays|views|monthly audience)\b.*$", "", inferred)
    inferred = re.sub(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}\b", " ", inferred)
    inferred = re.sub(r"\s+", " ", inferred).strip(" ,&")
    if not inferred:
        return []

    return [part.strip() for part in re.split(r"\s*&\s*|\s*,\s*", inferred) if part.strip()]


def _parse_browser_playlists(items: list[dict[str, str]]) -> list[Playlist]:
    seen_ids: set[str] = set()
    playlists: list[Playlist] = []
    for item in items:
        playlist_id = _extract_ytmusic_playlist_id(item.get("href", ""))
        name = item.get("text", "").strip()
        if not playlist_id or not name or playlist_id in seen_ids:
            continue
        seen_ids.add(playlist_id)
        playlists.append(
            Playlist(
                name=name,
                platform_id=playlist_id,
                platform=Platform.YTMUSIC,
            )
        )
    return playlists


def _parse_browser_track_rows(items: list[dict[str, object]]) -> list[Track]:
    tracks: list[Track] = []
    for item in items:
        title = str(item.get("title", "")).strip()
        track_id = _extract_ytmusic_track_id(str(item.get("title_href", "")))
        artists = [
            str(artist).strip()
            for artist in item.get("artists", [])
            if str(artist).strip()
        ]
        artists = _infer_browser_track_artists(
            str(item.get("text", "")),
            title,
            str(item.get("duration", "")),
            artists,
            [str(anchor_text).strip() for anchor_text in item.get("anchor_texts", []) if str(anchor_text).strip()],
        )
        if not title or not track_id or not artists:
            continue

        tracks.append(
            Track(
                title=title,
                artists=artists,
                duration_ms=_parse_duration_ms(str(item.get("duration", ""))),
                platform_id=track_id,
                platform=Platform.YTMUSIC,
            )
        )
    return tracks


def _browser_row_eval_script() -> str:
    return (
        "els => els.map(el => {"
        "const anchors = Array.from(el.querySelectorAll('a')).map(a => ({href: a.getAttribute('href') || '', text: (a.textContent || '').trim()})).filter(a => a.text);"
        "const primaryAnchor = anchors[0] || {href: '', text: ''};"
        "return ({"
        "text: (el.textContent || '').replace(/\\s+/g, ' ').trim(),"
        "title: primaryAnchor.text,"
        "title_href: primaryAnchor.href,"
        "artists: Array.from(el.querySelectorAll('a[href*=\"channel/\"]')).map(a => (a.textContent || '').trim()).filter(Boolean),"
        "anchor_texts: anchors.map(a => a.text),"
        "duration: (Array.from(el.querySelectorAll('yt-formatted-string')).map(node => (node.textContent || '').trim()).find(text => /^\\d{1,2}:\\d{2}(?::\\d{2})?$/.test(text)) || '')"
        "});"
        "})"
    )


def _requires_ytmusic_login(page_text: str, current_url: str) -> bool:
    lower_text = page_text.lower()
    lower_url = current_url.lower()
    return (
        "accounts.google.com" in lower_url
        or "servicelogin" in lower_url
        or "sign in to create & share playlists" in lower_text
        or "sign in to listen to your liked tracks" in lower_text
    )


def _is_ytmusic_logged_in(page_text: str, current_url: str) -> bool:
    """Positive logged-in check.

    Absence of login markers is not enough: a half-loaded page has no markers
    either. Require a rendered page (non-trivial text) without the "Sign in"
    button the logged-out shell always shows.
    """
    if _requires_ytmusic_login(page_text, current_url):
        return False
    lower_text = page_text.lower()
    if "sign in" in lower_text:
        return False
    return len(page_text.strip()) >= 100


def _expected_playlist_track_count(page_text: str) -> int | None:
    match = re.search(r"(\d+)\s+tracks\b", page_text)
    if match is None:
        return None
    return int(match.group(1))


class YTMusicPlatform(BasePlatform):
    platform = Platform.YTMUSIC

    def __init__(self, auth_file: Optional[Path] = None) -> None:
        self.auth_file = auth_file  # if None, chosen at authenticate() time
        self._client: Optional[YTMusic] = None
        self._browser_authenticated = False

    async def _check_browser_auth(self, *, interactive: bool) -> bool:
        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=not interactive) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(YTMUSIC_LIBRARY_PLAYLISTS_URL, wait_until="domcontentloaded")

            if interactive:
                print("YouTube Music browser login required.")
                print("A browser window has been opened for the playlist-sync YouTube Music profile.")
                print("Log in there, then wait for this command to continue automatically.")
                await page.wait_for_timeout(2_000)
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    page_text = await page.locator("body").inner_text()
                    if _is_ytmusic_logged_in(page_text, page.url):
                        return True
                    await page.wait_for_timeout(2_000)
                return False

            # Poll briefly: a slow-loading page is indeterminate, not logged in.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                page_text = await page.locator("body").inner_text()
                if _is_ytmusic_logged_in(page_text, page.url):
                    return True
                if _requires_ytmusic_login(page_text, page.url):
                    return False
                await page.wait_for_timeout(2_000)
            return False

    async def _fetch_browser_playlists(self) -> list[Playlist]:
        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=True) as context:
            page = await context.new_page()
            await page.goto(YTMUSIC_LIBRARY_PLAYLISTS_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(5_000)
            page_text = await page.locator("body").inner_text()
            if _requires_ytmusic_login(page_text, page.url):
                raise RuntimeError(
                    "YouTube Music browser automation is enabled, but the playlist-sync YouTube Music browser profile is not logged in. "
                    "Run `playlist-sync auth login ytmusic` and finish login in the opened browser window."
                )

            items = await page.locator("a[href*='playlist?list=']").evaluate_all(
                "els => els.map(el => ({href: el.href || el.getAttribute('href') || '', text: (el.textContent || '').trim()}))"
            )
            return _parse_browser_playlists(items)

    async def _load_all_browser_playlist_rows(self, page, expected_count: int | None = None) -> None:  # type: ignore[no-untyped-def]
        previous_count = -1
        stable_iterations = 0
        for _ in range(12):
            current_count = await page.locator("ytmusic-responsive-list-item-renderer").count()
            if expected_count is not None and current_count >= expected_count:
                return
            if current_count == previous_count:
                stable_iterations += 1
                if stable_iterations >= 2:
                    return
            else:
                stable_iterations = 0
                previous_count = current_count

            await page.keyboard.press("End")
            await page.wait_for_timeout(4_000)

    async def _fetch_browser_playlist(self, playlist_id: str) -> Playlist:
        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=True) as context:
            page = await context.new_page()
            await page.goto(f"https://music.youtube.com/playlist?list={playlist_id}", wait_until="domcontentloaded")
            await page.wait_for_timeout(5_000)
            page_text = await page.locator("body").inner_text()
            if _requires_ytmusic_login(page_text, page.url):
                raise RuntimeError(
                    "YouTube Music browser automation is enabled, but the playlist-sync YouTube Music browser profile is not logged in. "
                    "Run `playlist-sync auth login ytmusic` and finish login in the opened browser window."
                )

            await self._load_all_browser_playlist_rows(page, _expected_playlist_track_count(page_text))
            headings = await page.locator("h1, h2, h3").evaluate_all(
                "els => els.map(el => (el.textContent || '').replace(/\\s+/g, ' ').trim()).filter(Boolean)"
            )
            title = str(headings[0]).strip() if headings else ""
            rows = await page.locator("ytmusic-responsive-list-item-renderer").evaluate_all(_browser_row_eval_script())
            tracks = _parse_browser_track_rows(rows)
            return Playlist(
                name=title,
                platform_id=playlist_id,
                platform=Platform.YTMUSIC,
                tracks=tracks,
            )

    async def _fetch_browser_search_tracks(self, query: str, limit: int = 5) -> list[Track]:
        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=True) as context:
            page = await context.new_page()
            try:
                return await self._fetch_browser_search_tracks_page(page, query, limit=limit)
            finally:
                await page.close()

    async def _fetch_browser_search_tracks_page(self, page, query: str, limit: int = 5) -> list[Track]:
        await page.goto(f"https://music.youtube.com/search?q={quote_plus(query)}", wait_until="domcontentloaded")
        await page.wait_for_timeout(5_000)
        page_text = await page.locator("body").inner_text()
        if _requires_ytmusic_login(page_text, page.url):
            raise RuntimeError(
                "YouTube Music browser automation is enabled, but the playlist-sync YouTube Music browser profile is not logged in. "
                "Run `playlist-sync auth login ytmusic` and finish login in the opened browser window."
            )

        rows = await page.locator("ytmusic-responsive-list-item-renderer").evaluate_all(_browser_row_eval_script())
        song_rows = [
            row for row in rows
            if "Song •" in str(row.get("text", "")) and str(row.get("title_href", "")).strip()
        ]
        return _parse_browser_track_rows(song_rows[:limit])

    async def batch_search_tracks(
        self,
        queries: list[str],
        *,
        limit: int = 5,
        workers: int = 1,
    ) -> list[list[Track]]:
        if not self._browser_authenticated or workers <= 1 or len(queries) <= 1:
            return await super().batch_search_tracks(queries, limit=limit, workers=workers)

        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=True) as context:
            semaphore = asyncio.Semaphore(max(1, workers))
            results: list[list[Track] | None] = [None] * len(queries)

            async def _search(index: int, query: str) -> None:
                async with semaphore:
                    page = await context.new_page()
                    try:
                        results[index] = await self._fetch_browser_search_tracks_page(page, query, limit=limit)
                    finally:
                        await page.close()

            await asyncio.gather(*[_search(index, query) for index, query in enumerate(queries)])
            return [tracks or [] for tracks in results]

    async def _create_browser_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=True) as context:
            page = await context.new_page()
            await page.goto(YTMUSIC_LIBRARY_PLAYLISTS_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(5_000)
            page_text = await page.locator("body").inner_text()
            if _requires_ytmusic_login(page_text, page.url):
                raise RuntimeError(
                    "YouTube Music browser automation is enabled, but the playlist-sync YouTube Music browser profile is not logged in. "
                    "Run `playlist-sync auth login ytmusic` and finish login in the opened browser window."
                )

            await page.get_by_role("button", name="New playlist").click()
            form = page.locator("ytmusic-playlist-form").first
            await form.wait_for(timeout=15_000)

            await form.locator("input").first.fill(name)
            if description:
                await form.locator("textarea").first.fill(description)

            if not public:
                await form.get_by_role("combobox", name="Privacy").click()
                await page.get_by_role("option", name=re.compile(r"^Private\b")).click()
            await form.get_by_role("button", name="Create").click()

            await page.wait_for_url(re.compile(r"https://music\.youtube\.com/playlist\?list=.*"), timeout=15_000)
            await page.wait_for_timeout(3_000)

            playlist_id = _extract_ytmusic_playlist_id(page.url)
            if not playlist_id:
                raise RuntimeError("YouTube Music created a playlist, but the playlist id could not be read from the browser URL.")

            headings = await page.locator("h1, h2, h3").evaluate_all(
                "els => els.map(el => (el.textContent || '').replace(/\\s+/g, ' ').trim()).filter(Boolean)"
            )
            persisted_name = str(headings[0]).strip() if headings else ""
            if persisted_name != name:
                raise RuntimeError(
                    f"YouTube Music created playlist {playlist_id} but did not persist the requested name. "
                    f"Expected {name!r}, got {persisted_name!r}."
                )

            return Playlist(
                name=persisted_name,
                platform_id=playlist_id,
                platform=Platform.YTMUSIC,
                description=description,
                is_public=public,
            )

    async def _add_browser_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        target_playlist = await self._fetch_browser_playlist(playlist_id)
        playlist_name = target_playlist.name.strip()
        if not playlist_name:
            raise RuntimeError(f"YouTube Music browser automation could not read the playlist name for {playlist_id}.")

        existing_ids = {track.platform_id for track in target_playlist.tracks if track.platform_id}
        pending_ids = [track_id for track_id in track_ids if track_id and track_id not in existing_ids]
        if not pending_ids:
            return

        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=True) as context:
            page = await context.new_page()
            for track_id in pending_ids:
                await page.goto(f"https://music.youtube.com/watch?v={track_id}", wait_until="domcontentloaded")
                await page.wait_for_timeout(4_000)
                page_text = await page.locator("body").inner_text()
                if _requires_ytmusic_login(page_text, page.url):
                    raise RuntimeError(
                        "YouTube Music browser automation is enabled, but the playlist-sync YouTube Music browser profile is not logged in. "
                        "Run `playlist-sync auth login ytmusic` and finish login in the opened browser window."
                    )

                await page.get_by_role("button", name="Action menu").first.click(force=True)
                await page.wait_for_timeout(750)
                await page.locator("ytmusic-menu-navigation-item-renderer").filter(has_text="Save to playlist").first.click()
                await page.wait_for_timeout(1_000)

                option = page.locator("ytmusic-playlist-add-to-option-renderer").filter(has_text=playlist_name).first
                await option.click(force=True)
                await page.wait_for_timeout(1_250)

    async def _remove_browser_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=True) as context:
            page = await context.new_page()
            await page.goto(f"https://music.youtube.com/playlist?list={playlist_id}", wait_until="domcontentloaded")
            await page.wait_for_timeout(5_000)
            page_text = await page.locator("body").inner_text()
            if _requires_ytmusic_login(page_text, page.url):
                raise RuntimeError(
                    "YouTube Music browser automation is enabled, but the playlist-sync YouTube Music browser profile is not logged in. "
                    "Run `playlist-sync auth login ytmusic` and finish login in the opened browser window."
                )

            await self._load_all_browser_playlist_rows(page, _expected_playlist_track_count(page_text))
            for track_id in track_ids:
                row = page.locator("ytmusic-responsive-list-item-renderer").filter(
                    has=page.locator(f'a[href*="v={track_id}"]')
                ).first
                if not await row.count():
                    continue

                await row.get_by_role("button", name="Action menu").first.click(force=True)
                await page.wait_for_timeout(750)
                remove_item = page.locator(
                    "ytmusic-menu-service-item-renderer, ytmusic-menu-navigation-item-renderer"
                ).filter(has_text="Remove from playlist").first
                if not await remove_item.count():
                    await page.keyboard.press("Escape")
                    continue
                await remove_item.click()
                await page.wait_for_timeout(1_250)

    async def _like_browser_tracks(self, track_ids: list[str]) -> None:
        async with PlaywrightPersistentSession(Platform.YTMUSIC, headless=True) as context:
            page = await context.new_page()
            for track_id in track_ids:
                await page.goto(f"https://music.youtube.com/watch?v={track_id}", wait_until="domcontentloaded")
                await page.wait_for_timeout(4_000)
                page_text = await page.locator("body").inner_text()
                if _requires_ytmusic_login(page_text, page.url):
                    raise RuntimeError(
                        "YouTube Music browser automation is enabled, but the playlist-sync YouTube Music browser profile is not logged in. "
                        "Run `playlist-sync auth login ytmusic` and finish login in the opened browser window."
                    )

                like_button = page.locator("ytmusic-player-bar").get_by_role("button", name="Like", exact=True).first
                if not await like_button.count():
                    raise RuntimeError(
                        f"YouTube Music browser automation could not find the Like button for track {track_id}."
                    )
                if (await like_button.get_attribute("aria-pressed")) == "true":
                    continue  # already liked
                await like_button.click()
                await page.wait_for_timeout(1_000)

    async def ensure_browser_auth(self, *, interactive: bool = True) -> None:
        if await self._check_browser_auth(interactive=False):
            self._browser_authenticated = True
            return
        if interactive and await self._check_browser_auth(interactive=True):
            self._browser_authenticated = True
            return
        raise RuntimeError(
            "YouTube Music browser login did not complete in time. Run the command again and finish login in the opened browser window."
        )

    async def authenticate(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        client_id = os.environ.get("YTMUSIC_CLIENT_ID")
        client_secret = os.environ.get("YTMUSIC_CLIENT_SECRET")

        # Fast path: a valid saved auth file needs no browser at all.
        auth_file = self.auth_file or (OAUTH_FILE if client_id and client_secret else HEADERS_FILE)
        if auth_file.exists():
            try:
                client = YTMusic(str(auth_file))
                client.get_account_info()  # requires real auth
                self._client = client
                return
            except Exception:
                auth_file.unlink(missing_ok=True)

        # No valid saved auth. Check the browser profile, then try to (re)create
        # API credentials — silently via the logged-in profile when possible.
        self._browser_authenticated = await self._check_browser_auth(interactive=False)

        try:
            if client_id and client_secret:
                await self._authenticate_oauth(client_id, client_secret)
            else:
                await self._authenticate_headers()
            self._browser_authenticated = False  # the API client is preferred over scraping
            return
        except Exception:
            if self._browser_authenticated:
                return
            raise

    async def _authenticate_oauth(self, client_id: str, client_secret: str) -> None:
        """OAuth flow — requires YTMUSIC_CLIENT_ID + YTMUSIC_CLIENT_SECRET in .env.
        Token refreshes automatically; no need to re-auth after expiry.
        """
        auth_file = self.auth_file or OAUTH_FILE

        if auth_file.exists():
            # Validate the existing token is usable. get_account_info() requires real
            # auth; get_home() succeeds even for logged-out sessions, and
            # get_history() fails when the account has watch history disabled.
            try:
                self._client = YTMusic(str(auth_file))
                self._client.get_account_info()
                return
            except Exception:
                self._client = None
                auth_file.unlink(missing_ok=True)

        print("\nYouTube Music OAuth setup")
        print("─" * 40)
        setup_oauth(
            client_id=client_id,
            client_secret=client_secret,
            filepath=str(auth_file),
            open_browser=False,
        )
        self._client = YTMusic(str(auth_file))
        print("Authenticated with YouTube Music (OAuth).")

    async def _authenticate_headers(self) -> None:
        """Browser headers flow — opens Brave automatically to capture headers."""
        auth_file = self.auth_file or HEADERS_FILE

        if auth_file.exists():
            try:
                self._client = YTMusic(str(auth_file))
                self._client.get_account_info()  # requires real auth; get_home() passes logged out
                return
            except Exception:
                self._client = None
                auth_file.unlink(missing_ok=True)

        from playlist_sync.platforms.ytmusic_auth import capture_headers_via_browser
        # Silent headless capture when the browser profile is already logged in.
        success = await capture_headers_via_browser(auth_file, headless=self._browser_authenticated)

        if not success:
            if self._browser_authenticated:
                # Browser automation already works — don't block on a stdin paste prompt.
                raise RuntimeError(
                    "Automated YouTube Music header capture failed; using browser automation instead."
                )
            # No headed browser found — fall back to manual paste
            import ytmusicapi as _ytmusicapi
            print("\nNo browser found for automated capture. Falling back to manual entry.")
            print("1. Open https://music.youtube.com in your browser and log in.")
            print("2. Open DevTools (F12) → Network tab.")
            print("3. Click any request to music.youtube.com → copy all Request Headers.")
            print("4. Paste them below, then press Ctrl-D.\n")
            _ytmusicapi.setup(filepath=str(auth_file))

        self._client = YTMusic(str(auth_file))
        print("Authenticated with YouTube Music.")

    @property
    def client(self) -> YTMusic:
        if self._client is None:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return self._client

    async def get_playlists(self) -> list[Playlist]:
        if self._browser_authenticated:
            return await self._fetch_browser_playlists()
        data = self.client.get_library_playlists(limit=500)
        return [self._parse_playlist_stub(item) for item in (data or [])]

    async def get_playlist(self, playlist_id: str) -> Playlist:
        if self._browser_authenticated:
            return await self._fetch_browser_playlist(playlist_id)
        data = self.client.get_playlist(playlist_id, limit=5000)
        pl = Playlist(
            name=data["title"],
            platform_id=playlist_id,
            platform=Platform.YTMUSIC,
            description=data.get("description", ""),
        )
        pl.tracks = [self._parse_track(t) for t in data.get("tracks", []) if t]
        return pl

    async def search_track(self, query: str, limit: int = 5) -> list[Track]:
        if self._browser_authenticated:
            return await self._fetch_browser_search_tracks(query, limit=limit)
        results = self.client.search(query, filter="songs", limit=limit)
        return [self._parse_track(r) for r in results if r]

    async def create_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        if self._browser_authenticated:
            return await self._create_browser_playlist(name, description=description, public=public)

        privacy = "PUBLIC" if public else "PRIVATE"
        playlist_id = self.client.create_playlist(name, description, privacy_status=privacy)
        return Playlist(
            name=name,
            platform_id=playlist_id,
            platform=Platform.YTMUSIC,
            description=description,
            is_public=public,
        )

    async def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        if self._browser_authenticated:
            await self._add_browser_tracks(playlist_id, track_ids)
            return

        self.client.add_playlist_items(playlist_id, track_ids)

    async def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        if self._browser_authenticated:
            await self._remove_browser_tracks(playlist_id, track_ids)
            return

        pl_data = self.client.get_playlist(playlist_id, limit=5000)
        set_video_ids = {
            t["videoId"]: t.get("setVideoId")
            for t in pl_data.get("tracks", [])
            if t and t.get("videoId") in track_ids
        }
        items_to_remove = [
            {"videoId": vid, "setVideoId": set_id}
            for vid, set_id in set_video_ids.items()
        ]
        if items_to_remove:
            self.client.remove_playlist_items(playlist_id, items_to_remove)

    async def get_liked_songs(self) -> list[Track]:
        if self._browser_authenticated:
            liked = await self._fetch_browser_playlist(YTMUSIC_LIKED_MUSIC_PLAYLIST_ID)
            return liked.tracks

        data = self.client.get_liked_songs(limit=5000)
        return [self._parse_track(t) for t in data.get("tracks", []) if t]

    async def like_tracks(self, track_ids: list[str]) -> None:
        if self._browser_authenticated:
            await self._like_browser_tracks(track_ids)
            return

        for video_id in track_ids:
            self.client.rate_song(video_id, "LIKE")

    # ── Parsing helpers ──────────────────────────────────────────────────────

    def _parse_track(self, data: dict) -> Track:  # type: ignore[type-arg]
        artists = []
        for a in data.get("artists") or []:
            if isinstance(a, dict):
                artists.append(a.get("name", ""))
            elif isinstance(a, str):
                artists.append(a)

        album_info = data.get("album")
        album = album_info.get("name") if isinstance(album_info, dict) else None

        duration_ms: Optional[int] = None
        dur = data.get("duration_seconds") or data.get("duration")
        if isinstance(dur, int):
            duration_ms = dur * 1000
        elif isinstance(dur, str) and ":" in dur:
            parts = dur.split(":")
            duration_ms = (int(parts[0]) * 60 + int(parts[1])) * 1000

        return Track(
            title=data.get("title", ""),
            artists=artists,
            album=album,
            duration_ms=duration_ms,
            platform_id=data.get("videoId"),
            platform=Platform.YTMUSIC,
        )

    def _parse_playlist_stub(self, data: dict) -> Playlist:  # type: ignore[type-arg]
        return Playlist(
            name=data.get("title", ""),
            platform_id=data.get("playlistId"),
            platform=Platform.YTMUSIC,
            description=data.get("description", ""),
        )

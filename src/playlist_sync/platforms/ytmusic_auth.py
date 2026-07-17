"""Automated browser-based header capture for YouTube Music auth."""
from __future__ import annotations

import asyncio
from pathlib import Path

import ytmusicapi

from playlist_sync.core.models import Platform
from playlist_sync.platforms.browser_session import PlaywrightPersistentSession, find_browser_executable

_YTM_URL = "https://music.youtube.com"
_YTM_API_HOST = "music.youtube.com"


async def capture_headers_via_browser(auth_file: Path, *, headless: bool = False) -> bool:
    """
    Open a browser, navigate to YouTube Music, and automatically capture the
    request headers once the user is logged in. Use headless=True when the
    browser profile is already authenticated and no user interaction is needed.

    Returns True on success, False if headers could not be captured.
    """
    # Prefer a system browser; fall back to Playwright's bundled Chromium.
    browser_path = find_browser_executable()
    browser_name = Path(browser_path).stem if browser_path else "Chromium"

    async with PlaywrightPersistentSession(
        Platform.YTMUSIC,
        headless=headless,
        executable_path=browser_path,
    ) as context:
        page = context.pages[0] if context.pages else await context.new_page()

        captured_headers: str | None = None
        done = asyncio.Event()

        async def on_request(request) -> None:  # type: ignore[no-untyped-def]
            nonlocal captured_headers
            if done.is_set():
                return
            if _YTM_API_HOST not in request.url or "youtubei/v1" not in request.url:
                return
            # request.headers only holds provisional headers (never cookies);
            # all_headers() includes everything actually sent.
            headers = await request.all_headers()
            # Must have cookies and Google auth header to be usable
            if "cookie" in headers and "x-goog-authuser" in headers:
                captured_headers = "\n".join(f"{k}: {v}" for k, v in headers.items())
                done.set()

        page.on("request", on_request)

        print(f"\nOpening YouTube Music in {browser_name}...")
        print("Log in if prompted, then wait — headers will be captured automatically.\n")

        await page.goto(_YTM_URL)

        # Wait for initial load, then reload periodically to trigger API requests
        deadline = asyncio.get_event_loop().time() + 120
        while not done.is_set() and asyncio.get_event_loop().time() < deadline:
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            if not done.is_set():
                await asyncio.sleep(2)
                if not done.is_set():
                    try:
                        await page.reload()
                    except Exception:
                        pass

    if not captured_headers:
        return False

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    ytmusicapi.setup(filepath=str(auth_file), headers_raw=captured_headers)
    return True

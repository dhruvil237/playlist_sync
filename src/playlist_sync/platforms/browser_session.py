"""Shared Playwright session utilities for browser-driven platform automation."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from playwright.async_api import BrowserContext, Playwright, async_playwright

from playlist_sync.core.models import Platform

CONFIG_DIR = Path.home() / ".config" / "playlist-sync"
BROWSER_STATE_DIR = CONFIG_DIR / "browser"

DEFAULT_BROWSER_PATHS = [
    "/mnt/c/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe",
    "/mnt/c/Program Files (x86)/BraveSoftware/Brave-Browser/Application/brave.exe",
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/opt/google/chrome/chrome",
    "/usr/bin/brave-browser",
    "/opt/brave.com/brave/brave-browser",
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/microsoft-edge",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
]

BROWSER_PATH_ENV_VAR = "PLAYLIST_SYNC_BROWSER_PATH"

YTMUSIC_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def _is_compatible_browser_path(raw_path: str) -> bool:
    path = Path(raw_path)
    if os.name != "posix":
        return True
    return not (path.suffix.lower() == ".exe" or raw_path.startswith("/mnt/"))


def find_browser_executable(paths: Sequence[str] = DEFAULT_BROWSER_PATHS) -> str | None:
    for raw_path in paths:
        if _is_compatible_browser_path(raw_path) and Path(raw_path).exists():
            return raw_path
    return None


def browser_profile_dir(profile_name: str | Platform) -> Path:
    profile_key = profile_name.value if isinstance(profile_name, Platform) else profile_name
    path = BROWSER_STATE_DIR / profile_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_executable_path(headless: bool, explicit_path: str | None = None) -> str | None:
    if explicit_path is not None:
        return explicit_path

    configured_path = os.environ.get(BROWSER_PATH_ENV_VAR)
    if configured_path:
        return configured_path

    if headless:
        return None

    return find_browser_executable()


def default_user_agent(profile_name: str | Platform) -> str | None:
    if profile_name == Platform.YTMUSIC or profile_name == Platform.YTMUSIC.value:
        return YTMUSIC_USER_AGENT
    return None


@dataclass(slots=True)
class PlaywrightPersistentSession:
    profile_name: str | Platform
    headless: bool = False
    args: Sequence[str] = field(default_factory=lambda: ["--no-sandbox"])
    executable_path: str | None = None
    user_agent: str | None = None
    ignore_default_args: Sequence[str] = field(default_factory=tuple)

    _playwright: Playwright | None = field(init=False, default=None)
    _context: BrowserContext | None = field(init=False, default=None)

    async def __aenter__(self) -> BrowserContext:
        self._playwright = await async_playwright().start()
        launch_kwargs = {
            "user_data_dir": str(browser_profile_dir(self.profile_name)),
            "headless": self.headless,
            "args": [*self.args, "--disable-blink-features=AutomationControlled"],
        }
        ignored_args = [*self.ignore_default_args]
        if not self.headless and "--enable-automation" not in ignored_args:
            ignored_args.append("--enable-automation")
        if ignored_args:
            launch_kwargs["ignore_default_args"] = ignored_args
        user_agent = self.user_agent or default_user_agent(self.profile_name)
        if user_agent is not None:
            launch_kwargs["user_agent"] = user_agent
        executable_path = resolve_executable_path(self.headless, self.executable_path)
        if executable_path is not None:
            launch_kwargs["executable_path"] = executable_path
        self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        return self._context

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
"""Tests for shared Playwright browser session helpers."""
from __future__ import annotations

from pathlib import Path

from playlist_sync.core.models import Platform
from playlist_sync.platforms.browser_session import (
    BROWSER_PATH_ENV_VAR,
    _is_compatible_browser_path,
    browser_profile_dir,
    default_user_agent,
    find_browser_executable,
    resolve_executable_path,
)


def test_find_browser_executable_returns_first_existing_path(tmp_path: Path) -> None:
    first = tmp_path / "missing-browser"
    second = tmp_path / "chrome"
    second.write_text("binary")

    result = find_browser_executable([str(first), str(second)])

    assert result == str(second)


def test_find_browser_executable_skips_windows_browser_path_on_linux(tmp_path: Path) -> None:
    windows_browser = tmp_path / "browser.exe"
    native_browser = tmp_path / "chrome"
    windows_browser.write_text("binary")
    native_browser.write_text("binary")

    result = find_browser_executable([str(windows_browser), str(native_browser)])

    assert result == str(native_browser)


def test_compatible_browser_path_rejects_windows_executable_on_linux() -> None:
    assert _is_compatible_browser_path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe") is False


def test_browser_profile_dir_uses_platform_value(monkeypatch, tmp_path: Path) -> None:
    import playlist_sync.platforms.browser_session as browser_session

    monkeypatch.setattr(browser_session, "BROWSER_STATE_DIR", tmp_path)

    result = browser_profile_dir(Platform.SPOTIFY)

    assert result == tmp_path / "spotify"
    assert result.exists()


def test_browser_profile_dir_uses_string_profile(monkeypatch, tmp_path: Path) -> None:
    import playlist_sync.platforms.browser_session as browser_session

    monkeypatch.setattr(browser_session, "BROWSER_STATE_DIR", tmp_path)

    result = browser_profile_dir("ytmusic-read")

    assert result == tmp_path / "ytmusic-read"
    assert result.exists()


def test_resolve_executable_path_skips_external_browser_for_headless_sessions(monkeypatch) -> None:
    monkeypatch.setattr(
        "playlist_sync.platforms.browser_session.find_browser_executable",
        lambda: "/usr/bin/google-chrome",
    )

    result = resolve_executable_path(headless=True)

    assert result is None


def test_resolve_executable_path_uses_detected_browser_for_headed_sessions(monkeypatch) -> None:
    monkeypatch.setattr(
        "playlist_sync.platforms.browser_session.find_browser_executable",
        lambda: "/usr/bin/google-chrome",
    )

    result = resolve_executable_path(headless=False)

    assert result == "/usr/bin/google-chrome"


def test_resolve_executable_path_prefers_env_override(monkeypatch) -> None:
    monkeypatch.setenv(BROWSER_PATH_ENV_VAR, "/custom/from-env")
    monkeypatch.setattr(
        "playlist_sync.platforms.browser_session.find_browser_executable",
        lambda: "/usr/bin/google-chrome",
    )

    result = resolve_executable_path(headless=False)

    assert result == "/custom/from-env"


def test_resolve_executable_path_uses_explicit_path_for_headless_sessions() -> None:
    result = resolve_executable_path(headless=True, explicit_path="/custom/browser")

    assert result == "/custom/browser"


def test_default_user_agent_uses_chrome_signature_for_ytmusic() -> None:
    result = default_user_agent(Platform.YTMUSIC)

    assert result is not None
    assert "Chrome/" in result


def test_default_user_agent_is_none_for_spotify() -> None:
    assert default_user_agent(Platform.SPOTIFY) is None
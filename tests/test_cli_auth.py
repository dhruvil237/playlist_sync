"""Tests for CLI auth state helpers."""
from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from playlist_sync.cli.app import _clear_saved_auth, _has_saved_auth
from playlist_sync.core.models import Platform


def test_has_saved_auth_detects_spotify_bearer_token(monkeypatch, tmp_path: Path) -> None:
    import playlist_sync.cli.app as cli_app

    monkeypatch.setattr(cli_app, "has_usable_spotify_auth", lambda: True)
    monkeypatch.setattr(cli_app, "HEADERS_FILE", tmp_path / "ytmusic_headers.json")
    monkeypatch.setattr(cli_app, "OAUTH_FILE", tmp_path / "ytmusic_oauth.json")

    assert _has_saved_auth(Platform.SPOTIFY) is True


def test_clear_saved_auth_removes_ytmusic_files(monkeypatch, tmp_path: Path) -> None:
    import playlist_sync.cli.app as cli_app

    headers_file = tmp_path / "ytmusic_headers.json"
    oauth_file = tmp_path / "ytmusic_oauth.json"
    headers_file.write_text("headers")
    oauth_file.write_text("oauth")

    deleted_tokens: list[str] = []
    monkeypatch.setattr(cli_app, "delete_token", lambda name: deleted_tokens.append(name))
    monkeypatch.setattr(cli_app, "HEADERS_FILE", headers_file)
    monkeypatch.setattr(cli_app, "OAUTH_FILE", oauth_file)

    _clear_saved_auth(Platform.YTMUSIC)

    assert deleted_tokens == []
    assert not headers_file.exists()
    assert not oauth_file.exists()


def test_run_async_raises_click_exception_on_runtime_error() -> None:
    from playlist_sync.cli.app import _run_async

    async def raises() -> None:
        raise RuntimeError("token expired")

    try:
        _run_async(raises())
    except click.ClickException as exc:
        assert str(exc) == "token expired"
    else:
        raise AssertionError("expected ClickException")


def test_playlist_list_reports_auth_error_without_traceback(monkeypatch) -> None:
    import playlist_sync.cli.app as cli_app

    class BrokenPlatform:
        async def authenticate(self) -> None:
            raise RuntimeError("Spotify token expired")

    monkeypatch.setattr(cli_app, "_make_platform", lambda platform: BrokenPlatform())

    runner = CliRunner()
    result = runner.invoke(cli_app.cli, ["playlists", "list", "spotify"])

    assert result.exit_code != 0
    assert "Error: Spotify token expired" in result.output
    assert "Traceback" not in result.output
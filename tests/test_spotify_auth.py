"""Tests for Spotify auth state helpers."""
from __future__ import annotations

from playlist_sync.platforms.spotify import _raise_spotify_runtime_error, has_usable_saved_auth, validate_bearer_token


def test_has_usable_saved_auth_clears_expired_bearer_token(monkeypatch) -> None:
    import playlist_sync.platforms.spotify as spotify

    monkeypatch.setattr(
        spotify,
        "load_token",
        lambda name: {"access_token": "expired", "expires_at": 0} if name == "spotify_spdc" else None,
    )
    deleted: list[str] = []
    monkeypatch.setattr(spotify, "delete_token", lambda name: deleted.append(name))

    assert has_usable_saved_auth() is False
    assert deleted == ["spotify_spdc"]


def test_has_usable_saved_auth_accepts_oauth_cache(monkeypatch) -> None:
    import playlist_sync.platforms.spotify as spotify

    monkeypatch.setattr(spotify, "load_token", lambda name: {"cached": True} if name == "spotify" else None)
    monkeypatch.setattr(spotify, "delete_token", lambda name: None)

    assert has_usable_saved_auth() is True


def test_validate_bearer_token_raises_clear_message_on_429(monkeypatch) -> None:
    import playlist_sync.platforms.spotify as spotify

    class FakeClient:
        def current_user(self) -> None:
            raise spotify.spotipy.SpotifyException(429, -1, "rate limited")

    monkeypatch.setattr(spotify.BearerAuthManager, "get_access_token", lambda self, as_dict=True: {"access_token": "token"})
    monkeypatch.setattr(spotify.spotipy, "Spotify", lambda **kwargs: FakeClient())

    try:
        validate_bearer_token({"access_token": "token", "expires_at": 9999999999})
    except RuntimeError as exc:
        assert "web-player token is being rate limited" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_raise_spotify_runtime_error_explains_bearer_token_429(monkeypatch) -> None:
    import playlist_sync.platforms.spotify as spotify

    monkeypatch.setattr(spotify, "load_token", lambda name: {"token": True} if name == "spotify_spdc" else None)

    try:
        _raise_spotify_runtime_error(spotify.spotipy.SpotifyException(429, -1, "rate limited"))
    except RuntimeError as exc:
        assert "Reconnect with Spotify OAuth" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_raise_spotify_runtime_error_explains_premium_required_403() -> None:
    import playlist_sync.platforms.spotify as spotify

    try:
        _raise_spotify_runtime_error(
            spotify.spotipy.SpotifyException(
                403,
                -1,
                "Active premium subscription required for the owner of the app.",
            )
        )
    except RuntimeError as exc:
        assert "Spotify OAuth is connected" in str(exc)
        assert "owner of the Spotify app configured in .env needs an active Premium subscription" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
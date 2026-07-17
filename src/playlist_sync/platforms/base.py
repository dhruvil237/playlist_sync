"""Abstract base class for all streaming platform adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from playlist_sync.core.models import Platform, Playlist, Track


class BasePlatform(ABC):
    """All platform adapters must implement this interface."""

    platform: Platform

    @abstractmethod
    async def authenticate(self) -> None:
        """Authenticate with the platform (OAuth flow or API key)."""

    @abstractmethod
    async def get_playlists(self) -> list[Playlist]:
        """Return all playlists owned by the authenticated user."""

    @abstractmethod
    async def get_playlist(self, playlist_id: str) -> Playlist:
        """Return a single playlist with its tracks."""

    @abstractmethod
    async def search_track(self, query: str, limit: int = 5) -> list[Track]:
        """Search for tracks matching a query string. Returns up to `limit` candidates."""

    async def batch_search_tracks(
        self,
        queries: list[str],
        *,
        limit: int = 5,
        workers: int = 1,
    ) -> list[list[Track]]:
        """Search for many query strings. The default implementation falls back to sequential single-query searches."""
        return [await self.search_track(query, limit=limit) for query in queries]

    @abstractmethod
    async def create_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        """Create a new empty playlist and return it."""

    @abstractmethod
    async def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Add tracks (by platform-specific IDs) to an existing playlist."""

    @abstractmethod
    async def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Remove tracks from a playlist."""

    @abstractmethod
    async def get_liked_songs(self) -> list[Track]:
        """Return all tracks in the user's liked/saved songs library."""

    @abstractmethod
    async def like_tracks(self, track_ids: list[str]) -> None:
        """Add tracks to the user's liked/saved songs library."""

    async def get_playlist_by_name(self, name: str) -> Optional[Playlist]:
        """Find a playlist by name (case-insensitive). Returns None if not found."""
        playlists = await self.get_playlists()
        for pl in playlists:
            if pl.name.lower() == name.lower():
                return pl
        return None

    async def find_or_create_playlist(self, name: str, description: str = "") -> Playlist:
        """Return an existing playlist by name, or create one if it doesn't exist."""
        existing = await self.get_playlist_by_name(name)
        if existing is not None:
            return existing
        return await self.create_playlist(name, description)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(platform={self.platform.value})"

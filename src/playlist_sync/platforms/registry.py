"""Platform registry — maps string names to adapter classes."""
from __future__ import annotations

from typing import TYPE_CHECKING

from playlist_sync.core.models import Platform

if TYPE_CHECKING:
    from playlist_sync.platforms.base import BasePlatform

_REGISTRY: dict[Platform, type["BasePlatform"]] = {}


def register(cls: type["BasePlatform"]) -> type["BasePlatform"]:
    """Class decorator to register a platform adapter."""
    _REGISTRY[cls.platform] = cls
    return cls


def get_platform_class(platform: Platform) -> type["BasePlatform"]:
    if platform not in _REGISTRY:
        raise ValueError(f"No adapter registered for {platform.value}. "
                         f"Available: {[p.value for p in _REGISTRY]}")
    return _REGISTRY[platform]


def list_platforms() -> list[Platform]:
    return list(_REGISTRY.keys())


# Register built-in adapters
def _register_defaults() -> None:
    from playlist_sync.platforms.spotify import SpotifyPlatform
    from playlist_sync.platforms.ytmusic import YTMusicPlatform

    _REGISTRY[Platform.SPOTIFY] = SpotifyPlatform
    _REGISTRY[Platform.YTMUSIC] = YTMusicPlatform


_register_defaults()

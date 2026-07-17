"""Core data models shared across all platforms."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Platform(str, Enum):
    SPOTIFY = "spotify"
    YTMUSIC = "ytmusic"
    APPLE_MUSIC = "apple_music"
    TIDAL = "tidal"
    DEEZER = "deezer"


class SyncDirection(str, Enum):
    SOURCE_TO_TARGET = "source_to_target"
    TARGET_TO_SOURCE = "target_to_source"
    BIDIRECTIONAL = "bidirectional"


class ConflictStrategy(str, Enum):
    SOURCE_WINS = "source_wins"
    TARGET_WINS = "target_wins"
    NEWER_WINS = "newer_wins"
    MANUAL = "manual"


class MatchStatus(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    SKIPPED = "skipped"
    MANUAL_OVERRIDE = "manual_override"


@dataclass
class Track:
    title: str
    artists: list[str]
    album: Optional[str] = None
    duration_ms: Optional[int] = None
    isrc: Optional[str] = None          # International Standard Recording Code
    mbid: Optional[str] = None          # MusicBrainz ID
    platform_id: Optional[str] = None   # Platform-specific ID
    platform: Optional[Platform] = None
    added_at: Optional[datetime] = None

    @property
    def artist_str(self) -> str:
        return ", ".join(self.artists)

    @property
    def search_query(self) -> str:
        return f"{self.title} {self.artist_str}"

    def __repr__(self) -> str:
        return f"Track({self.title!r} by {self.artist_str!r})"


@dataclass
class Playlist:
    name: str
    tracks: list[Track] = field(default_factory=list)
    description: Optional[str] = None
    platform_id: Optional[str] = None
    platform: Optional[Platform] = None
    is_public: bool = False
    owner: Optional[str] = None
    last_modified: Optional[datetime] = None

    def __repr__(self) -> str:
        return f"Playlist({self.name!r}, {len(self.tracks)} tracks)"


@dataclass
class MatchResult:
    source_track: Track
    matched_track: Optional[Track]
    status: MatchStatus
    confidence: float = 0.0   # 0.0 - 1.0
    candidates: list[tuple[Track, float]] = field(default_factory=list)

    @property
    def is_successful(self) -> bool:
        return self.status in (MatchStatus.MATCHED, MatchStatus.MANUAL_OVERRIDE)


@dataclass
class SyncResult:
    source_platform: Platform
    target_platform: Platform
    playlist_name: str
    matched: list[MatchResult] = field(default_factory=list)
    ambiguous: list[MatchResult] = field(default_factory=list)
    not_found: list[MatchResult] = field(default_factory=list)
    skipped: list[MatchResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    run_id: Optional[int] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None

    @property
    def total(self) -> int:
        return len(self.matched) + len(self.ambiguous) + len(self.not_found) + len(self.skipped)

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return len(self.matched) / self.total

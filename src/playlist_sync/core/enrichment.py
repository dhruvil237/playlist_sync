"""MusicBrainz enrichment: alternate artist/title representations for hard-to-match tracks.

Streaming platforms often disagree on scripts ("美波" vs "Minami") or credit
names. MusicBrainz knows these as artist aliases; scoring against alias
variants lets fuzzy matching bridge representations it could never bridge
textually.
"""
from __future__ import annotations

import asyncio

import musicbrainzngs

from playlist_sync.core.models import Track

MIN_RECORDING_SCORE = 80   # ignore weak MusicBrainz search hits
MAX_ALIASES_PER_ARTIST = 3

_user_agent_set = False


def _ensure_user_agent() -> None:
    global _user_agent_set
    if not _user_agent_set:
        musicbrainzngs.set_useragent("playlist-sync", "0.1.0", "https://pypi.org/project/playlist-sync/")
        _user_agent_set = True


def _alias_names(artist: dict) -> list[str]:  # type: ignore[type-arg]
    """Alias names with primary aliases first — non-primary ones are often
    nicknames or search hints ("373" for 美波) that make poor variants."""
    primary: list[str] = []
    secondary: list[str] = []
    for alias in artist.get("alias-list") or []:
        name = (alias.get("alias") or "").strip()
        if not name:
            continue
        (primary if alias.get("primary") else secondary).append(name)
    return primary + secondary


def _variant_pairs(title: str, artists: list[str], limit: int) -> list[tuple[str, list[str]]]:
    """Return (title, artists) alternates for a track, from MusicBrainz aliases."""
    _ensure_user_agent()
    try:
        result = musicbrainzngs.search_recordings(
            recording=title, artist=", ".join(artists), limit=limit
        )
    except Exception:
        return []  # MusicBrainz being down must never break matching

    variants: list[tuple[str, list[str]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = {
        (title.lower(), tuple(a.lower() for a in artists))
    }

    def _add(variant_title: str, variant_artists: list[str]) -> None:
        key = (variant_title.lower(), tuple(a.lower() for a in variant_artists))
        if variant_artists and key not in seen:
            seen.add(key)
            variants.append((variant_title, variant_artists))

    for rec in result.get("recording-list", []):
        try:
            if int(rec.get("ext:score", 0)) < MIN_RECORDING_SCORE:
                continue
        except (TypeError, ValueError):
            continue
        rec_title = (rec.get("title") or title).strip() or title

        credit_names: list[str] = []
        alias_sets: list[list[str]] = []
        for credit in rec.get("artist-credit", []):
            if not isinstance(credit, dict) or "artist" not in credit:
                continue
            artist = credit["artist"]
            primary = (artist.get("name") or "").strip()
            if primary:
                credit_names.append(primary)
                alias_sets.append(_alias_names(artist))

        if not credit_names:
            continue

        # The canonical credit itself, then each single-alias substitution.
        _add(rec_title, credit_names)
        for index, aliases in enumerate(alias_sets):
            for alias in aliases[:MAX_ALIASES_PER_ARTIST]:
                substituted = list(credit_names)
                substituted[index] = alias
                _add(rec_title, substituted)

    return variants


async def variant_tracks(track: Track, *, limit: int = 3, max_variants: int = 4) -> list[Track]:
    """Async wrapper: alternate Track representations of `track` via MusicBrainz."""
    pairs = await asyncio.to_thread(_variant_pairs, track.title, track.artists, limit)
    return [
        Track(
            title=variant_title,
            artists=variant_artists,
            album=track.album,
            duration_ms=track.duration_ms,
            isrc=track.isrc,
        )
        for variant_title, variant_artists in pairs[:max_variants]
    ]

"""Track matcher: fuzzy matching with optional AI-powered fallback via any OpenAI-compatible API."""
from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

from openai import OpenAI
from rapidfuzz import fuzz

from playlist_sync.core.models import MatchResult, MatchStatus, Track
from playlist_sync.platforms.base import BasePlatform

# Thresholds — tune as needed
CONFIDENT_THRESHOLD = 0.85    # Auto-accept above this
AMBIGUOUS_THRESHOLD = 0.60    # Offer as candidate above this; below = not found

# Default model — overridable via AI_MODEL env var or constructor arg.
# Works with any OpenAI-compatible provider (OpenAI, Ollama, Groq, Together, etc.)
DEFAULT_AI_MODEL = "gpt-4o-mini"

SOFT_VARIANT_MARKERS = {"remaster", "remastered"}
HARD_VARIANT_MARKERS = {
    "acoustic",
    "cover",
    "edit",
    "instrumental",
    "karaoke",
    "live",
    "mix",
    "remix",
    "slowed",
    "sped up",
    "unplugged",
}


def _variant_markers(text: str) -> set[str]:
    lowered = text.lower()
    markers = {marker for marker in SOFT_VARIANT_MARKERS | HARD_VARIANT_MARKERS if marker in lowered}
    if "spedup" in lowered:
        markers.add("sped up")
    return markers


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and common noise words for cleaner comparison."""
    text = text.lower()
    # Remove feat., ft., (feat ...), [feat ...]
    text = re.sub(r"\(?feat\.?\s+[^)]*\)?", "", text)
    text = re.sub(r"\[feat\.?\s+[^\]]*\]", "", text)
    # Remove soundtrack/source annotations like (From "Movie")
    text = re.sub(r"\((?:from|theme from)\s+[^)]*\)", "", text)
    text = re.sub(r"\[(?:from|theme from)\s+[^\]]*\]", "", text)
    # Remove remaster/remastered/live/remix annotations
    text = re.sub(r"\b(remaster(ed)?|live|remix|edit|version|radio edit)\b", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _track_score(source: Track, candidate: Track) -> float:
    """Composite fuzzy similarity between two tracks (0.0 – 1.0)."""
    title_score = fuzz.token_sort_ratio(
        _normalize(source.title), _normalize(candidate.title)
    ) / 100.0

    src_artists = [_normalize(a) for a in source.artists]
    cand_artists = [_normalize(a) for a in candidate.artists]
    best_artist_match = max(
        (fuzz.token_sort_ratio(sa, ca) / 100.0 for sa in src_artists for ca in cand_artists),
        default=0.0,
    )
    source_artist_coverage = 0.0
    if src_artists and cand_artists:
        source_artist_coverage = sum(
            max((fuzz.token_sort_ratio(sa, ca) / 100.0 for ca in cand_artists), default=0.0)
            for sa in src_artists
        ) / len(src_artists)
    artist_score = 0.3 * best_artist_match + 0.7 * source_artist_coverage

    # Duration similarity (if available) — within 5 sec = perfect
    dur_score = 1.0
    if source.duration_ms and candidate.duration_ms:
        diff_sec = abs(source.duration_ms - candidate.duration_ms) / 1000
        dur_score = max(0.0, 1.0 - diff_sec / 30.0)

    # ISRC exact match overrides everything
    if source.isrc and candidate.isrc and source.isrc == candidate.isrc:
        return 1.0

    score = 0.50 * title_score + 0.35 * artist_score + 0.15 * dur_score

    source_markers = _variant_markers(source.title)
    candidate_markers = _variant_markers(candidate.title)
    extra_soft_markers = candidate_markers.difference(source_markers).intersection(SOFT_VARIANT_MARKERS)
    extra_hard_markers = candidate_markers.difference(source_markers).intersection(HARD_VARIANT_MARKERS)
    score -= 0.04 * len(extra_soft_markers)
    score -= 0.18 * len(extra_hard_markers)

    return max(0.0, min(score, 1.0))


class TrackMatcher:
    """
    Matches tracks from one platform to another.

    Matching pipeline:
      1. ISRC exact match (when available)
      2. Fuzzy score >= CONFIDENT_THRESHOLD → auto-accept
      3. Fuzzy score in [AMBIGUOUS_THRESHOLD, CONFIDENT_THRESHOLD) → ask AI
      4. AI picks the best candidate (or falls back to ambiguous/interactive)
      5. Score < AMBIGUOUS_THRESHOLD → not found

    The AI client uses the OpenAI SDK, so any OpenAI-compatible endpoint works:
      - OpenAI:    base_url=None (default), api_key=OPENAI_API_KEY
      - Ollama:    base_url="http://localhost:11434/v1", api_key="ollama", model="llama3"
      - Groq:      base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY
      - Together:  base_url="https://api.together.xyz/v1", api_key=TOGETHER_API_KEY
    """

    def __init__(
        self,
        use_ai: bool = True,
        ai_model: Optional[str] = None,
        ai_base_url: Optional[str] = None,
        ai_api_key: Optional[str] = None,
        use_musicbrainz: bool = True,
    ) -> None:
        self.use_ai = use_ai
        self.use_musicbrainz = use_musicbrainz
        self.ai_model = ai_model or os.environ.get("AI_MODEL", DEFAULT_AI_MODEL)
        self._ai_base_url = ai_base_url or os.environ.get("AI_BASE_URL")
        self._ai_api_key = ai_api_key or os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self._ai_client: Optional[OpenAI] = None
        self._ai_warned = False

    @property
    def ai_client(self) -> OpenAI:
        if self._ai_client is None:
            self._ai_client = OpenAI(
                api_key=self._ai_api_key or "no-key",  # some local providers ignore the key
                base_url=self._ai_base_url,            # None = use OpenAI's default endpoint
            )
        return self._ai_client

    async def match(self, source: Track, target_platform: BasePlatform) -> MatchResult:
        """Find the best match for `source` on `target_platform`."""
        candidates_raw = await target_platform.search_track(source.search_query, limit=5)

        result = await self._match_from_candidates(source, candidates_raw)
        if result.status == MatchStatus.NOT_FOUND:
            result = await self._not_found_fallback(source, target_platform, result)
        return result

    async def match_many(
        self,
        sources: list[Track],
        target_platform: BasePlatform,
        *,
        workers: int = 1,
    ) -> list[MatchResult]:
        """Find the best matches for many source tracks, reusing platform batch search when available."""
        candidate_lists = await target_platform.batch_search_tracks(
            [source.search_query for source in sources],
            limit=5,
            workers=workers,
        )
        results: list[MatchResult] = []
        for source, candidates_raw in zip(sources, candidate_lists):
            result = await self._match_from_candidates(source, candidates_raw)
            if result.status == MatchStatus.NOT_FOUND:
                result = await self._not_found_fallback(source, target_platform, result)
            results.append(result)
        return results

    async def _not_found_fallback(
        self,
        source: Track,
        target_platform: BasePlatform,
        primary: MatchResult,
    ) -> MatchResult:
        """Rescue pipeline for tracks fuzzy matching gave up on.

        1. MusicBrainz alias variants: platforms often credit the same artist in
           different scripts ("美波" vs "Minami"); rescoring against alias variants
           (plus one extra search per variant) bridges that.
        2. AI judgment on the pooled candidates: models know that "廻廻奇譚" and
           "Kaikai Kitan" are the same song even when no alias data exists.
        """
        pooled: dict[str, Track] = {
            (t.platform_id or repr(t)): t for t, _ in primary.candidates
        }
        best = primary

        if self.use_musicbrainz:
            from playlist_sync.core.enrichment import variant_tracks

            for variant in await variant_tracks(source):
                extra = await target_platform.search_track(variant.search_query, limit=5)
                for track in extra:
                    pooled.setdefault(track.platform_id or repr(track), track)
                if not pooled:
                    continue
                var_result = await self._match_from_candidates(variant, list(pooled.values()))
                if var_result.status in (MatchStatus.MATCHED, MatchStatus.MANUAL_OVERRIDE):
                    return MatchResult(
                        source_track=source,
                        matched_track=var_result.matched_track,
                        status=MatchStatus.MATCHED,
                        confidence=var_result.confidence,
                        candidates=var_result.candidates,
                    )
                if var_result.status == MatchStatus.AMBIGUOUS and var_result.confidence > best.confidence:
                    best = MatchResult(
                        source_track=source,
                        matched_track=var_result.matched_track,
                        status=MatchStatus.AMBIGUOUS,
                        confidence=var_result.confidence,
                        candidates=var_result.candidates,
                    )

        if best.status == MatchStatus.NOT_FOUND and self.use_ai and pooled:
            scored = [(t, _track_score(source, t)) for t in pooled.values()]
            scored.sort(key=lambda x: x[1], reverse=True)
            choice = await self._ask_ai(source, scored[:5])
            if choice is not None:
                return MatchResult(
                    source_track=source,
                    matched_track=choice,
                    status=MatchStatus.MATCHED,
                    confidence=AMBIGUOUS_THRESHOLD,  # low-confidence rescue
                    candidates=scored,
                )

        return best

    async def _match_from_candidates(self, source: Track, candidates_raw: list[Track]) -> MatchResult:
        """Apply the scoring and AI decision pipeline to a source track and a prepared candidate list."""

        if not candidates_raw:
            return MatchResult(
                source_track=source,
                matched_track=None,
                status=MatchStatus.NOT_FOUND,
                confidence=0.0,
            )

        scored: list[tuple[Track, float]] = sorted(
            [(t, _track_score(source, t)) for t in candidates_raw],
            key=lambda x: x[1],
            reverse=True,
        )

        best_track, best_score = scored[0]

        # ISRC or very high confidence
        if best_score >= CONFIDENT_THRESHOLD:
            return MatchResult(
                source_track=source,
                matched_track=best_track,
                status=MatchStatus.MATCHED,
                confidence=best_score,
                candidates=scored,
            )

        # In the ambiguous zone — try AI if enabled
        if best_score >= AMBIGUOUS_THRESHOLD:
            if self.use_ai:
                ai_result = await self._ask_ai(source, scored[:5])
                if ai_result is not None:
                    return MatchResult(
                        source_track=source,
                        matched_track=ai_result,
                        status=MatchStatus.MATCHED,
                        confidence=best_score,
                        candidates=scored,
                    )
            # Return as ambiguous for interactive resolution
            return MatchResult(
                source_track=source,
                matched_track=best_track,
                status=MatchStatus.AMBIGUOUS,
                confidence=best_score,
                candidates=scored,
            )

        return MatchResult(
            source_track=source,
            matched_track=None,
            status=MatchStatus.NOT_FOUND,
            confidence=best_score,
            candidates=scored,
        )

    async def _ask_ai(
        self, source: Track, candidates: list[tuple[Track, float]]
    ) -> Optional[Track]:
        """Use an OpenAI-compatible model to pick the best candidate when fuzzy matching is uncertain."""
        options = "\n".join(
            f"  [{i+1}] {t.title} — {t.artist_str} "
            f"(album: {t.album or 'unknown'}, dur: {(t.duration_ms or 0)//1000}s, score: {s:.2f})"
            for i, (t, s) in enumerate(candidates)
        )
        prompt = (
            f"I'm trying to match a song from one music platform to another.\n\n"
            f"SOURCE: {source.title} — {source.artist_str} "
            f"(album: {source.album or 'unknown'}, dur: {(source.duration_ms or 0)//1000}s)\n\n"
            f"CANDIDATES:\n{options}\n\n"
            f"Which candidate number is the best match? Reply with ONLY a single digit (1-{len(candidates)}) "
            f"or 'none' if none are a good match. No explanation."
        )

        try:
            # The OpenAI client is synchronous — run it off the event loop.
            # No token cap: reasoning models spend budget on reasoning first and
            # return empty content when capped; the prompt keeps replies tiny anyway.
            response = await asyncio.to_thread(
                self.ai_client.chat.completions.create,
                model=self.ai_model,
                messages=[{"role": "user", "content": prompt}],
            )
            text = (response.choices[0].message.content or "").strip().lower()
            if text.isdigit():
                idx = int(text) - 1
                if 0 <= idx < len(candidates):
                    return candidates[idx][0]
        except Exception as exc:
            # Fall through to ambiguous handling, but don't hide the failure forever.
            if not self._ai_warned:
                self._ai_warned = True
                print(f"[matcher] AI matching unavailable ({exc}); continuing without it.")

        return None

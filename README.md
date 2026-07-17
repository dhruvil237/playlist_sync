# playlist-sync

Sync your playlists between **Spotify** and **YouTube Music** — with smart track
matching, a safety net for every change, and both a CLI and a web dashboard.

Moving a playlist between platforms is harder than it sounds: the "same" song has
different titles, artist spellings, scripts (美波 vs *Minami*), and a sea of
covers, remixes, live cuts, and sped-up versions. playlist-sync deals with all of
that:

- **Layered track matching** — exact ISRC → fuzzy scoring (title/artist/duration,
  with penalties for remix/live/cover variants) → MusicBrainz artist-alias
  variants for cross-script credits → an AI model as the last-resort judge
  (it knows 廻廻奇譚 and *Kaikai Kitan* are the same song).
- **Match cache** — every resolved track is remembered across runs *and*
  playlists, so re-syncs take seconds and never re-spend AI calls.
- **Safe by design** — duplicate-proof adds, true dry-run mode, automatic
  playlist snapshots before every write, and one-command restore.
- **Reconcile ("prune")** — removes tracks from the target that you deleted from
  the source, plus outdated wrong-version picks, always with confirmation.
- **Review workflow** — inspect low-confidence matches and fix wrong versions in
  two clicks; corrections are remembered forever.
- **Resumable** — a crashed 400-track sync picks up where it left off.
- **Works without API keys** — if Spotify/Google won't give you API access,
  playlist-sync drives a real browser session instead (Playwright).

## Requirements

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Linux, macOS, or Windows via WSL2

## Install

```bash
git clone https://github.com/<you>/playlist-sync.git
cd playlist-sync
uv sync
uv run playwright install chromium   # for browser-based auth / scraping
cp .env.example .env                 # then edit .env (see below)
```

## Configure (.env)

Only configure what you need — everything has a fallback:

| Variable | Needed for | Notes |
|---|---|---|
| `OPENAI_API_KEY` (or `AI_API_KEY`) | AI matching | Any OpenAI-compatible provider works |
| `AI_MODEL` | AI matching | e.g. `gpt-4o-mini`; default in `.env.example` |
| `AI_BASE_URL` | AI matching | Set for Ollama/Groq/Together; blank = OpenAI |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | Spotify OAuth (optional) | See caveat below |
| `YTMUSIC_CLIENT_ID` / `YTMUSIC_CLIENT_SECRET` | YT Music OAuth (optional) | Headers/browser auth works without |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | Web UI | Defaults `127.0.0.1:8765` |

No AI key? Everything still works — ambiguous matches just fall back to
interactive prompts (or get reported for review) instead of being auto-resolved.

## Connect your accounts

The simplest path — one command per platform, log in when the browser opens:

```bash
uv run playlist-sync auth login spotify
uv run playlist-sync auth login ytmusic
uv run playlist-sync auth status     # both should show "present"
```

This stores a logged-in browser profile (and, for YT Music, auto-captures API
headers from it — no DevTools needed). Sessions survive across runs; if one
expires, just run `auth login` again.

<details>
<summary>Alternative auth methods (OAuth apps, manual header paste)</summary>

- **Spotify OAuth**: create an app at
  [developer.spotify.com](https://developer.spotify.com/dashboard), set the
  redirect URI to `http://127.0.0.1:8765/auth/spotify/callback`, put the client
  ID/secret in `.env`, then connect from the web UI's Connections page.
  ⚠️ Spotify now requires the app owner to have **Premium** for most API
  endpoints — if you hit `403 Active premium subscription required`,
  playlist-sync automatically falls back to browser mode.
- **YT Music OAuth**: Google Cloud project → enable *YouTube Data API v3* →
  OAuth credentials of type *TVs and Limited Input devices* → put ID/secret in
  `.env`.
- **Manual headers**: both platforms accept a pasted `Copy as cURL` snippet from
  your browser's DevTools Network tab — the web UI's Connections page walks you
  through it step by step.
</details>

## Use it

### Web UI (recommended)

```bash
uv run playlist-sync serve
# open http://127.0.0.1:8765
```

| Page | What it does |
|---|---|
| **Connections** | Connect/disconnect Spotify and YT Music |
| **Sync** | Pick source → target, choose a playlist or Liked Songs, dry-run toggle, AI toggle, and **Prune** (remove stale target tracks after syncing, with a confirmation list) |
| **Review** | Low-confidence matches — click **Fix** to pick the correct version from live search results; corrections are permanent |
| **Snapshots** | Every pre-write playlist state, each with one-click **Restore** |
| **History** | Past sync runs and their outcomes |

### CLI

```bash
# Preview without writing anything
uv run playlist-sync sync "My Playlist" --from spotify --to ytmusic --dry-run

# Real sync (interactive prompts for ambiguous matches)
uv run playlist-sync sync "My Playlist" --from spotify --to ytmusic

# Sync + remove tracks that no longer belong on the target
uv run playlist-sync sync "My Playlist" --from spotify --to ytmusic --prune

# Liked/saved songs
uv run playlist-sync sync-liked --from spotify --to ytmusic

# Housekeeping
uv run playlist-sync playlists list spotify      # list playlists
uv run playlist-sync reconcile "My Playlist" --from spotify --to ytmusic --dry-run
uv run playlist-sync review --from spotify --to ytmusic   # fix wrong versions
uv run playlist-sync snapshots list
uv run playlist-sync snapshots restore 3         # undo a bad change
uv run playlist-sync history                     # past runs
```

Useful flags for `sync`: `--dry-run`, `--prune`, `--no-interactive`, `--no-ai`,
`--no-cache`, `--no-musicbrainz`, `--workers N`, `--ai-model`, `--ai-base-url`.

### A typical workflow

```bash
uv run playlist-sync sync "Mainstream songs" --from spotify --to ytmusic --dry-run  # preview
uv run playlist-sync sync "Mainstream songs" --from spotify --to ytmusic --prune    # sync + clean
uv run playlist-sync review --from spotify --to ytmusic                             # fix any wrong versions
uv run playlist-sync sync "Mainstream songs" --from spotify --to ytmusic --prune    # apply corrections
```

## How matching works

1. **ISRC** exact match when both platforms expose it.
2. **Fuzzy scoring**: normalized title (feat./remaster/soundtrack annotations
   stripped) + artist coverage + duration, with penalties for variant markers
   (live, remix, acoustic, sped up, …). ≥ 0.85 auto-accepts.
3. **AI disambiguation** for the 0.60–0.85 band: the model picks among the top
   candidates or declines.
4. **Not-found rescue**: MusicBrainz artist aliases generate alternate
   representations (e.g. 美波 → *Minami*) that are re-searched and re-scored;
   if that fails, the AI judges the pooled candidates directly.
5. Everything successful lands in the **match cache** (`track_mappings`) and is
   never computed again — corrections you make in Review live there too.

Sync history, per-track resume state, mappings, and snapshots are stored in
SQLite at `~/.config/playlist-sync/history.db`. Auth material lives in
`~/.config/playlist-sync/` — keep that directory private.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `403 Active premium subscription required` (Spotify) | Expected with Spotify's dev-app policy — playlist-sync auto-falls back to browser mode. Nothing to do. |
| "browser profile is not logged in" | Run `uv run playlist-sync auth login <platform>` and finish the login in the opened window. |
| Spotify rate-limited (`429`) after cURL-token connect | That token type is heavily limited; use `auth login` (browser) or OAuth instead. |
| A song synced to the wrong version | `uv run playlist-sync review --from … --to …`, pick the right one, then sync with `--prune`. |
| A sync/prune did something you regret | `uv run playlist-sync snapshots list` → `snapshots restore <id>`. |
| Browser window never opens (WSL2) | Ensure WSLg is available (`wsl --update`), or set `PLAYLIST_SYNC_BROWSER_PATH` to a Linux browser binary. |

## Development

```bash
uv run pytest            # 90 tests, all offline
uv run ruff check src tests
uv run mypy src          # strict mode
```

Architecture: platform adapters (`platforms/`, subclass `BasePlatform` and
register in `registry.py` to add a service) → matching (`core/matcher.py`,
`core/enrichment.py`) → orchestration (`core/syncer.py`, `core/reconciler.py`)
→ storage (`storage/`) → interfaces (`cli/`, `web/`).

Adding a platform = implementing one class: auth, search, playlist CRUD, likes.
The `Platform` enum already has slots for Apple Music, Tidal, and Deezer.

## License

MIT — see [LICENSE](LICENSE).

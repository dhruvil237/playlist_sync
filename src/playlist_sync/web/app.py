"""NiceGUI web UI — auth, sync, history, all in the browser."""
from __future__ import annotations

import asyncio
import os
import re


def _curl_to_headers_raw(curl: str) -> str:
    """Extract headers from a 'Copy as cURL (bash)' snippet into key: value lines."""
    lines: list[str] = []

    # -H 'key: value' or -H "key: value"
    for match in re.finditer(r"""-H\s+['"]([^'"]+)['"]""", curl):
        lines.append(match.group(1))

    # -b 'cookie_value' → cookie: cookie_value  (Brave/Chrome use this for cookies)
    for match in re.finditer(r"""-b\s+['"]([^'"]+)['"]""", curl):
        lines.append(f"cookie: {match.group(1)}")

    if not lines:
        # Might already be plain headers — return as-is
        return curl
    return "\n".join(lines)

from dotenv import load_dotenv
from nicegui import app, ui
from starlette.requests import Request
from starlette.responses import RedirectResponse

load_dotenv()

PORT = int(os.environ.get("DASHBOARD_PORT", 8765))
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
SPOTIFY_REDIRECT = f"http://{HOST}:{PORT}/auth/spotify/callback"

# ── Spotify OAuth callback ────────────────────────────────────────────────────

@app.get("/auth/spotify/callback")
async def spotify_callback(code: str = "", error: str = "") -> RedirectResponse:
    if error or not code:
        return RedirectResponse("/?notify=spotify_error")
    try:
        from playlist_sync.platforms.spotify import SCOPES as SPOTIFY_SCOPES
        from playlist_sync.storage.token_store import load_token, save_token
        import spotipy

        class _Cache(spotipy.CacheHandler):
            def get_cached_token(self): return load_token("spotify")  # type: ignore[override]
            def save_token_to_cache(self, t): save_token("spotify", t)  # type: ignore[override]

        from spotipy.oauth2 import SpotifyOAuth
        auth_manager = SpotifyOAuth(
            client_id=os.environ.get("SPOTIFY_CLIENT_ID", ""),
            client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
            redirect_uri=SPOTIFY_REDIRECT,
            scope=" ".join(SPOTIFY_SCOPES),
            cache_handler=_Cache(),
            open_browser=False,
        )
        auth_manager.get_access_token(code, as_dict=True, check_cache=False)
        return RedirectResponse("/?notify=spotify_ok")
    except Exception as e:
        print(f"[spotify callback error] {e}")
        return RedirectResponse("/?notify=spotify_error")


# ── YTMusic headers receive endpoint ─────────────────────────────────────────

_ytmusic_received: dict = {"done": False}

@app.post("/auth/ytmusic/receive")
async def ytmusic_receive(request: Request) -> dict:
    try:
        headers_raw = (await request.body()).decode()
        from playlist_sync.platforms.ytmusic import HEADERS_FILE
        import ytmusicapi as _ytmusicapi
        HEADERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ytmusicapi.setup(filepath=str(HEADERS_FILE), headers_raw=_curl_to_headers_raw(headers_raw))
        _ytmusic_received["done"] = True
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nav(current: str) -> None:
    pages = [
        ("link", "Connections", "/"),
        ("sync", "Sync",        "/sync"),
        ("rate_review", "Review", "/review"),
        ("settings_backup_restore", "Snapshots", "/snapshots"),
        ("history", "History",  "/history"),
        ("settings", "Settings","/settings"),
    ]
    with ui.left_drawer(value=True, fixed=True).style(
        "background:#12121f; padding:20px 12px; border-right:1px solid #2d2d4e"
    ):
        ui.label("🎵 playlist-sync").style(
            "color:#a78bfa; font-size:1.15rem; font-weight:700; margin-bottom:28px; display:block"
        )
        for icon, label, path in pages:
            active = current == path
            with ui.row().classes("items-center gap-3 cursor-pointer").style(
                f"padding:10px 14px; border-radius:10px; width:100%; margin-bottom:4px;"
                f"background:{'#7c3aed' if active else 'transparent'};"
                f"color:{'white' if active else '#94a3b8'};"
            ).on("click", lambda p=path: ui.navigate.to(p)):
                ui.icon(icon).style("font-size:1.1rem")
                ui.label(label)


def _card(**kwargs) -> ui.card:  # type: ignore[type-arg]
    return ui.card().style(
        "background:#1a1a2e; border:1px solid #2d2d4e; border-radius:16px; padding:24px"
        + (f"; {kwargs.get('style', '')}" if kwargs.get("style") else "")
    )


def _heading(text: str) -> None:
    ui.label(text).style("font-size:1.8rem; font-weight:700; color:white; margin-bottom:4px")


# ── Connections page (/) ──────────────────────────────────────────────────────

@ui.page("/")
async def connections_page(notify: str = "") -> None:
    from playlist_sync.storage.token_store import has_token, delete_token
    from playlist_sync.platforms.spotify import has_usable_saved_auth as has_usable_spotify_auth
    from playlist_sync.platforms.ytmusic import HEADERS_FILE, OAUTH_FILE

    ui.dark_mode().enable()
    _nav("/")

    with ui.column().classes("w-full p-8 gap-6"):
        _heading("Connections")
        ui.label("Connect your streaming accounts to start syncing.").style("color:#6b7280")

        if notify == "spotify_ok":
            ui.notify("Spotify connected!", type="positive", position="top")
        elif notify == "spotify_error":
            ui.notify(
                "Spotify auth failed. Make sure your redirect URI in the Spotify dashboard "
                f"is set to:  {SPOTIFY_REDIRECT}",
                type="negative", position="top", timeout=8000,
            )

        with ui.row().classes("gap-6 flex-wrap items-start"):
            _spotify_card(has_token, delete_token, has_usable_spotify_auth)
            _ytmusic_card(has_token, delete_token, HEADERS_FILE, OAUTH_FILE)


def _spotify_card(has_token, delete_token, has_usable_spotify_auth) -> None:  # type: ignore[no-untyped-def]
    connected = has_usable_spotify_auth()

    with _card():
        with ui.row().classes("items-center gap-3 mb-1").style("min-width:300px"):
            ui.image(
                "https://upload.wikimedia.org/wikipedia/commons/thumb/1/19/"
                "Spotify_logo_without_text.svg/168px-Spotify_logo_without_text.svg.png"
            ).style("width:36px;height:36px")
            ui.label("Spotify").style("color:white; font-size:1.1rem; font-weight:600")
            ui.icon("circle", color="green" if connected else "grey").style(
                "font-size:0.75rem; margin-left:auto"
            )

        ui.label("Connected" if connected else "Not connected").style(
            f"color:{'#4ade80' if connected else '#6b7280'}; font-size:0.82rem; margin-bottom:14px"
        )

        if connected:
            def _disconnect() -> None:
                delete_token("spotify")
                delete_token("spotify_spdc")
                ui.navigate.to("/")
            ui.button("Disconnect", color="red", on_click=_disconnect).classes("w-full")
        else:
            def _open_spdc_dialog() -> None:
                with ui.dialog() as dlg, ui.card().style(
                    "background:#1a1a2e; border:1px solid #1db954; "
                    "border-radius:16px; padding:28px; min-width:560px; max-width:640px"
                ):
                    with ui.row().classes("items-center justify-between mb-4"):
                        ui.label("Connect Spotify").style(
                            "color:#1db954; font-size:1.1rem; font-weight:700"
                        )
                        ui.button(icon="close", on_click=dlg.close).props("flat round dense color=grey")

                    ui.label("Use Spotify OAuth if possible. The copied web-player cURL token is a fallback and can be rate limited immediately.").style(
                        "color:#fbbf24; font-size:0.84rem; margin-bottom:12px"
                    )

                    def _start_spotify_oauth() -> None:
                        from spotipy.oauth2 import SpotifyOAuth
                        auth_manager = SpotifyOAuth(
                            client_id=os.environ.get("SPOTIFY_CLIENT_ID", ""),
                            client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
                            redirect_uri=SPOTIFY_REDIRECT,
                            scope=" ".join(__import__("playlist_sync.platforms.spotify", fromlist=["SCOPES"]).SCOPES),
                            open_browser=False,
                        )
                        ui.navigate.to(auth_manager.get_authorize_url())

                    if os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET"):
                        ui.button("Connect with Spotify OAuth", on_click=_start_spotify_oauth).classes("w-full").style(
                            "background:#1db954; color:black; font-weight:700; border-radius:8px; margin-bottom:14px"
                        )
                        ui.separator().style("margin:6px 0 14px 0")

                    # Step 1
                    with ui.row().classes("items-start gap-3 mb-4"):
                        ui.label("1").style(
                            "background:#1db954; color:black; border-radius:50%; "
                            "width:24px; height:24px; text-align:center; line-height:24px; "
                            "font-weight:700; flex-shrink:0"
                        )
                        with ui.column().classes("gap-1"):
                            ui.label("Open Spotify in your browser and make sure you're logged in").style("color:white; font-size:0.9rem")
                            ui.button(
                                "Open open.spotify.com ↗",
                                on_click=lambda: ui.run_javascript(
                                    "window.open('https://open.spotify.com', '_blank')"
                                ),
                            ).props("outline").style("color:#1db954; border-color:#1db954; margin-top:4px")

                    # Step 2
                    with ui.row().classes("items-start gap-3 mb-4"):
                        ui.label("2").style(
                            "background:#1db954; color:black; border-radius:50%; "
                            "width:24px; height:24px; text-align:center; line-height:24px; "
                            "font-weight:700; flex-shrink:0"
                        )
                        with ui.column().classes("gap-1"):
                            ui.label("Copy a request as cURL from DevTools (fallback only)").style("color:white; font-size:0.9rem")
                            ui.html(
                                "<div style='color:#94a3b8; font-size:0.83rem; line-height:1.8'>"
                                "Press <kbd style='background:#2d2d4e;padding:1px 5px;border-radius:4px'>F12</kbd> "
                                "→ <b style='color:#e2e8f0'>Network</b> tab "
                                "→ Click <b style='color:#e2e8f0'>Fetch/XHR</b> filter button "
                                "→ In the search box type <kbd style='background:#2d2d4e;padding:1px 5px;border-radius:4px'>spclient</kbd> "
                                "(or interact with the page to trigger requests) "
                                "→ Click any result → check it has <b style='color:#1db954'>Authorization: Bearer BQA…</b> in Request Headers "
                                "→ Right-click → <b style='color:#1db954'>Copy → Copy as cURL (bash)</b>"
                                "<br><span style='color:#fbbf24'>Warning: these copied Spotify web-player tokens are often rate limited even on the first API call. OAuth above is the reliable path.</span>"
                                "</div>"
                            )

                    # Step 3
                    with ui.row().classes("items-start gap-3 mb-2"):
                        ui.label("3").style(
                            "background:#1db954; color:black; border-radius:50%; "
                            "width:24px; height:24px; text-align:center; line-height:24px; "
                            "font-weight:700; flex-shrink:0"
                        )
                        ui.label("Paste the cURL command below and click Connect").style("color:white; font-size:0.9rem")

                    paste_area = ui.textarea(
                        placeholder="curl 'https://spclient.wg.spotify.com/...' \\\n  -H 'authorization: Bearer BQA...' \\\n  ..."
                    ).classes("w-full").style(
                        "font-family:monospace; font-size:0.75rem; min-height:120px; "
                        "background:#0f0f1a; color:#94a3b8; border-radius:8px; margin-bottom:8px"
                    )
                    error_lbl = ui.label("").style("color:#f87171; font-size:0.8rem; margin-bottom:4px")

                    async def _save() -> None:
                        raw = paste_area.value.strip()
                        if not raw:
                            error_lbl.set_text("Please paste the cURL command first.")
                            return
                        error_lbl.set_text("")
                        connect_btn.props("loading")
                        try:
                            from playlist_sync.platforms.spotify import parse_spotify_curl, validate_bearer_token
                            from playlist_sync.storage.token_store import save_token
                            token_data = parse_spotify_curl(raw)
                            validate_bearer_token(token_data)
                            save_token("spotify_spdc", token_data)
                            dlg.close()
                            ui.notify("Spotify connected!", type="positive")
                            ui.navigate.to("/")
                        except Exception as e:
                            error_lbl.set_text(f"Could not connect — {e}")
                        finally:
                            connect_btn.props(remove="loading")

                    with ui.row().classes("gap-2 justify-end"):
                        ui.button("Cancel", on_click=dlg.close).props("flat color=grey")
                        connect_btn = ui.button("Connect", on_click=_save).style(
                            "background:#1db954; color:black; font-weight:700"
                        )

                dlg.open()

            ui.button("Connect Spotify", on_click=_open_spdc_dialog).classes("w-full").style(
                "background:#1db954; color:black; font-weight:700; border-radius:8px"
            )


def _ytmusic_card(has_token, delete_token, HEADERS_FILE, OAUTH_FILE) -> None:  # type: ignore[no-untyped-def]
    connected = HEADERS_FILE.exists() or OAUTH_FILE.exists()

    with _card():
        with ui.row().classes("items-center gap-3 mb-1").style("min-width:300px"):
            ui.image(
                "https://upload.wikimedia.org/wikipedia/commons/thumb/6/6a/"
                "Youtube_Music_icon.svg/512px-Youtube_Music_icon.svg.png"
            ).style("width:36px;height:36px")
            ui.label("YouTube Music").style("color:white; font-size:1.1rem; font-weight:600")
            ui.icon("circle", color="green" if connected else "grey").style(
                "font-size:0.75rem; margin-left:auto"
            )

        ui.label("Connected" if connected else "Not connected").style(
            f"color:{'#4ade80' if connected else '#6b7280'}; font-size:0.82rem; margin-bottom:14px"
        )

        if connected:
            def _disconnect() -> None:
                HEADERS_FILE.unlink(missing_ok=True)
                OAUTH_FILE.unlink(missing_ok=True)
                ui.navigate.to("/")
            ui.button("Disconnect", color="red", on_click=_disconnect).classes("w-full")
        else:
            def _open_dialog() -> None:
                with ui.dialog() as dlg, ui.card().style(
                    "background:#1a1a2e; border:1px solid #7c3aed; "
                    "border-radius:16px; padding:28px; min-width:560px; max-width:620px"
                ):
                    with ui.row().classes("items-center justify-between mb-4"):
                        ui.label("Connect YouTube Music").style(
                            "color:#a78bfa; font-size:1.1rem; font-weight:700"
                        )
                        ui.button(icon="close", on_click=dlg.close).props("flat round dense color=grey")

                    # Step 1 — open YTM
                    with ui.row().classes("items-start gap-3 mb-4"):
                        ui.label("1").style(
                            "background:#7c3aed; color:white; border-radius:50%; "
                            "width:24px; height:24px; text-align:center; line-height:24px; "
                            "font-weight:700; flex-shrink:0"
                        )
                        with ui.column().classes("gap-1"):
                            ui.label("Open YouTube Music and make sure you're logged in").style("color:white; font-size:0.9rem")
                            ui.button(
                                "Open YouTube Music ↗",
                                on_click=lambda: ui.run_javascript(
                                    "window.open('https://music.youtube.com', '_blank')"
                                ),
                            ).props("outline").style("color:#ff4444; border-color:#ff4444; margin-top:4px")

                    # Step 2 — copy headers
                    with ui.row().classes("items-start gap-3 mb-4"):
                        ui.label("2").style(
                            "background:#7c3aed; color:white; border-radius:50%; "
                            "width:24px; height:24px; text-align:center; line-height:24px; "
                            "font-weight:700; flex-shrink:0"
                        )
                        with ui.column().classes("gap-1"):
                            ui.label("Copy request as cURL from DevTools").style("color:white; font-size:0.9rem")
                            ui.html(
                                "<div style='color:#94a3b8; font-size:0.83rem; line-height:1.8'>"
                                "Press <kbd style='background:#2d2d4e;padding:1px 5px;border-radius:4px'>F12</kbd> "
                                "→ <b style='color:#e2e8f0'>Network</b> tab "
                                "→ Check <b style='color:#e2e8f0'>Disable cache</b> "
                                "→ Reload the page (Ctrl+R) "
                                "→ In the filter box type <kbd style='background:#2d2d4e;padding:1px 5px;border-radius:4px'>youtubei/v1</kbd> "
                                "→ Right-click any <b style='color:#a78bfa'>XHR/Fetch</b> result (not .js or .png) "
                                "→ <b style='color:#a78bfa'>Copy → Copy as cURL (bash)</b>"
                                "</div>"
                            )

                    # Step 3 — paste
                    with ui.row().classes("items-start gap-3 mb-2"):
                        ui.label("3").style(
                            "background:#7c3aed; color:white; border-radius:50%; "
                            "width:24px; height:24px; text-align:center; line-height:24px; "
                            "font-weight:700; flex-shrink:0"
                        )
                        ui.label("Paste the cURL command below and click Connect").style("color:white; font-size:0.9rem")

                    paste_area = ui.textarea(placeholder="curl 'https://music.youtube.com/youtubei/v1/...' \\\n  -H 'cookie: ...' \\\n  -H 'authorization: SAPISIDHASH ...' \\\n  ...").classes("w-full").style(
                        "font-family:monospace; font-size:0.75rem; min-height:140px; "
                        "background:#0f0f1a; color:#94a3b8; border-radius:8px; margin-bottom:8px"
                    )

                    error_lbl = ui.label("").style("color:#f87171; font-size:0.8rem; margin-bottom:4px")

                    async def _save() -> None:
                        raw = paste_area.value.strip()
                        if not raw:
                            error_lbl.set_text("Please paste the cURL command first.")
                            return
                        error_lbl.set_text("")
                        connect_btn.props("loading")
                        try:
                            import ytmusicapi as _ytmusicapi
                            from ytmusicapi import YTMusic
                            import asyncio as _aio
                            headers_raw = _curl_to_headers_raw(raw)
                            HEADERS_FILE.parent.mkdir(parents=True, exist_ok=True)
                            _ytmusicapi.setup(filepath=str(HEADERS_FILE), headers_raw=headers_raw)
                            # Validate the session is actually authenticated
                            client = YTMusic(str(HEADERS_FILE))
                            try:
                                await _aio.wait_for(_aio.to_thread(client.get_account_info), timeout=10)
                            except Exception:
                                # get_account_info failed → cookies are not authenticated
                                HEADERS_FILE.unlink(missing_ok=True)
                                error_lbl.set_text(
                                    "Headers saved but session is NOT logged in. "
                                    "Make sure you copy from a music.youtube.com/youtubei/v1/ "
                                    "request (not a .js file). Try filtering by 'youtubei/v1' "
                                    "in the Network tab."
                                )
                                return
                            dlg.close()
                            ui.notify("YouTube Music connected!", type="positive")
                            ui.navigate.to("/")
                        except Exception as e:
                            error_lbl.set_text(f"Could not parse cURL — {e}")
                        finally:
                            connect_btn.props(remove="loading")

                    with ui.row().classes("gap-2 justify-end"):
                        ui.button("Cancel", on_click=dlg.close).props("flat color=grey")
                        connect_btn = ui.button("Connect", on_click=_save).style(
                            "background:#7c3aed; color:white"
                        )

                dlg.open()

            ui.button("Connect YouTube Music", on_click=_open_dialog).classes("w-full").style(
                "background:#ff0000; color:white; border-radius:8px"
            )


# ── Sync page (/sync) ─────────────────────────────────────────────────────────

@ui.page("/sync")
async def sync_page() -> None:
    ui.dark_mode().enable()
    _nav("/sync")

    platform_map = {"Spotify": "spotify", "YouTube Music": "ytmusic"}
    # playlist name → playlist_id (for source platform)
    playlists_cache: dict[str, str] = {}
    resolve_result: dict = {"track": None}
    resolve_ready = asyncio.Event()

    with ui.column().classes("w-full p-8 gap-6"):
        _heading("Sync Playlists")

        # ── Config card ───────────────────────────────────────────────────────
        with _card().style("max-width:600px; width:100%"):
            with ui.row().classes("gap-4 w-full"):
                src = ui.select(
                    ["Spotify", "YouTube Music"], label="From", value="Spotify"
                ).classes("flex-1")
                tgt = ui.select(
                    ["Spotify", "YouTube Music"], label="To", value="YouTube Music"
                ).classes("flex-1")

            ui.separator().style("margin:4px 0")

            sync_type = ui.select(
                ["Playlist", "Liked Songs"], label="What to sync", value="Playlist"
            ).classes("w-full")

            # Playlist picker row — visible only for Playlist mode
            with ui.row().classes("w-full items-center gap-2") as pl_row:
                pl_select = ui.select(
                    {}, label="Select playlist", value=None
                ).classes("flex-1")
                load_btn = ui.button(icon="refresh", on_click=lambda: asyncio.ensure_future(_load_playlists())).props(
                    "flat round color=purple"
                ).tooltip("Load playlists from source platform")

            pl_row.bind_visibility_from(sync_type, "value", backward=lambda v: v == "Playlist")
            load_status = ui.label("").style("color:#6b7280; font-size:0.78rem; margin-top:-8px")
            load_status.bind_visibility_from(sync_type, "value", backward=lambda v: v == "Playlist")

            ui.separator().style("margin:4px 0")

            with ui.row().classes("gap-6"):
                dry_run = ui.checkbox("Dry run (preview only)")
                use_ai  = ui.checkbox("AI matching", value=True)
                prune   = ui.checkbox("Prune stale tracks after sync").tooltip(
                    "After syncing, remove target entries that no longer belong "
                    "(dropped from source, outdated versions, duplicates). "
                    "You confirm the removal list first; a snapshot is saved."
                )

        # ── Start button ──────────────────────────────────────────────────────
        start_btn = ui.button("Start Sync", on_click=lambda: asyncio.ensure_future(_start())).style(
            "background:#7c3aed; color:white; font-size:1rem; padding:12px 32px; "
            "border-radius:12px"
        )

        # ── Progress card ─────────────────────────────────────────────────────
        with _card().style("max-width:600px; width:100%") as prog_card:
            prog_card.set_visibility(False)
            with ui.row().classes("items-center justify-between mb-1"):
                prog_lbl = ui.label("").style("color:#94a3b8; font-size:0.85rem")
                prog_pct = ui.label("").style("color:#a78bfa; font-size:0.85rem; font-weight:600")
            prog_bar = ui.linear_progress(value=0, color="purple").style("margin-bottom:10px")
            log_view = ui.log(max_lines=500).style(
                "height:260px; background:#0f0f1a; border-radius:8px; "
                "font-family:monospace; font-size:0.78rem; color:#94a3b8"
            )

        # ── Ambiguous match dialog ────────────────────────────────────────────
        resolve_dlg = ui.dialog().props("persistent")
        with resolve_dlg:
            with ui.card().style(
                "background:#1a1a2e; border:1px solid #7c3aed; "
                "border-radius:16px; padding:24px; min-width:520px; max-width:600px"
            ):
                ui.label("Ambiguous match — pick the correct one").style(
                    "color:#a78bfa; font-weight:700; font-size:1rem; margin-bottom:4px"
                )
                source_lbl = ui.label("").style(
                    "color:#6b7280; font-size:0.82rem; margin-bottom:14px; font-style:italic"
                )
                cands_col = ui.column().classes("gap-2 w-full")
                ui.button("Skip this track", on_click=lambda: _pick(None)).props(
                    "flat color=grey size=sm"
                ).classes("mt-3")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pick(track: object) -> None:
        resolve_result["track"] = track
        resolve_dlg.close()
        resolve_ready.set()

    async def ui_resolve(match) -> object:  # type: ignore[no-untyped-def]
        src_t = match.source_track
        source_lbl.set_text(
            f'"{src_t.title}" — {src_t.artist_str}'
            + (f'  ·  {src_t.album}' if src_t.album else "")
            + f'  ·  {(src_t.duration_ms or 0)//1000}s'
        )
        cands_col.clear()
        with cands_col:
            for track, score in match.candidates[:6]:
                color = "#4ade80" if score >= 0.85 else "#f59e0b" if score >= 0.6 else "#f87171"
                t = track
                with ui.card().classes("w-full cursor-pointer").style(
                    "background:#0f0f1a; border:1px solid #2d2d4e; border-radius:8px; padding:12px"
                ).on("click", lambda tr=t: _pick(tr)):
                    with ui.row().classes("justify-between items-center"):
                        with ui.column().classes("gap-0"):
                            ui.label(track.title).style("color:white; font-weight:600; font-size:0.9rem")
                            ui.label(f"{track.artist_str}  ·  {(track.duration_ms or 0)//1000}s").style(
                                "color:#6b7280; font-size:0.78rem"
                            )
                        ui.badge(f"{score:.0%}").style(
                            f"background:{color}; color:black; font-weight:700; font-size:0.8rem"
                        )
        resolve_ready.clear()
        resolve_dlg.open()
        await resolve_ready.wait()
        return resolve_result["track"]

    async def _load_playlists() -> None:
        src_key = platform_map[src.value]
        from playlist_sync.storage.token_store import has_token
        from playlist_sync.platforms.spotify import has_usable_saved_auth as has_usable_spotify_auth
        from playlist_sync.platforms.ytmusic import HEADERS_FILE, OAUTH_FILE
        connected = (
            (src_key == "spotify" and has_usable_spotify_auth())
            or (src_key == "ytmusic" and (HEADERS_FILE.exists() or OAUTH_FILE.exists()))
        )
        if not connected:
            load_status.set_text(f"Not connected to {src.value} — go to Connections first.")
            return

        load_status.set_text("Loading playlists…")
        load_btn.props("loading")
        try:
            from playlist_sync.platforms.registry import get_platform_class
            from playlist_sync.core.models import Platform
            s = get_platform_class(Platform(src_key))()
            await s.authenticate()
            pls = await s.get_playlists()
            playlists_cache.clear()
            for pl in pls:
                playlists_cache[pl.name] = pl.platform_id or pl.name
            pl_select.options = {pid: name for name, pid in playlists_cache.items()}
            pl_select.update()
            if pls:
                pl_select.value = list(playlists_cache.values())[0]
                load_status.set_text(f"{len(pls)} playlists loaded from {src.value}")
            else:
                load_status.set_text(
                    f"No playlists found on {src.value}. "
                    "If syncing TO this platform, playlists will be created automatically."
                )
        except Exception as e:
            msg = str(e)
            if "401" in msg or "token expired" in msg.lower() or "access token expired" in msg.lower():
                load_status.set_text(
                    "Spotify session expired — go to Connections, disconnect, and paste a fresh cURL "
                    "(filter Network by 'spclient' on open.spotify.com)."
                )
            elif "429" in msg:
                if src_key == "spotify" and has_token("spotify_spdc") and not has_token("spotify"):
                    load_status.set_text(
                        "Spotify's copied web-player token is rate limited. Disconnect Spotify and reconnect using Spotify OAuth."
                    )
                else:
                    # Spotify rate limit — disable button with a countdown
                    import re as _re
                    wait = 60
                    m = _re.search(r"Retry will occur after[:\s]+(\d+)", msg)
                    if m:
                        wait = int(m.group(1)) + 5
                    load_status.set_text(f"Spotify rate-limited — retrying in {wait}s…")
                    load_btn.props("disable")
                    for remaining in range(wait, 0, -1):
                        await asyncio.sleep(1)
                        load_status.set_text(f"Spotify rate-limited — retrying in {remaining}s…")
                    load_btn.props(remove="disable")
                    load_status.set_text("Ready — click ↺ to load playlists")
            elif "Not connected" in msg or "not authenticated" in msg.lower():
                load_status.set_text(f"Not connected — go to Connections and reconnect {src.value}.")
            else:
                load_status.set_text(f"Failed to load: {e}")
        finally:
            load_btn.props(remove="loading")


    async def _start() -> None:
        src_key = platform_map[src.value]
        tgt_key = platform_map[tgt.value]
        if src_key == tgt_key:
            ui.notify("Source and target must be different platforms", type="warning")
            return
        if sync_type.value == "Playlist" and not pl_select.value:
            ui.notify("Please select a playlist (or click the refresh button to load them)", type="warning")
            return

        prog_card.set_visibility(True)
        log_view.clear()
        prog_bar.set_value(0)
        prog_pct.set_text("")
        prog_lbl.set_text("Authenticating…")
        start_btn.props("loading")

        from playlist_sync.platforms.registry import get_platform_class
        from playlist_sync.core.models import Platform
        from playlist_sync.core.syncer import Syncer

        try:
            s = get_platform_class(Platform(src_key))()
            t = get_platform_class(Platform(tgt_key))()
            await s.authenticate()
            await t.authenticate()
            log_view.push(f"✓ Authenticated with {src.value} and {tgt.value}")
        except Exception as e:
            ui.notify(f"Auth failed: {e}", type="negative")
            prog_lbl.set_text(f"Auth error: {e}")
            start_btn.props(remove="loading")
            return

        syncer = Syncer(s, t, use_ai_matching=use_ai.value)

        def on_progress(current: int, total: int, track: object) -> None:
            pct = current / total if total else 0
            prog_bar.set_value(pct)
            prog_pct.set_text(f"{pct:.0%}")
            prog_lbl.set_text(f"[{current}/{total}]  Matching…")
            log_view.push(f"[{current}/{total}] {track}")

        try:
            if sync_type.value == "Liked Songs":
                prog_lbl.set_text("Fetching liked songs…")
                result = await syncer.sync_liked_songs(
                    dry_run=dry_run.value, on_progress=on_progress, on_resolve=ui_resolve
                )
            else:
                # Use platform_id if we loaded it, else fall back to name search
                selected_id = pl_select.value
                selected_name = next(
                    (name for name, pid in playlists_cache.items() if pid == selected_id),
                    selected_id,
                )
                prog_lbl.set_text(f'Fetching "{selected_name}"…')
                result = await syncer.sync_playlist(
                    selected_name,
                    source_playlist_id=selected_id if selected_id != selected_name else None,
                    dry_run=dry_run.value, on_progress=on_progress, on_resolve=ui_resolve,
                )

            prog_bar.set_value(1)
            prog_pct.set_text("100%")
            prog_lbl.set_text("Done")
            log_view.push("─" * 50)
            log_view.push(f"  Newly matched:   {len(result.matched)}")
            log_view.push(f"  Ambiguous:       {len(result.ambiguous)}")
            log_view.push(f"  Not found:       {len(result.not_found)}")
            log_view.push(f"  Already in sync: {len(result.skipped)}")
            if result.needed_matching:
                log_view.push(f"  Match rate:      {result.success_rate:.0%} of {result.needed_matching} needing a match")
            else:
                log_view.push("  Everything already in sync")
            if result.dry_run:
                log_view.push("  [DRY RUN — nothing was written]")
            ui.notify(
                "Done — everything already in sync" if not result.needed_matching
                else f"Done — {result.success_rate:.0%} of {result.needed_matching} matched",
                type="positive" if result.success_rate >= 0.8 else "warning",
            )

            if prune.value and not result.dry_run and sync_type.value == "Playlist":
                await _run_prune(s, t, result.playlist_name)
        except Exception as e:
            log_view.push(f"ERROR: {e}")
            ui.notify(str(e), type="negative")
        finally:
            start_btn.props(remove="loading")

    async def _run_prune(src_adapter, tgt_adapter, playlist_name: str) -> None:  # type: ignore[no-untyped-def]
        from playlist_sync.core.reconciler import Reconciler

        prog_lbl.set_text("Computing prune plan…")
        reconciler = Reconciler(src_adapter, tgt_adapter)
        try:
            plan = await reconciler.plan(playlist_name)
        except Exception as e:
            log_view.push(f"PRUNE ERROR: {e}")
            ui.notify(f"Prune failed: {e}", type="negative")
            return

        if plan.is_empty:
            log_view.push("  Prune: nothing to remove — playlist is clean.")
            prog_lbl.set_text("Done")
            return

        confirmed = asyncio.Event()
        answer = {"apply": False}
        with ui.dialog().props("persistent") as prune_dlg, ui.card().style(
            "background:#1a1a2e; border:1px solid #f59e0b; border-radius:16px; "
            "padding:24px; min-width:520px; max-width:620px"
        ):
            ui.label(f"Prune {len(plan.removals)} stale tracks?").style(
                "color:#f59e0b; font-weight:700; font-size:1rem; margin-bottom:4px"
            )
            ui.label("These target entries no longer belong (dropped from source, outdated "
                     "versions, or duplicates). A snapshot is saved before removal.").style(
                "color:#94a3b8; font-size:0.82rem; margin-bottom:10px"
            )
            with ui.column().classes("gap-1 w-full").style(
                "max-height:280px; overflow-y:auto; background:#0f0f1a; "
                "border-radius:8px; padding:10px"
            ):
                for track in plan.removals:
                    ui.label(f"– {track.title} — {track.artist_str}").style(
                        "color:#e2e8f0; font-size:0.8rem"
                    )

            def _decide(apply: bool) -> None:
                answer["apply"] = apply
                prune_dlg.close()
                confirmed.set()

            with ui.row().classes("gap-2 justify-end mt-3"):
                ui.button("Keep everything", on_click=lambda: _decide(False)).props("flat color=grey")
                ui.button(f"Remove {len(plan.removals)} tracks", color="orange",
                          on_click=lambda: _decide(True))
        prune_dlg.open()
        await confirmed.wait()

        if not answer["apply"]:
            log_view.push("  Prune skipped.")
            prog_lbl.set_text("Done")
            return
        prog_lbl.set_text("Pruning…")
        removed = await reconciler.apply(plan)
        log_view.push(f"  Pruned {removed} tracks (snapshot saved — see Snapshots page).")
        prog_lbl.set_text("Done")
        ui.notify(f"Pruned {removed} tracks", type="positive")


# ── Review page (/review) ─────────────────────────────────────────────────────

@ui.page("/review")
async def review_page() -> None:
    from playlist_sync.core.matcher import _track_score
    from playlist_sync.core.models import Platform
    from playlist_sync.core.review import (
        load_low_confidence_mappings,
        mapping_source_track,
        update_mapping_target,
    )
    from playlist_sync.storage.database import create_db

    ui.dark_mode().enable()
    _nav("/review")

    session_factory = create_db()
    platform_map = {"Spotify": "spotify", "YouTube Music": "ytmusic"}
    adapter_cache: dict = {}

    with ui.column().classes("w-full p-8 gap-6"):
        _heading("Review Matches")
        ui.label("Low-confidence and AI-rescued matches — the ones most likely to be wrong "
                 "versions. Corrections update the match cache; run a sync with Prune to "
                 "apply them to the playlist.").style("color:#6b7280; max-width:640px")

        with ui.row().classes("gap-4 items-end"):
            src = ui.select(["Spotify", "YouTube Music"], label="From", value="Spotify")
            tgt = ui.select(["Spotify", "YouTube Music"], label="To", value="YouTube Music")
            threshold = ui.number(label="Below confidence", value=0.75, min=0.1, max=1.0,
                                  step=0.05, format="%.2f")
            load_btn = ui.button("Load", on_click=lambda: asyncio.ensure_future(_load()))

        results_col = ui.column().classes("gap-3 w-full")

    async def _target_adapter():  # type: ignore[no-untyped-def]
        key = platform_map[tgt.value]
        if key not in adapter_cache:
            from playlist_sync.platforms.registry import get_platform_class
            adapter = get_platform_class(Platform(key))()
            await adapter.authenticate()
            adapter_cache[key] = adapter
        return adapter_cache[key]

    async def _load() -> None:
        results_col.clear()
        load_btn.props("loading")
        try:
            mappings = load_low_confidence_mappings(
                session_factory,
                Platform(platform_map[src.value]),
                Platform(platform_map[tgt.value]),
                threshold=float(threshold.value or 0.75),
                limit=50,
            )
            with results_col:
                if not mappings:
                    ui.label("Nothing to review at this threshold. 🎉").style("color:#4ade80")
                for mapping in mappings:
                    _mapping_card(mapping)
        finally:
            load_btn.props(remove="loading")

    def _mapping_card(mapping) -> None:  # type: ignore[no-untyped-def]
        src_platform = Platform(platform_map[src.value])
        src_track = mapping_source_track(mapping, src_platform)
        with _card().style("width:100%; max-width:680px; padding:16px"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-0"):
                    ui.label(f"{src_track.title} — {src_track.artist_str}").style(
                        "color:white; font-weight:600; font-size:0.92rem")
                    ui.label(
                        f"→ {mapping.target_title} — {mapping.target_artists.replace('||', ', ')}"
                    ).style("color:#94a3b8; font-size:0.82rem")
                with ui.row().classes("items-center gap-3"):
                    color = "#f87171" if mapping.confidence < 0.6 else "#f59e0b"
                    ui.badge(f"{mapping.confidence:.0%}").style(
                        f"background:{color}; color:black; font-weight:700")
                    ui.button("Fix", on_click=lambda m=mapping, s=src_track:
                              asyncio.ensure_future(_open_fix(m, s))).props("outline color=purple")

    async def _open_fix(mapping, src_track) -> None:  # type: ignore[no-untyped-def]
        try:
            adapter = await _target_adapter()
            candidates = await adapter.search_track(src_track.search_query, limit=6)
        except Exception as e:
            ui.notify(f"Search failed: {e}", type="negative")
            return

        with ui.dialog() as dlg, ui.card().style(
            "background:#1a1a2e; border:1px solid #7c3aed; border-radius:16px; "
            "padding:24px; min-width:520px; max-width:620px"
        ):
            ui.label(f"Pick the correct match for: {src_track.title} — {src_track.artist_str}").style(
                "color:#a78bfa; font-weight:700; margin-bottom:10px")

            def _pick(track) -> None:  # type: ignore[no-untyped-def]
                if update_mapping_target(session_factory, mapping.id, track):
                    ui.notify("Mapping corrected — run a sync with Prune to apply it.",
                              type="positive")
                    dlg.close()
                    asyncio.ensure_future(_load())
                else:
                    ui.notify("Could not update mapping", type="negative")

            with ui.column().classes("gap-2 w-full"):
                for cand in candidates:
                    score = _track_score(src_track, cand)
                    current = cand.platform_id == mapping.target_platform_id
                    with ui.card().classes("w-full cursor-pointer").style(
                        "background:#0f0f1a; border:1px solid "
                        + ("#7c3aed" if current else "#2d2d4e")
                        + "; border-radius:8px; padding:10px"
                    ).on("click", lambda c=cand: _pick(c)):
                        with ui.row().classes("justify-between items-center w-full"):
                            with ui.column().classes("gap-0"):
                                ui.label(cand.title + ("  (current)" if current else "")).style(
                                    "color:white; font-size:0.88rem; font-weight:600")
                                ui.label(cand.artist_str).style("color:#6b7280; font-size:0.78rem")
                            ui.badge(f"{score:.0%}").style(
                                "background:#2d2d4e; color:#e2e8f0; font-weight:600")
            ui.button("Cancel", on_click=dlg.close).props("flat color=grey").classes("mt-2")
        dlg.open()


# ── Snapshots page (/snapshots) ───────────────────────────────────────────────

@ui.page("/snapshots")
async def snapshots_page() -> None:
    from playlist_sync.core.models import Platform
    from playlist_sync.platforms.registry import get_platform_class
    from playlist_sync.storage.database import create_db
    from playlist_sync.storage.snapshots import get_snapshot, list_snapshots, restore_snapshot

    ui.dark_mode().enable()
    _nav("/snapshots")

    session_factory = create_db()

    with ui.column().classes("w-full p-8 gap-6"):
        _heading("Snapshots")
        ui.label("Playlist states captured automatically before every sync, prune, and "
                 "restore. Restoring brings the playlist back to that exact track list.").style(
            "color:#6b7280; max-width:640px")
        rows_col = ui.column().classes("gap-2 w-full")

    def _render() -> None:
        rows_col.clear()
        snaps = list_snapshots(session_factory, limit=50)
        with rows_col:
            if not snaps:
                ui.label("No snapshots yet — run a sync and one will appear here.").style(
                    "color:#6b7280")
            for snap in snaps:
                with _card().style("width:100%; max-width:680px; padding:14px"):
                    with ui.row().classes("items-center justify-between w-full"):
                        with ui.column().classes("gap-0"):
                            ui.label(f"#{snap.id}  {snap.playlist_name}").style(
                                "color:white; font-weight:600; font-size:0.92rem")
                            ui.label(
                                f"{snap.platform} · {snap.track_count} tracks · "
                                f"{snap.taken_at:%Y-%m-%d %H:%M} · {snap.reason}"
                            ).style("color:#94a3b8; font-size:0.8rem")
                        ui.button("Restore", on_click=lambda sid=snap.id:
                                  asyncio.ensure_future(_confirm_restore(sid))).props(
                            "outline color=orange")

    async def _confirm_restore(snapshot_id: int) -> None:
        snap = get_snapshot(session_factory, snapshot_id)
        if snap is None:
            ui.notify("Snapshot not found", type="negative")
            return
        with ui.dialog() as dlg, ui.card().style(
            "background:#1a1a2e; border:1px solid #f59e0b; border-radius:16px; padding:24px"
        ):
            ui.label(f"Restore “{snap.playlist_name}” to snapshot #{snap.id}?").style(
                "color:#f59e0b; font-weight:700; margin-bottom:6px")
            ui.label(f"The playlist will be set back to exactly {snap.track_count} tracks "
                     f"as of {snap.taken_at:%Y-%m-%d %H:%M}. The current state is snapshotted "
                     "first, so this is reversible.").style("color:#94a3b8; font-size:0.84rem")

            async def _do_restore() -> None:
                dlg.close()
                try:
                    adapter = get_platform_class(Platform(snap.platform))()
                    await adapter.authenticate()
                    added, removed = await restore_snapshot(session_factory, adapter, snap)
                    ui.notify(f"Restored — +{added} added, -{removed} removed", type="positive")
                    _render()
                except Exception as e:
                    ui.notify(f"Restore failed: {e}", type="negative")

            with ui.row().classes("gap-2 justify-end mt-3"):
                ui.button("Cancel", on_click=dlg.close).props("flat color=grey")
                ui.button("Restore", color="orange",
                          on_click=lambda: asyncio.ensure_future(_do_restore()))
        dlg.open()

    _render()


# ── History page (/history) ───────────────────────────────────────────────────

@ui.page("/history")
async def history_page() -> None:
    from playlist_sync.storage.database import SyncRun, create_db

    ui.dark_mode().enable()
    _nav("/history")

    with ui.column().classes("w-full p-8 gap-6"):
        _heading("Sync History")

        with create_db()() as session:
            runs = session.query(SyncRun).order_by(SyncRun.started_at.desc()).limit(100).all()
            rows = [
                {
                    "date":     r.started_at.strftime("%Y-%m-%d %H:%M"),
                    "playlist": r.playlist_name,
                    "route":    f"{r.source_platform} → {r.target_platform}",
                    "matched":  r.matched,
                    "missing":  r.not_found,
                    "rate":     f"{r.success_rate:.0%}",
                    "dry":      "✓" if r.dry_run else "",
                }
                for r in runs
            ]

        cols = [
            {"name": "date",     "label": "Date",     "field": "date",    "sortable": True},
            {"name": "playlist", "label": "Playlist",  "field": "playlist"},
            {"name": "route",    "label": "Route",     "field": "route"},
            {"name": "matched",  "label": "Matched",   "field": "matched", "align": "right"},
            {"name": "missing",  "label": "Not found", "field": "missing", "align": "right"},
            {"name": "rate",     "label": "Rate",      "field": "rate",    "align": "right"},
            {"name": "dry",      "label": "Dry",       "field": "dry",     "align": "center"},
        ]
        ui.table(columns=cols, rows=rows, row_key="date").props("dark flat").style(
            "background:#1a1a2e; border-radius:16px; color:white; width:100%"
        )
        if not rows:
            ui.label("No history yet.").style("color:#6b7280; margin-top:8px")


# ── Settings page (/settings) ─────────────────────────────────────────────────

@ui.page("/settings")
async def settings_page() -> None:
    ui.dark_mode().enable()
    _nav("/settings")

    with ui.column().classes("w-full p-8 gap-6"):
        _heading("Settings")

        with _card().style("max-width:480px; width:100%"):
            ui.label("AI Matching").style("color:#a78bfa; font-weight:600; margin-bottom:8px")
            ui.input("Model", value=os.environ.get("AI_MODEL", "gpt-4o-mini")).classes("w-full")
            ui.input("Base URL (blank = OpenAI)", value=os.environ.get("AI_BASE_URL", ""),
                     placeholder="http://localhost:11434/v1").classes("w-full")
            ui.input("API Key", value=os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY", ""),
                     password=True).classes("w-full")

            ui.separator().style("margin:16px 0")
            ui.label("Spotify").style("color:#a78bfa; font-weight:600; margin-bottom:8px")
            ui.input("Client ID", value=os.environ.get("SPOTIFY_CLIENT_ID", "")).classes("w-full")
            ui.input("Client Secret", value=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
                     password=True).classes("w-full")
            ui.label(f"Redirect URI in use: {SPOTIFY_REDIRECT}").style(
                "color:#f59e0b; font-size:0.78rem; font-family:monospace; margin-top:4px"
            )
            ui.label("Changes shown here are read-only — edit your .env file to persist them.").style(
                "color:#6b7280; font-size:0.74rem; margin-top:6px"
            )


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server(host: str = HOST, port: int = PORT, reload: bool = False) -> None:
    ui.run(host=host, port=port, title="playlist-sync", dark=True,
           reload=reload, favicon="🎵", show=False)

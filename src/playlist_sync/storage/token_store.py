"""Token storage using a local JSON file (works on WSL2 and headless Linux)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

TOKEN_DIR = Path(os.environ.get("PLAYLIST_SYNC_TOKEN_DIR", Path.home() / ".config" / "playlist-sync" / "tokens"))


def _token_path(platform: str) -> Path:
    return TOKEN_DIR / f"{platform}.json"


def save_token(platform: str, data: dict[str, Any]) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    path = _token_path(platform)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)  # owner read/write only


def load_token(platform: str) -> Optional[dict[str, Any]]:
    path = _token_path(platform)
    if not path.exists():
        return None
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def delete_token(platform: str) -> None:
    path = _token_path(platform)
    if path.exists():
        path.unlink()


def has_token(platform: str) -> bool:
    return _token_path(platform).exists()

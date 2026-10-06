"""
Simple file-based cache for FIDE data.
Each player's data is saved as cache/{fide_id}.json
"""
import json
from pathlib import Path
from datetime import datetime

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _path(fide_id: str) -> Path:
    return CACHE_DIR / f"{fide_id}.json"


def load(fide_id: str) -> dict | None:
    """Load cached data for a player. Returns None if not cached."""
    p = _path(fide_id)
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(fide_id: str, data: dict) -> None:
    """Save player data to cache."""
    data["_cached_at"] = datetime.now().isoformat()
    with open(_path(fide_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cached_at(fide_id: str) -> str | None:
    """Return when the data was cached, or None."""
    data = load(fide_id)
    if data:
        return data.get("_cached_at")
    return None


def clear(fide_id: str) -> None:
    """Delete cached data for a player."""
    p = _path(fide_id)
    if p.exists():
        p.unlink()

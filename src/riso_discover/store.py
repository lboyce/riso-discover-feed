"""Generic rolling on-disk store: accumulate dated entries across weekly runs.

Some upstreams only expose a shallow recent window — AIPT's feed (~this week) and CBR's "current"
lists (~2 weeks). To let curated shelves span *several* weeks, we persist what we've seen and merge
each run into it (dedupe, prune past a retention window, newest-first). The store is committed by the
weekly Action so it grows over time.

Shape on disk: ``{"groups": {<group_id>: [entry, ...]}}``. Each entry is a dict with at least a
``url`` (stable key) and a date (``published`` or the auto-stamped ``first_seen``).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional


def entry_date(entry: dict) -> Optional[date]:
    raw = entry.get("published") or entry.get("first_seen")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def merge_entries(
    existing: list[dict], fresh: list[dict], today: date, retention_days: int
) -> list[dict]:
    """Merge this run's entries into a group: dedupe by ``url`` (preserving ``first_seen``), prune
    past the retention window, return newest-first. Pure function."""
    by_url = {e["url"]: dict(e) for e in existing if e.get("url")}
    for f in fresh:
        url = f.get("url")
        if not url:
            continue
        first_seen = by_url.get(url, {}).get("first_seen") or today.isoformat()
        by_url[url] = {**f, "first_seen": first_seen}
    cutoff = today - timedelta(days=retention_days)
    kept = [e for e in by_url.values() if (entry_date(e) is None or entry_date(e) >= cutoff)]
    kept.sort(key=lambda e: entry_date(e) or date.min, reverse=True)
    return kept


class RollingStore:
    """Per-group entry history persisted as JSON. ``path=None`` keeps it in-memory (tests)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None

    def load(self) -> dict[str, list[dict]]:
        if not self.path or not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8")).get("groups", {})
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, groups: dict[str, list[dict]]) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"groups": groups}, ensure_ascii=False, indent=2) + "\n", "utf-8"
        )

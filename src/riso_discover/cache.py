"""Tiny on-disk JSON cache for API responses.

Resolution lookups are the expensive part (Metron is rate-limited to 20 req/min). This cache lets
re-runs and retries avoid re-hitting the API. It is intentionally simple: one JSON file per call
signature under a gitignored .cache/ directory. Values must be JSON-serialisable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

from .config import REPO_ROOT

DEFAULT_CACHE_DIR = REPO_ROOT / ".cache"


class JsonCache:
    def __init__(self, namespace: str, cache_dir: Path = DEFAULT_CACHE_DIR, *, enabled: bool = True):
        self.enabled = enabled
        self.dir = cache_dir / namespace
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.dir / f"{digest}.json"

    def get(self, key: str) -> Optional[Any]:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))["value"]
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        path.write_text(json.dumps({"key": key, "value": value}, ensure_ascii=False), "utf-8")

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = compute()
        self.set(key, value)
        return value

"""Shared Metron access layer.

Metron is the resolution + cv_id bridge for *every* source (CLAUDE.md §7), so the mokkari client,
the on-disk cache, and the rate-limit retry policy live here once and are shared by every source
(MetronSource, WikidataSource, and later RSS/CBR). The gateway also satisfies the resolver's
``IssueSearcher`` and ``SeriesSearcher`` protocols, so resolver functions can take it directly.

mokkari enforces the rate limit locally and *raises* ``RateLimitError`` (with ``retry_after``) rather
than sleeping; ``_with_retry`` waits it out. Successful results are cached on disk, so retries and
re-runs never re-fetch.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Callable, Optional, TypeVar

from mokkari.exceptions import RateLimitError

from .cache import JsonCache
from .config import MetronCredentials

log = logging.getLogger(__name__)

T = TypeVar("T")

#: How many times to wait-and-retry when Metron's rate limit is hit. Generous because this is a
#: weekly batch job — correctness matters more than wall-clock.
MAX_RATE_RETRIES = 8


# --- mokkari object -> plain dict (for caching + the resolver) -------------------------------


def _issue_to_dict(issue: Any) -> dict:
    series = getattr(issue, "series", None)
    publisher = getattr(issue, "publisher", None)
    image = getattr(issue, "image", None)
    store_date = getattr(issue, "store_date", None)
    return {
        "id": getattr(issue, "id", None),
        "number": getattr(issue, "number", None),
        "store_date": store_date.isoformat() if isinstance(store_date, date) else store_date,
        "image": str(image) if image else None,
        "publisher": getattr(publisher, "name", None) if publisher else None,
        "cv_id": getattr(issue, "cv_id", None),
        "gcd_id": getattr(issue, "gcd_id", None),
        "isbn": getattr(issue, "isbn", None),
        "upc": getattr(issue, "upc", None),
        "series": {
            "id": getattr(series, "id", None),
            "name": getattr(series, "name", None),
            "year_began": getattr(series, "year_began", None),
        }
        if series
        else {},
    }


def _base_series_to_dict(series: Any) -> dict:
    # BaseSeries (list view) has display_name (not name) and lacks cv_id / publisher.
    return {
        "id": getattr(series, "id", None),
        "display_name": getattr(series, "display_name", None),
        "year_began": getattr(series, "year_began", None),
        "year_end": getattr(series, "year_end", None),
        "issue_count": getattr(series, "issue_count", None),
    }


def _series_to_dict(series: Any) -> dict:
    # Series (detail view) carries cv_id, publisher, series_type.
    publisher = getattr(series, "publisher", None)
    series_type = getattr(series, "series_type", None)
    return {
        "id": getattr(series, "id", None),
        "name": getattr(series, "name", None),
        "year_began": getattr(series, "year_began", None),
        "year_end": getattr(series, "year_end", None),
        "issue_count": getattr(series, "issue_count", None),
        "publisher": getattr(publisher, "name", None) if publisher else None,
        "series_type": getattr(series_type, "name", None) if series_type else None,
        "cv_id": getattr(series, "cv_id", None),
        "gcd_id": getattr(series, "gcd_id", None),
    }


class MetronGateway:
    """Cached, rate-limit-aware access to the slice of the Metron API the pipeline needs."""

    def __init__(
        self,
        credentials: MetronCredentials,
        *,
        cache: Optional[JsonCache] = None,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.credentials = credentials
        self.cache = cache or JsonCache("metron")
        self._client = client  # injectable for tests; lazily built otherwise
        self._sleep = sleep  # injectable so tests never actually sleep

    # -- mokkari client (lazy so tests can inject a fake and never import mokkari) ------------
    @property
    def client(self) -> Any:
        if self._client is None:
            import mokkari

            self._client = mokkari.api(self.credentials.username, self.credentials.password)
        return self._client

    def _with_retry(self, fn: Callable[[], T], *, label: str) -> T:
        for attempt in range(MAX_RATE_RETRIES + 1):
            try:
                return fn()
            except RateLimitError as exc:
                if attempt >= MAX_RATE_RETRIES:
                    raise
                wait = max(float(getattr(exc, "retry_after", 0) or 0), 1.0) + 1.0
                log.info(
                    "Rate limited on %s; waiting %.0fs (retry %d/%d)",
                    label,
                    wait,
                    attempt + 1,
                    MAX_RATE_RETRIES,
                )
                self._sleep(wait)
        raise RuntimeError("unreachable")  # pragma: no cover

    # -- issues -------------------------------------------------------------------------------
    def list_issue_ids(self, start: date, end: date) -> list[int]:
        key = f"issues_list:{start.isoformat()}:{end.isoformat()}"

        def compute() -> list[int]:
            results = self._with_retry(
                lambda: self.client.issues_list(
                    {
                        "store_date_range_after": start.isoformat(),
                        "store_date_range_before": end.isoformat(),
                    }
                ),
                label=f"issues_list {start}..{end}",
            )
            return [getattr(r, "id") for r in results]

        return self.cache.get_or_compute(key, compute)

    def issue_detail(self, issue_id: int) -> dict:
        return self.cache.get_or_compute(
            f"issue:{issue_id}",
            lambda: _issue_to_dict(
                self._with_retry(lambda: self.client.issue(issue_id), label=f"issue {issue_id}")
            ),
        )

    def series_cv_id(self, series_id: Optional[int]) -> Optional[int]:
        if series_id is None:
            return None
        return self.series_detail(series_id).get("cv_id")

    def search_issues(self, series_name: str, issue_number: Optional[str] = None) -> list[dict]:
        """Fuzzy issue search for the resolver's editorial path (IssueSearcher protocol)."""
        params: dict[str, Any] = {"series_name": series_name}
        if issue_number is not None:
            params["number"] = issue_number
        key = f"issues_search:{series_name}:{issue_number}"

        def compute() -> list[dict]:
            results = self._with_retry(
                lambda: self.client.issues_list(params), label=f"issues_search {series_name}"
            )
            # The search returns lightweight issues; fetch detail so cv_id/publisher are present.
            return [self.issue_detail(getattr(r, "id")) for r in results]

        return self.cache.get_or_compute(key, compute)

    # -- series -------------------------------------------------------------------------------
    def search_series(self, name: str) -> list[dict]:
        """Series name search (SeriesSearcher protocol). Returns lightweight BaseSeries dicts."""
        key = f"series_search:{name}"

        def compute() -> list[dict]:
            results = self._with_retry(
                lambda: self.client.series_list({"name": name}), label=f"series_search {name}"
            )
            return [_base_series_to_dict(r) for r in results]

        return self.cache.get_or_compute(key, compute)

    def series_detail(self, series_id: int) -> dict:
        return self.cache.get_or_compute(
            f"series:{series_id}",
            lambda: _series_to_dict(
                self._with_retry(lambda: self.client.series(series_id), label=f"series {series_id}")
            ),
        )

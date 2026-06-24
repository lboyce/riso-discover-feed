"""Metron source — the backbone. Powers New This Week and Upcoming Releases via store_date.

Flow per CLAUDE.md Section 6/7:
  1. issues_list(store_date_range_after/before) -> lightweight issues (no cv_id)
  2. issue(id) -> full record carrying cv_id, publisher, isbn/upc, series.id  (cached)
  3. series(series.id) -> series cv_id for the ComicVine volume id  (cached)
  4. resolve_metron_issue(...) -> Entity

All Metron access goes through mokkari (no hand-rolled HTTP). Calls are cached on disk; mokkari
self-throttles to the API rate limit. One failing issue is logged and skipped — never fatal.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Optional, TypeVar

from mokkari.exceptions import RateLimitError

from ..cache import JsonCache
from ..config import MetronCredentials
from ..models import Item, Reason, Section, entity_key
from ..resolver import resolve_metron_issue
from .base import BaseSource, SourceOutput

log = logging.getLogger(__name__)

T = TypeVar("T")

#: How many times to wait-and-retry when Metron's rate limit is hit. Generous because this is a
#: weekly batch job — correctness matters more than wall-clock.
MAX_RATE_RETRIES = 8


def week_window(today: date) -> tuple[date, date]:
    """Monday..Sunday of the week containing ``today`` — the New This Week window."""
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def upcoming_window(today: date, weeks: int = 4) -> tuple[date, date]:
    """The four weeks *after* the current week — the Upcoming Releases window."""
    _, this_week_end = week_window(today)
    start = this_week_end + timedelta(days=1)
    return start, start + timedelta(days=7 * weeks - 1)


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


@dataclass
class _Window:
    section_id: str
    section_type: str  # "new_releases" | "upcoming"
    title: str
    start: date
    end: date


class MetronSource(BaseSource):
    name = "metron"
    tier = "distribution"

    def __init__(
        self,
        credentials: MetronCredentials,
        *,
        today: date,
        cache: Optional[JsonCache] = None,
        client: Any = None,
        upcoming_weeks: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.credentials = credentials
        self.today = today
        self.cache = cache or JsonCache("metron")
        self._client = client  # injectable for tests; lazily built otherwise
        self.upcoming_weeks = upcoming_weeks
        self._sleep = sleep  # injectable so tests never actually sleep

    # -- mokkari client (lazy so tests can inject a fake and never import mokkari) ------------
    @property
    def client(self) -> Any:
        if self._client is None:
            import mokkari

            self._client = mokkari.api(self.credentials.username, self.credentials.password)
        return self._client

    def _with_retry(self, fn: Callable[[], T], *, label: str) -> T:
        """Run a Metron call, waiting out the rate limit instead of failing on it.

        mokkari enforces the limit locally and raises RateLimitError with a retry_after; we sleep
        that long (plus a small buffer) and retry. Successful results are cached by the caller, so
        progress is never lost across retries or re-runs."""
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

    # -- cached primitive calls ---------------------------------------------------------------
    def _list_issue_ids(self, start: date, end: date) -> list[int]:
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

    def _issue_detail(self, issue_id: int) -> dict:
        return self.cache.get_or_compute(
            f"issue:{issue_id}",
            lambda: _issue_to_dict(
                self._with_retry(lambda: self.client.issue(issue_id), label=f"issue {issue_id}")
            ),
        )

    def _series_cv_id(self, series_id: int) -> Optional[int]:
        if series_id is None:
            return None
        return self.cache.get_or_compute(
            f"series_cv_id:{series_id}",
            lambda: getattr(
                self._with_retry(
                    lambda: self.client.series(series_id), label=f"series {series_id}"
                ),
                "cv_id",
                None,
            ),
        )

    # -- main entry ---------------------------------------------------------------------------
    def run(self) -> SourceOutput:
        nt_start, nt_end = week_window(self.today)
        windows = [
            _Window("new-this-week", "new_releases", "New This Week", nt_start, nt_end),
        ]
        if self.upcoming_weeks > 0:
            up_start, up_end = upcoming_window(self.today, self.upcoming_weeks)
            windows.append(
                _Window("upcoming-releases", "upcoming", "Upcoming Releases", up_start, up_end)
            )

        out = SourceOutput()
        for win in windows:
            section = Section(
                id=win.section_id,
                type=win.section_type,  # type: ignore[arg-type]
                title=win.title,
                source_tier="distribution",
                source="Metron",
            )
            for entity, key in self._resolve_window(win.start, win.end):
                out.entities.setdefault(key, entity)
                section.items.append(
                    Item(
                        entity=key,
                        reason=Reason(
                            type="new_release",
                            source="Metron",
                        ),
                    )
                )
            out.sections.append(section)
            log.info("Metron %s: %d issues", win.section_id, len(section.items))
        return out

    def _resolve_window(self, start: date, end: date):
        try:
            issue_ids = self._list_issue_ids(start, end)
        except Exception as exc:  # never let a list failure crash the run
            log.warning("Metron issues_list %s..%s failed: %s", start, end, exc)
            return

        for issue_id in issue_ids:
            try:
                detail = self._issue_detail(issue_id)
                series_id = (detail.get("series") or {}).get("id")
                series_cv_id = self._series_cv_id(series_id) if series_id else None
                entity = resolve_metron_issue(detail, series_cv_id=series_cv_id)
                yield entity, entity_key(entity.ids)
            except Exception as exc:  # one bad book must not abort the rest
                log.warning("Metron issue %s failed to resolve: %s", issue_id, exc)
                continue

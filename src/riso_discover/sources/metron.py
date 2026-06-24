"""Metron source — the backbone. Powers New This Week and Upcoming Releases via store_date.

Flow per CLAUDE.md Section 6/7:
  1. issues_list(store_date_range_after/before) -> lightweight issues (no cv_id)
  2. issue(id) -> full record carrying cv_id, publisher, isbn/upc, series.id  (cached)
  3. series(series.id) -> series cv_id for the ComicVine volume id  (cached)
  4. resolve_metron_issue(...) -> Entity

All Metron access goes through the shared MetronGateway (cache + rate-limit retry). One failing
issue is logged and skipped — never fatal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from ..metron_gateway import MetronGateway
from ..models import Item, Reason, Section, entity_key
from ..resolver import resolve_metron_issue
from .base import BaseSource, SourceOutput

log = logging.getLogger(__name__)


def week_window(today: date) -> tuple[date, date]:
    """Monday..Sunday of the week containing ``today`` — the New This Week window."""
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def upcoming_window(today: date, weeks: int = 4) -> tuple[date, date]:
    """The weeks *after* the current week — the Upcoming Releases window."""
    _, this_week_end = week_window(today)
    start = this_week_end + timedelta(days=1)
    return start, start + timedelta(days=7 * weeks - 1)


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

    def __init__(self, gateway: MetronGateway, *, today: date, upcoming_weeks: int = 4):
        self.gateway = gateway
        self.today = today
        self.upcoming_weeks = upcoming_weeks

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
                    Item(entity=key, reason=Reason(type="new_release", source="Metron"))
                )
            out.sections.append(section)
            log.info("Metron %s: %d issues", win.section_id, len(section.items))
        return out

    def _resolve_window(self, start: date, end: date):
        try:
            issue_ids = self.gateway.list_issue_ids(start, end)
        except Exception as exc:  # never let a list failure crash the run
            log.warning("Metron issues_list %s..%s failed: %s", start, end, exc)
            return

        for issue_id in issue_ids:
            try:
                detail = self.gateway.issue_detail(issue_id)
                series_id = (detail.get("series") or {}).get("id")
                series_cv_id = self.gateway.series_cv_id(series_id) if series_id else None
                entity = resolve_metron_issue(detail, series_cv_id=series_cv_id)
                yield entity, entity_key(entity.ids)
            except Exception as exc:  # one bad book must not abort the rest
                log.warning("Metron issue %s failed to resolve: %s", issue_id, exc)
                continue

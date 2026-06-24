"""Comic Book Roundup — the automated curator (personal-tier).

CBR is read as a SIGNAL only (CLAUDE.md §8): we take *which* issues are highly rated / most pulled
this week, then make RISO's OWN independent selection and present it as RISO's picks. We never
reproduce CBR's ranked list, never keep their order, and never parse or store their scores. No CBR
reference appears in the output (owner's conservative posture). Optional attribution is intentionally
omitted.

This is a personal-tier source: it runs only when build_tier="personal" (config.active_sources drops
it from a distributed build), pending permission from CBR.

Entry links encode everything we need:
  /comic-books/reviews/<publisher>/<series-slug>-(YYYY)/<issue>
so we get publisher, series, volume year, and issue without touching the score.
"""

from __future__ import annotations

import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Callable, Optional

from ..metron_gateway import MetronGateway
from ..models import Item, Reason, Section, entity_key
from ..resolver import resolve_query
from .base import BaseSource, SourceOutput

log = logging.getLogger(__name__)

CBR_BASE = "https://comicbookroundup.com"
CBR_USER_AGENT = (
    "riso-discover-feed/0.1 (https://github.com/lboyce/riso-discover-feed; lukeslens@gmail.com)"
)

# href pattern for a CBR issue entry: /comic-books/reviews/<publisher>/<series-slug>-(YYYY)/<issue>
_ENTRY_HREF_RE = re.compile(
    r"^/comic-books/reviews/(?P<publisher>[^/]+)/(?P<slug>.+?)(?:-\((?P<year>\d{4})\))?/(?P<issue>\d+)/?$"
)
_TRAILING_ISSUE_RE = re.compile(r"\s*#\s*\d+\s*$")  # strip "#66" from anchor text

# Slug -> display publisher. Fallback title-cases the slug.
_PUBLISHER_NAMES = {
    "dc-comics": "DC Comics",
    "marvel-comics": "Marvel",
    "image-comics": "Image Comics",
    "idw-publishing": "IDW Publishing",
    "dark-horse-comics": "Dark Horse Comics",
    "boom-studios": "BOOM! Studios",
    "dynamite-entertainment": "Dynamite Entertainment",
    "oni-press": "Oni Press",
    "valiant-comics": "Valiant",
    "vault-comics": "Vault Comics",
    "mad-cave-studios": "Mad Cave Studios",
    "titan-comics": "Titan Comics",
    "skybound": "Skybound",
}


def _publisher_name(slug: str) -> str:
    return _PUBLISHER_NAMES.get(slug, slug.replace("-", " ").title())


@dataclass(frozen=True)
class Candidate:
    series: str
    issue: str
    publisher: str
    year: Optional[int]


@dataclass(frozen=True)
class _List:
    path: str
    section_id: str
    section_title: str
    section_type: str  # "featured_picks" | "trending"
    reason_type: str  # "featured_pick" | "trending"
    reason_label: str


LISTS = [
    _List(
        "/comic-books/recent-highest-rated-list",
        "featured-books",
        "Featured Books of the Week",
        "featured_picks",
        "featured_pick",
        "Featured Book of the Week",
    ),
    _List(
        "/comic-books/recent-most-pulled-list",
        "trending",
        "Trending This Week",
        "trending",
        "trending",
        "Trending This Week",
    ),
]


class _EntryLinkParser(HTMLParser):
    """Collects (href, text) for anchors whose href matches the CBR issue pattern."""

    def __init__(self):
        super().__init__()
        self._href: Optional[str] = None
        self._text: list[str] = []
        self.entries: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if _ENTRY_HREF_RE.match(href):
                self._href = href
                self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.entries.append((self._href, "".join(self._text).strip()))
            self._href = None
            self._text = []


def parse_list(html: str) -> list[Candidate]:
    """Extract (series, issue, publisher, year) candidates from a CBR list page. No scores."""
    parser = _EntryLinkParser()
    parser.feed(html)
    out: list[Candidate] = []
    seen = set()
    for href, text in parser.entries:
        m = _ENTRY_HREF_RE.match(href)
        if not m:
            continue
        issue = m.group("issue")
        publisher = _publisher_name(m.group("publisher"))
        year = int(m.group("year")) if m.group("year") else None
        # Prefer the human anchor text for the series name; fall back to de-slugged.
        series = _TRAILING_ISSUE_RE.sub("", text).strip() if text else None
        if not series:
            series = m.group("slug").replace("-", " ").title()
        dedupe_key = (series.lower(), issue, publisher)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append(Candidate(series=series, issue=issue, publisher=publisher, year=year))
    return out


def select_picks(candidates: list[Candidate], week: int) -> list[Candidate]:
    """RISO's own selection: round-robin by publisher (one-per-publisher spacing), week-rotated start.

    The output order deliberately differs from CBR's input/ranked order. Pure function."""
    groups: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for c in candidates:
        if c.publisher not in groups:
            groups[c.publisher] = []
            order.append(c.publisher)
        groups[c.publisher].append(c)
    if not order:
        return []
    # Rotate which publisher leads, by week, for variation without persisted state.
    start = week % len(order)
    rotated = order[start:] + order[:start]
    picks: list[Candidate] = []
    i = 0
    while any(groups[p] for p in rotated):
        p = rotated[i % len(rotated)]
        if groups[p]:
            picks.append(groups[p].pop(0))
        i += 1
    return picks


class CBRSource(BaseSource):
    name = "cbr"
    tier = "personal"

    def __init__(
        self,
        gateway: MetronGateway,
        *,
        today: date,
        max_picks: int = 8,
        fetch: Optional[Callable[[str], str]] = None,
    ):
        self.gateway = gateway
        self.today = today
        self.max_picks = max_picks
        self._fetch = fetch or self._default_fetch

    def _default_fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": CBR_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def run(self) -> SourceOutput:
        out = SourceOutput()
        week = self.today.isocalendar().week
        for spec in LISTS:
            section = Section(
                id=spec.section_id,
                type=spec.section_type,  # type: ignore[arg-type]
                title=spec.section_title,
                source_tier="personal",
                source="RISO",
            )
            try:
                picks = self._resolved_picks(spec, week)
            except Exception as exc:  # a failing list must not crash the run
                log.warning("CBR list %s failed: %s", spec.section_id, exc)
                out.sections.append(section)
                continue

            for key, entity, reason in picks:
                out.entities.setdefault(key, entity)
                section.items.append(Item(entity=key, reason=reason))
            out.sections.append(section)
            log.info("CBR %s: %d picks", spec.section_id, len(section.items))
        return out

    def _resolved_picks(self, spec: _List, week: int):
        html = self._fetch(CBR_BASE + spec.path)
        ordered = select_picks(parse_list(html), week)
        results = []
        seen: set[str] = set()
        for cand in ordered:
            if len(results) >= self.max_picks:
                break
            try:
                entity, _conf = resolve_query(
                    self.gateway, cand.series, cand.issue,
                    publisher_hint=cand.publisher, date_hint=self.today,
                )
            except Exception as exc:
                log.warning("CBR pick %s #%s failed: %s", cand.series, cand.issue, exc)
                continue
            if entity is None:
                continue  # picks must be pullable; skip and let the next candidate fill the slot
            key = entity_key(entity.ids)
            if key in seen:
                continue  # two CBR entries resolved to the same book; feature it once
            seen.add(key)
            reason = Reason(type=spec.reason_type, source="RISO", label=spec.reason_label)
            results.append((key, entity, reason))
        return results

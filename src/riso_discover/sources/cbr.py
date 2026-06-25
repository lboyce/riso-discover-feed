"""Comic Book Roundup — curation shelves (personal-tier).

Three shelves, all resolved to pullable Metron entities:
  * Critically Acclaimed — CBR's highest-rated list, in order, shown WITH the aggregate critic score.
  * Popular This Week    — CBR's most-pulled list.
  * RISO Recommends      — a balanced, week-varied pick drawn from both lists (publisher-diverse,
                           week-seeded shuffle): "quality without full human editorial".

Owner decision (testing phase): the CBR rating IS shown (score /10 + review count), with CBR credited
as the source on the two direct lists. This is gated behind ``show_rating`` so a clean ship (scores
off, no CBR reference) is one flag away — and CBR remains personal-tier, so a distribution build
drops it entirely. Written permission from CBR is being sought before any public ship.

Each entry link encodes /comic-books/reviews/<publisher>/<series-slug>-(YYYY)/<issue>; the card also
carries the aggregate score and review count, which we now parse.
"""

from __future__ import annotations

import logging
import random
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

HIGHEST_RATED_PATH = "/comic-books/recent-highest-rated-list"
MOST_PULLED_PATH = "/comic-books/recent-most-pulled-list"

_ENTRY_HREF_RE = re.compile(
    r"^/comic-books/reviews/(?P<publisher>[^/]+)/(?P<slug>.+?)(?:-\((?P<year>\d{4})\))?/(?P<issue>\d+)/?$"
)
_TRAILING_ISSUE_RE = re.compile(r"\s*#\s*\d+\s*$")  # strip "#66" from anchor text

_PUBLISHER_NAMES = {
    "dc-comics": "DC Comics",
    "marvel-comics": "Marvel",
    "image-comics": "Image Comics",
    "idw-publishing": "IDW Publishing",
    "dark-horse-comics": "Dark Horse Comics",
    "boom-studios": "BOOM! Studios",
    "dynamite-entertainment": "Dynamite Entertainment",
    "dynamite": "Dynamite Entertainment",
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
    href: str  # CBR path, for attribution link-back
    score: Optional[float] = None  # aggregate critic score (out of 10)
    review_count: Optional[int] = None


class _CardParser(HTMLParser):
    """Builds one card per CBR list entry: title (link) + aggregate score + review count.

    Card markup (verified live):
      <h3><a href=ENTRY>Title #N</a></h3>
      <a href=ENTRY><img .../></a>
      <div class="review COLOR"><span>9.7</span></div>
      <div class="review-count">5 reviews</div>
    """

    def __init__(self):
        super().__init__()
        self.cards: list[dict] = []
        self._cur: Optional[dict] = None
        self._capture: Optional[str] = None  # "title" | "score" | "count"
        self._buf: list[str] = []
        self._pending_href: Optional[str] = None
        self._expect_score = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and _ENTRY_HREF_RE.match(a.get("href", "")):
            self._pending_href = a["href"]
            self._capture, self._buf = "title", []
        elif tag == "div":
            cls = (a.get("class") or "").split()
            if "review-count" in cls:
                self._capture, self._buf = "count", []
            elif "review" in cls:
                self._expect_score = True
        elif tag == "span" and self._expect_score:
            self._capture, self._buf, self._expect_score = "score", [], False

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag):
        text = "".join(self._buf).strip()
        if tag == "a" and self._capture == "title":
            self._capture = None
            if text:  # title anchor has text; the cover anchor (just an <img>) does not
                self._cur = {"href": self._pending_href, "title": text, "score": None, "count": None}
                self.cards.append(self._cur)
        elif tag == "span" and self._capture == "score":
            self._capture = None
            if self._cur is not None:
                m = re.match(r"\d+(?:\.\d+)?", text)
                if m:
                    self._cur["score"] = float(m.group(0))
        elif tag == "div" and self._capture == "count":
            self._capture = None
            if self._cur is not None:
                m = re.search(r"\d+", text)
                if m:
                    self._cur["count"] = int(m.group(0))


def parse_list(html: str) -> list[Candidate]:
    """Extract candidates (series, issue, publisher, year, href, score, review_count) from a list."""
    parser = _CardParser()
    parser.feed(html)
    out: list[Candidate] = []
    seen = set()
    for card in parser.cards:
        m = _ENTRY_HREF_RE.match(card["href"])
        if not m:
            continue
        issue = m.group("issue")
        publisher = _publisher_name(m.group("publisher"))
        series = _TRAILING_ISSUE_RE.sub("", card["title"]).strip() or m.group("slug").replace("-", " ").title()
        key = (series.lower(), issue, publisher)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Candidate(
                series=series,
                issue=issue,
                publisher=publisher,
                year=int(m.group("year")) if m.group("year") else None,
                href=card["href"],
                score=card["score"],
                review_count=card["count"],
            )
        )
    return out


def select_picks(candidates: list[Candidate], week: int) -> list[Candidate]:
    """Balanced, week-varied selection: publisher round-robin with a week-seeded shuffle of both the
    publisher order and the within-publisher order. Deterministic per week, varies week to week, and
    never leads with the same publisher run. Pure function."""
    groups: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for c in candidates:
        if c.publisher not in groups:
            groups[c.publisher] = []
            order.append(c.publisher)
        groups[c.publisher].append(c)
    if not order:
        return []
    rng = random.Random(week)
    for p in order:
        rng.shuffle(groups[p])
    rng.shuffle(order)
    picks: list[Candidate] = []
    i = 0
    while any(groups[p] for p in order):
        p = order[i % len(order)]
        if groups[p]:
            picks.append(groups[p].pop(0))
        i += 1
    return picks


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    out, seen = [], set()
    for c in candidates:
        key = (c.series.lower(), c.issue, c.publisher)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


class CBRSource(BaseSource):
    name = "cbr"
    tier = "personal"

    def __init__(
        self,
        gateway: MetronGateway,
        *,
        today: date,
        max_per_shelf: int = 12,
        recommends_count: int = 8,
        show_rating: bool = True,
        fetch: Optional[Callable[[str], str]] = None,
    ):
        self.gateway = gateway
        self.today = today
        self.max_per_shelf = max_per_shelf
        self.recommends_count = recommends_count
        self.show_rating = show_rating
        self._fetch = fetch or self._default_fetch

    def _default_fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": CBR_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _candidates(self, path: str) -> list[Candidate]:
        try:
            return parse_list(self._fetch(CBR_BASE + path))
        except Exception as exc:  # a failing list must not crash the run
            log.warning("CBR fetch/parse %s failed: %s", path, exc)
            return []

    def _reason(self, cand: Candidate, *, reason_type: str, source: str, label: str, cbr_link: bool) -> Reason:
        kw = {"type": reason_type, "source": source, "label": label}
        if self.show_rating:
            if cand.score is not None:
                kw["score"] = cand.score
                kw["score_max"] = 10.0
            if cand.review_count is not None:
                kw["review_count"] = cand.review_count
            if cbr_link:
                kw["url"] = CBR_BASE + cand.href
        return Reason(**kw)  # type: ignore[arg-type]

    def _shelf(self, cands, *, cap, reason_type, source, label, cbr_link):
        results, seen = [], set()
        for cand in cands:
            if len(results) >= cap:
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
                continue
            seen.add(key)
            results.append(
                (key, entity, self._reason(cand, reason_type=reason_type, source=source,
                                           label=label, cbr_link=cbr_link))
            )
        return results

    def run(self) -> SourceOutput:
        out = SourceOutput()
        week = self.today.isocalendar().week
        acclaimed = self._candidates(HIGHEST_RATED_PATH)
        popular = self._candidates(MOST_PULLED_PATH)
        cbr_source = "Comic Book Roundup" if self.show_rating else "RISO"

        shelves = [
            ("riso-recommends", "RISO Recommends", "featured_picks", "featured_pick", "RISO",
             select_picks(_dedupe(acclaimed + popular), week), self.recommends_count, False),
            ("critically-acclaimed", "Critically Acclaimed", "featured_picks", "featured_pick",
             cbr_source, acclaimed, self.max_per_shelf, True),
            ("popular", "Popular This Week", "trending", "trending",
             cbr_source, popular, self.max_per_shelf, True),
        ]

        for sid, title, sec_type, reason_type, source, cands, cap, cbr_link in shelves:
            section = Section(
                id=sid, type=sec_type, title=title, source_tier="personal", source=source,
            )
            for key, entity, reason in self._shelf(
                cands, cap=cap, reason_type=reason_type, source=source, label=title, cbr_link=cbr_link
            ):
                out.entities.setdefault(key, entity)
                section.items.append(Item(entity=key, reason=reason))
            out.sections.append(section)
            log.info("CBR %s: %d picks", sid, len(section.items))
        return out

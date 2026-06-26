"""RSS source — per-outlet review departments.

Reads each outlet's feed, keeps the single-book *reviews* (reviews-biased, per the owner's decision
and CLAUDE.md §6's reliability ladder), and resolves the reviewed issue to a pullable entity via
Metron's editorial path (resolve_query). Reviews that don't resolve degrade to an editorial-only
entry (headline + excerpt + image + link), keyed by the article URL.

Copyright (§8): we emit only the headline, the feed-provided excerpt (HTML-stripped and truncated),
the feed image, and a link back — never the full article body (entry.content).

Feeds are fetched with a descriptive User-Agent (some outlet CDNs block default/bot agents). A
failing feed or entry is logged and skipped — never fatal.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import date
from html import unescape
from typing import Callable, Optional

import feedparser

from ..cache import JsonCache
from ..config import REPO_ROOT
from ..metron_gateway import MetronGateway
from ..models import Entity, Ids, Item, Reason, Resolution, Section, entity_key
from ..resolver import resolve_query
from ..store import RollingStore, merge_entries
from .base import BaseSource, SourceOutput

log = logging.getLogger(__name__)

RSS_USER_AGENT = (
    "riso-discover-feed/0.1 (https://github.com/lboyce/riso-discover-feed; lukeslens@gmail.com)"
)

#: Persisted, committed store so the AIPT shelf accumulates reviews across weekly runs (a shallow
#: feed otherwise only sees this week). Older reviews surface once their issues are ComicVine-indexed.
DEFAULT_REVIEW_STORE = REPO_ROOT / "state" / "aipt_reviews.json"
DEFAULT_RETENTION_DAYS = 90


@dataclass(frozen=True)
class Feed:
    outlet: str
    url: str
    section_id: str
    section_title: str
    #: True if the URL is already a reviews-only feed (then every "Series #N" entry counts as a
    #: review). False for general feeds, where we additionally require a review signal.
    reviews_feed: bool = False


# Active review departments. Reliability-first (§6 ladder): we ship the feeds that actually deliver
# clean single-book reviews. AIPT's reviews-category feed is exactly that — titles like
# "'In Your Skin' #3 blurs the line..." tagged "Reviews" — and parses + resolves cleanly.
FEEDS = [
    Feed(
        "AIPT",
        "https://aiptcomics.com/category/comic-books/comic-book-reviews/feed/",
        "aipt-reviews",
        "AIPT Reviews",
        reviews_feed=True,
    ),
]

# Candidates confirmed distribution-clean (§7) but NOT yet shipped as single-book review departments,
# verified live 2026-06: each needs work before it yields clean, resolvable single-issue reviews.
#   - The Comics Beat (comicsbeat.com/category/comics/reviews/feed/): "Rundown"/"Round-Up" columns
#     cover several books at once — these belong to the deferred Editorial Best-Of (structured
#     columns), not a single-book review department.
#   - The Comics Journal (tcj.com/feed/): long-form graphic-novel/essay reviews, no "#N" issue
#     pattern — better suited to a future trades/collections editorial path.
#   - Multiversity (multiversitycomics.com): the documented /feed URL 404s / times out; needs a
#     working feed URL confirmed before enabling.

_ISSUE_RE = re.compile(r"#\s*(\d+)")
_LEADING_REVIEW_RE = re.compile(r"^\s*review\s*[:\-–—]\s*", re.I)
_REVIEW_WORD_RE = re.compile(r"\breviews?\b", re.I)  # review/reviews, but not preview(s)
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def parse_review_title(title: Optional[str]) -> Optional[tuple[str, str]]:
    """Extract (series, issue) from a review-style headline. Pure, tested.

    Handles "Saga #66 review", "Review: Saga #66", quoted variants. Returns None if there's no
    "#N" reference (i.e. not a single-issue headline)."""
    if not title:
        return None
    m = _ISSUE_RE.search(title)
    if not m:
        return None
    issue = m.group(1)
    series = title[: m.start()]
    series = _LEADING_REVIEW_RE.sub("", series)
    series = series.strip().strip("\"'‘’“”").strip()
    series = " ".join(series.split())
    return (series, issue) if series else None


def _is_review(entry: dict, ref: Optional[tuple[str, str]], reviews_feed: bool) -> bool:
    """Reviews-bias filter: a single-book review names one issue and carries a review signal."""
    if ref is None:
        return False
    if reviews_feed:
        return True
    if _REVIEW_WORD_RE.search(entry.get("title", "") or ""):
        return True
    for tag in entry.get("tags", []) or []:
        # Word-boundary match so a "Previews" category doesn't substring-match "review".
        if _REVIEW_WORD_RE.search(tag.get("term") or ""):
            return True
    return False


def _clean_snippet(html: Optional[str], limit: int = 280) -> Optional[str]:
    """Strip HTML, unescape entities, collapse whitespace, truncate. Never the full body."""
    if not html:
        return None
    text = unescape(_TAG_RE.sub("", html))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text or None


def _entry_image(entry: dict) -> Optional[str]:
    for key in ("media_thumbnail", "media_content"):
        items = entry.get(key)
        if items and items[0].get("url"):
            return items[0]["url"]
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("image") and enc.get("href"):
            return enc["href"]
    m = _IMG_SRC_RE.search(entry.get("summary", "") or "")
    return m.group(1) if m else None


def _published_date(entry: dict) -> Optional[date]:
    pp = entry.get("published_parsed") or entry.get("updated_parsed")
    if isinstance(pp, time.struct_time):
        return date(pp.tm_year, pp.tm_mon, pp.tm_mday)
    return None


# --- AIPT article enrichment (Lets-Review WordPress block; verified live) --------------------
# Reproduces only the score + verdict + likes/dislikes (a teaser), never the full body, and the
# article link is kept prominent for a "Read more at AIPT" button. Owner's §8 call; toggle-gated.

_LR_VERDICT_RE = re.compile(r'lets-review-block__conclusion"[^>]*>(.*?)</div>', re.DOTALL)
_LR_PRO_RE = re.compile(r'lets-review-block__pro">(.*?)</div>', re.DOTALL)
_LR_CON_RE = re.compile(r'lets-review-block__con">(.*?)</div>', re.DOTALL)
_LR_SCORE_RE = re.compile(r'score-level-\d+">.*?class="score">\s*([\d.]+)', re.DOTALL)


def parse_review_article(html: str) -> dict:
    """Extract {score, verdict, pros, cons} from an AIPT review's Lets-Review block. Pure, tested."""
    verdict_m = _LR_VERDICT_RE.search(html)
    score_m = _LR_SCORE_RE.search(html)
    return {
        "score": float(score_m.group(1)) if score_m else None,
        "verdict": _clean_snippet(verdict_m.group(1), limit=400) if verdict_m else None,
        "pros": [_clean_snippet(x, limit=160) for x in _LR_PRO_RE.findall(html)][:6],
        "cons": [_clean_snippet(x, limit=160) for x in _LR_CON_RE.findall(html)][:6],
    }


class RSSSource(BaseSource):
    name = "rss"
    tier = "distribution"

    def __init__(
        self,
        gateway: MetronGateway,
        *,
        today: date,
        max_items: int = 12,
        feeds: Optional[list[Feed]] = None,
        include_verdict: bool = False,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        review_store: Optional[RollingStore] = None,
        fetch: Optional[Callable[[str], bytes]] = None,
        article_fetch: Optional[Callable[[str], str]] = None,
        articles_cache: Optional[JsonCache] = None,
    ):
        self.gateway = gateway
        self.today = today
        self.max_items = max_items
        self.feeds = feeds if feeds is not None else FEEDS
        self.include_verdict = include_verdict
        self.retention_days = retention_days
        self._store = review_store if review_store is not None else RollingStore(DEFAULT_REVIEW_STORE)
        self._fetch = fetch or self._default_fetch
        self._article_fetch = article_fetch or self._default_article_fetch
        self._articles = articles_cache or JsonCache("rss_articles")  # parsed review extracts by URL

    def _default_fetch(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": RSS_USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def _default_article_fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": RSS_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _review_extract(self, url: str) -> dict:
        """Fetch + parse an AIPT review article (cached by URL). {} on failure (graceful)."""
        def compute() -> dict:
            try:
                return parse_review_article(self._article_fetch(url))
            except Exception as exc:
                log.warning("AIPT article fetch failed %s: %s", url, exc)
                return {}
        return self._articles.get_or_compute(f"article:{url}", compute)

    def run(self) -> SourceOutput:
        out = SourceOutput()
        feeds_store = self._store.load()
        for feed in self.feeds:
            section = Section(
                id=feed.section_id,
                type="rss_department",
                title=feed.section_title,
                source_tier="distribution",
                source=feed.outlet,
            )
            try:
                fresh = self._parse_feed_reviews(feed)
            except Exception as exc:  # a blocked/malformed feed must not crash the run
                log.warning("RSS feed %s failed: %s", feed.outlet, exc)
                fresh = []

            # Merge this run's reviews into the rolling store, then build the shelf from the store
            # (newest-first) so it spans recent weeks, not just this week.
            entries = merge_entries(
                feeds_store.get(feed.section_id, []), fresh, self.today, self.retention_days
            )
            feeds_store[feed.section_id] = entries

            seen: set[str] = set()
            for entry in entries:
                if len(section.items) >= self.max_items:
                    break
                try:
                    built = self._build_item(feed, entry)
                except Exception as exc:  # one bad entry must not abort the rest
                    log.warning("RSS entry %r failed: %s", entry.get("url"), exc)
                    continue
                if built is None:
                    continue
                key, entity, reason = built
                if key in seen:
                    continue
                seen.add(key)
                out.entities.setdefault(key, entity)
                section.items.append(Item(entity=key, reason=reason))
            out.sections.append(section)
            log.info(
                "RSS %s: %d reviews (store=%d)", feed.section_id, len(section.items), len(entries)
            )
        self._store.save(feeds_store)
        return out

    def _parse_feed_reviews(self, feed: Feed) -> list[dict]:
        """Normalize this run's review entries from a feed (no resolution yet)."""
        parsed = feedparser.parse(self._fetch(feed.url))
        out = []
        for entry in parsed.entries:
            ref = parse_review_title(entry.get("title", ""))
            if not _is_review(entry, ref, feed.reviews_feed):
                continue
            link = entry.get("link")
            if not link:
                continue
            series, issue = ref
            pd = _published_date(entry)
            out.append({
                "url": link,
                "series": series,
                "issue": issue,
                "published": pd.isoformat() if pd else None,
                "image": _entry_image(entry),
                "summary": _clean_snippet(entry.get("summary")),
            })
        return out

    def _build_item(self, feed: Feed, entry: dict):
        """Resolve a stored review entry. AIPT is intentionally NOT issue-gated (owner: keep reviews
        fresh) — a freshness-lagged review still resolves to a volume (series-matchable, with a cover);
        a wholly-unresolved one degrades to an editorial link. Older reviews fully resolve over time as
        the store ages, so the shelf is never empty and improves with age."""
        series, issue, link = entry["series"], entry["issue"], entry["url"]
        pd = date.fromisoformat(entry["published"]) if entry.get("published") else None
        resolved, _confidence = resolve_query(self.gateway, series, issue, date_hint=pd)
        if resolved is not None:
            entity = resolved
            key = entity_key(entity.ids)
        else:  # graceful degradation: editorial-only entry keyed by the article URL
            ids = Ids(source_url=link, series_name=series)
            entity = Entity(
                kind="issue", title=f"{series} #{issue}", series_name=series, issue_number=issue,
                format="single_issue", ids=ids,
                resolution=Resolution(confidence="unresolved", issue_pending=False),
            )
            key = entity_key(ids)

        reason = Reason(
            type="review_signal",
            source=feed.outlet,
            label=f"{feed.outlet} review",
            url=link,  # RISO's "Read more at <outlet>" link
            snippet=entry.get("summary"),
            image=entry.get("image"),
        )
        # Enrich from the article: score + verdict teaser + likes/dislikes (never the full body).
        if self.include_verdict:
            extract = self._review_extract(link)
            if extract.get("score") is not None:
                reason.score = extract["score"]
                reason.score_max = 10.0
            if extract.get("verdict"):
                reason.snippet = extract["verdict"]  # the verdict reads better than the feed excerpt
            if extract.get("pros"):
                reason.pros = extract["pros"]
            if extract.get("cons"):
                reason.cons = extract["cons"]

        return key, entity, reason

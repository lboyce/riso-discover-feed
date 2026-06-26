"""RSS review department source — title parsing, reviews-bias filter, assembly. Fully offline.

A fixture feed is parsed by real feedparser (via an injected fetch); a fake gateway resolves the
Saga review and fails the obscure indie one, exercising both the pullable and editorial-fallback
paths. The preview and news items must be filtered out (reviews-bias).
"""

from datetime import date
from pathlib import Path

import pytest

from riso_discover.cache import JsonCache
from riso_discover.sources.rss import (
    Feed,
    RSSSource,
    _clean_snippet,
    parse_review_article,
    parse_review_title,
)
from riso_discover.store import RollingStore, merge_entries

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FEED_XML = (FIXTURES / "rss_reviews.xml").read_bytes()
AIPT_ARTICLE = (FIXTURES / "aipt_review.html").read_text("utf-8")
_NO_CACHE = JsonCache("test", enabled=False)

TEST_FEED = Feed("AIPT", "https://example/feed", "aipt-reviews", "AIPT Reviews")


# --- title parsing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Saga #66 review: A stunning return", ("Saga", "66")),
        ("Review: Saga #66", ("Saga", "66")),
        ("‘Saga’ #66 review", ("Saga", "66")),
        ("Saga #66", ("Saga", "66")),
        ("Marvel announces new X-Men ongoing", None),
        ("", None),
    ],
)
def test_parse_review_title(title, expected):
    assert parse_review_title(title) == expected


def test_clean_snippet_strips_html_and_truncates():
    out = _clean_snippet("<p>Hello <b>world</b> &amp; friends</p>")
    assert out == "Hello world & friends"
    long = _clean_snippet("<p>" + "x " * 400 + "</p>", limit=50)
    assert len(long) <= 51 and long.endswith("…")


# --- assembly -------------------------------------------------------------------------------


class FakeGateway:
    """SeriesSearcher/IssueSearcher fake; resolves only Saga #66."""

    def search_issues(self, series_name, issue_number=None):
        if "Saga" in series_name and issue_number == "66":
            return [{
                "id": 700, "number": "66", "store_date": "2024-05-01",
                "publisher": "Image Comics", "cv_id": 880000, "isbn": None, "upc": None,
                "gcd_id": None, "image": None,
                "series": {"id": 7777, "name": "Saga", "year_began": 2012, "cv_id": 18029},
            }]
        return []  # Obscure Indie Thing -> unresolved


def _run():
    source = RSSSource(
        FakeGateway(),
        today=date(2024, 5, 10),
        feeds=[TEST_FEED],
        fetch=lambda url: FEED_XML,
        review_store=RollingStore(None),  # in-memory; no disk
    )
    return source.run()


def test_aipt_keeps_reviews_fresh_with_editorial_fallback():
    out = _run()
    section = next(s for s in out.sections if s.id == "aipt-reviews")
    assert section.type == "rss_department"
    # AIPT is not issue-gated (owner: keep fresh): Saga #66 resolves; Obscure Indie Thing degrades to
    # an editorial link. Preview & news were filtered out. So two review items.
    assert len(section.items) == 2
    assert "cv-issue-4000-880000" in out.entities  # resolved review
    assert any(k.startswith("rss-") for k in out.entities)  # editorial fallback for the unresolved one


def test_resolved_review_is_pullable_with_review_signal():
    out = _run()
    e = out.entities["cv-issue-4000-880000"]
    assert e.ids.comicvine_issue == "4000-880000"
    assert e.ids.comicvine_volume == "4050-18029"

    item = next(
        it for s in out.sections for it in s.items if it.entity == "cv-issue-4000-880000"
    )
    assert item.reason.type == "review_signal"
    assert item.reason.url == "https://aiptcomics.com/2024/05/01/saga-66-review/"
    assert item.reason.image == "https://img.example/saga66-thumb.jpg"
    assert "Brian K. Vaughan" in item.reason.snippet and "<" not in item.reason.snippet


# --- AIPT article enrichment (verdict + score + likes/dislikes) -----------------------------


def test_parse_review_article_extracts_score_verdict_proscons():
    x = parse_review_article(AIPT_ARTICLE)
    assert x["score"] == 9.0
    assert x["verdict"].startswith("A propulsive, beautifully drawn return")
    assert x["pros"] == ["Gorgeous Fiona Staples art", "A gut-punch of an emotional return"]
    assert x["cons"] == ["The wait between issues remains brutal"]
    # The article body paragraphs are never captured.
    assert "Body text" not in (x["verdict"] or "")


def test_include_verdict_enriches_the_reason():
    src = RSSSource(
        FakeGateway(),
        today=date(2024, 5, 10),
        feeds=[TEST_FEED],
        fetch=lambda url: FEED_XML,
        include_verdict=True,
        article_fetch=lambda url: AIPT_ARTICLE,
        articles_cache=_NO_CACHE,
        review_store=RollingStore(None),
    )
    out = src.run()
    item = next(
        it for s in out.sections for it in s.items if it.entity == "cv-issue-4000-880000"
    )
    r = item.reason
    assert r.score == 9.0 and r.score_max == 10.0
    assert r.snippet.startswith("A propulsive")  # verdict replaces the feed excerpt
    assert r.pros and r.cons
    assert r.url == "https://aiptcomics.com/2024/05/01/saga-66-review/"  # Read more link kept


# --- rolling review store -------------------------------------------------------------------


def test_merge_reviews_dedupes_preserves_first_seen_and_prunes():
    today = date(2024, 5, 10)
    existing = [
        {"url": "a", "series": "X", "issue": "1", "published": "2024-05-03", "first_seen": "2024-05-03"},
        {"url": "old", "series": "Y", "issue": "2", "published": "2024-01-01", "first_seen": "2024-01-01"},
    ]
    fresh = [
        {"url": "a", "series": "X", "issue": "1", "published": "2024-05-03"},  # dupe
        {"url": "b", "series": "Z", "issue": "3", "published": "2024-05-09"},  # new
    ]
    merged = merge_entries(existing, fresh, today, retention_days=90)
    urls = [e["url"] for e in merged]
    assert urls == ["b", "a"]  # newest first; "old" pruned (>90d); "a" deduped
    assert next(e for e in merged if e["url"] == "a")["first_seen"] == "2024-05-03"  # preserved


NEWISH_XML = b"""<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Newish Thing #1 review</title><link>https://aipt/newish-1-review/</link>
  <category>Reviews</category><pubDate>Wed, 08 May 2024 12:00:00 +0000</pubDate>
  <description>A debut.</description></item>
</channel></rss>"""


def test_rolling_store_surfaces_prior_week_once_indexed(tmp_path):
    class GW:
        def search_issues(self, name, number=None):
            if "Saga" in name and number == "66":  # last week's review — now CV-indexed
                return [{"id": 700, "number": "66", "store_date": "2024-05-01",
                         "publisher": "Image Comics", "cv_id": 880000,
                         "series": {"id": 7777, "name": "Saga", "year_began": 2012, "cv_id": 18029}}]
            return []  # this week's review is still unindexed

    store = RollingStore(tmp_path / "reviews.json")
    store.save({"aipt-reviews": [{"url": "https://aipt/saga-66-review/", "series": "Saga",
                                  "issue": "66", "published": "2024-05-01", "first_seen": "2024-05-01"}]})
    out = RSSSource(GW(), today=date(2024, 5, 10), feeds=[TEST_FEED],
                    fetch=lambda url: NEWISH_XML, review_store=store).run()

    keys = [it.entity for s in out.sections for it in s.items]
    # The prior-week Saga (not in this week's feed) surfaces from the store, now fully resolved.
    assert "cv-issue-4000-880000" in keys
    # This week's review is accumulated into the store for future runs (and grows the pool).
    assert any(e["url"] == "https://aipt/newish-1-review/" for e in store.load()["aipt-reviews"])

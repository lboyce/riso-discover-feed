"""RSS review department source — title parsing, reviews-bias filter, assembly. Fully offline.

A fixture feed is parsed by real feedparser (via an injected fetch); a fake gateway resolves the
Saga review and fails the obscure indie one, exercising both the pullable and editorial-fallback
paths. The preview and news items must be filtered out (reviews-bias).
"""

from datetime import date
from pathlib import Path

import pytest

from riso_discover.sources.rss import (
    Feed,
    RSSSource,
    _clean_snippet,
    parse_review_title,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FEED_XML = (FIXTURES / "rss_reviews.xml").read_bytes()

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
    )
    return source.run()


def test_only_reviews_kept_preview_and_news_filtered():
    out = _run()
    section = next(s for s in out.sections if s.id == "aipt-reviews")
    assert section.type == "rss_department"
    assert len(section.items) == 2  # Saga review + obscure review; preview & news dropped


def test_resolved_review_is_pullable_with_review_signal():
    out = _run()
    e = out.entities["cv-issue-4000-880000"]
    assert e.kind == "issue"
    assert e.ids.comicvine_issue == "4000-880000"
    assert e.ids.comicvine_volume == "4050-18029"
    assert e.resolution.confidence == "high"

    section = next(s for s in out.sections if s.id == "aipt-reviews")
    item = next(i for i in section.items if i.entity == "cv-issue-4000-880000")
    assert item.reason.type == "review_signal"
    assert item.reason.source == "AIPT"
    assert item.reason.url == "https://aiptcomics.com/2024/05/01/saga-66-review/"
    assert item.reason.image == "https://img.example/saga66-thumb.jpg"
    assert "Brian K. Vaughan" in item.reason.snippet
    assert "<" not in item.reason.snippet  # HTML stripped, never full body


def test_unresolved_review_degrades_to_editorial_link():
    out = _run()
    # Keyed by the article URL (rss-<hash>), not a book id.
    editorial = [k for k in out.entities if k.startswith("rss-")]
    assert len(editorial) == 1
    e = out.entities[editorial[0]]
    assert e.resolution.confidence == "unresolved"
    assert e.series_name == "Obscure Indie Thing"
    assert e.issue_number == "1"
    assert e.ids.comicvine_issue is None
    assert e.ids.source_url == "https://aiptcomics.com/2024/05/02/obscure-indie-thing-1-review/"

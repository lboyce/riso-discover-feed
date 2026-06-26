"""CBR curation shelves — parse (with scores), balanced select, three-shelf assembly. Offline."""

from datetime import date
from pathlib import Path

from riso_discover.cache import JsonCache
from riso_discover.sources.cbr import CBRSource, parse_issue_quotes, parse_list, select_picks
from riso_discover.store import RollingStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HTML = (FIXTURES / "cbr_highest_rated.html").read_text("utf-8")
ISSUE_HTML = (FIXTURES / "cbr_issue.html").read_text("utf-8")
_NO_CACHE = JsonCache("test", enabled=False)


# --- critic quotes ---------------------------------------------------------------------------


def test_parse_issue_quotes_takes_top_two_by_score():
    quotes = parse_issue_quotes(ISSUE_HTML, limit=2)
    assert [q["outlet"] for q in quotes] == ["Geek Dad", "Comic Watch"]  # 10.0, 9.5 (8.0 dropped)
    top = quotes[0]
    assert top["reviewer"] == "Ray Goldfield"
    assert top["score"] == 10.0
    assert top["url"] == "https://geekdad.example/review"
    assert top["excerpt"].startswith("The story is great") and "<" not in top["excerpt"]
    assert "Read full review" not in top["excerpt"]  # trailing link text not captured


def test_shelf_items_carry_two_critic_quotes():
    out = _run()
    acc = next(s for s in out.sections if s.id == "critically-acclaimed")
    for it in acc.items:
        quotes = it.reason.quotes
        assert quotes and len(quotes) == 2
        assert all(q.outlet and q.excerpt for q in quotes)
        assert quotes[0].score >= (quotes[1].score or 0)  # highest-scored first


# --- parse (now WITH score + review_count) ---------------------------------------------------


def test_parse_list_extracts_score_and_count():
    cands = parse_list(HTML)
    assert len(cands) == 5
    by_series = {c.series: c for c in cands}
    ga = by_series["Absolute Green Arrow"]
    assert ga.issue == "2" and ga.publisher == "DC Comics" and ga.year == 2026
    assert ga.score == 9.2 and ga.review_count == 11
    assert ga.href == "/comic-books/reviews/dc-comics/absolute-green-arrow-(2026)/2"
    spidey = by_series["Spider-Man: Long Way Home"]
    assert spidey.score == 8.5 and spidey.publisher == "Marvel"


def test_select_picks_balanced_and_week_varied():
    cands = parse_list(HTML)
    picks = select_picks(cands, week=0)
    assert len(picks) == 5
    assert len({p.publisher for p in picks[:3]}) == 3  # publisher-diverse up front
    # Week seed changes the order deterministically.
    assert [p.series for p in select_picks(cands, 0)] != [p.series for p in select_picks(cands, 1)]


# --- assembly ---------------------------------------------------------------------------------


class FakeGateway:
    def search_issues(self, series_name, issue_number=None):
        known = {
            ("Absolute Green Arrow", "2"): (770000, "DC Comics", 99001),
            ("Spider-Man: Long Way Home", "1"): (770001, "Marvel", 99002),
            ("Nightwing", "139"): (770002, "DC Comics", 99003),
            ("Uncanny X-Men", "30"): (770003, "Marvel", 99004),
            ("If Destruction Be Our Lot", "2"): (770004, "Image Comics", 99005),
        }
        hit = known.get((series_name, issue_number))
        if not hit:
            return []
        cv, pub, scv = hit
        # Uncanny X-Men #30 simulates freshness lag: resolves to a volume but NO issue cv_id.
        issue_cv = None if series_name == "Uncanny X-Men" else cv
        return [{
            "id": cv, "number": issue_number, "store_date": "2026-06-20", "publisher": pub,
            "cv_id": issue_cv, "isbn": None, "upc": None, "gcd_id": None,
            "image": f"https://static.metron.cloud/cover-{cv}.jpg",
            "series": {"id": scv, "name": series_name, "year_began": 2026, "cv_id": scv},
        }]


def _run(show_rating=True):
    src = CBRSource(FakeGateway(), today=date(2026, 6, 24), show_rating=show_rating,
                    fetch=lambda url: HTML,  # both lists return the same fixture
                    issue_fetch=lambda url: ISSUE_HTML,  # every issue page returns the quote fixture
                    quotes_cache=_NO_CACHE, store=RollingStore(None))
    return src.run()


def test_three_shelves_present():
    out = _run()
    ids = [s.id for s in out.sections]
    assert set(ids) == {"riso-recommends", "critically-acclaimed", "popular"}


def test_match_gate_excludes_freshness_lagged_picks():
    out = _run()
    # Uncanny X-Men #30 resolved to a volume but no comicvine_issue (freshness lag) -> never featured.
    titles = {e.title for e in out.entities.values()}
    assert not any("Uncanny X-Men" in t for t in titles)
    # Every featured pick on every shelf carries a ComicVine issue id (matchable).
    for s in out.sections:
        for it in s.items:
            assert out.entities[it.entity].ids.comicvine_issue is not None


def test_critically_acclaimed_shows_score_and_credits_cbr():
    out = _run()
    acc = next(s for s in out.sections if s.id == "critically-acclaimed")
    assert acc.source == "Comic Book Roundup"
    it = acc.items[0]
    assert it.reason.score is not None and it.reason.score_max == 10.0
    assert it.reason.review_count is not None
    assert it.reason.url.startswith("https://comicbookroundup.com/comic-books/reviews/")
    # The Metron cover carries through so unowned picks still render in the mosaic.
    assert out.entities[it.entity].cover_url.startswith("https://static.metron.cloud/")


def test_riso_recommends_is_riso_sourced_no_cbr_link():
    out = _run()
    rec = next(s for s in out.sections if s.id == "riso-recommends")
    assert rec.source == "RISO"
    assert all(it.reason.url is None for it in rec.items)  # RISO pick, no CBR link
    assert all(it.reason.score is not None for it in rec.items)  # still shows the quality score


def test_show_rating_off_omits_scores_and_cbr():
    out = _run(show_rating=False)
    for s in out.sections:
        for it in s.items:
            assert it.reason.score is None
            assert it.reason.url is None
            assert it.reason.source == "RISO"

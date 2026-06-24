"""CBR automated curator — parse (signal only), select (independent), assemble. Fully offline.

Verifies the §8 cleanliness rules: scores are never captured, output order differs from CBR's, and
sections are framed as RISO's picks (personal-tier, no CBR reference).
"""

from datetime import date
from pathlib import Path

from riso_discover.sources.cbr import CBRSource, parse_list, select_picks

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HTML = (FIXTURES / "cbr_highest_rated.html").read_text("utf-8")


# --- parse (signal extraction, no scores) ---------------------------------------------------


def test_parse_list_extracts_signal_not_scores():
    cands = parse_list(HTML)
    assert len(cands) == 5
    by_series = {c.series: c for c in cands}
    ga = by_series["Absolute Green Arrow"]
    assert ga.issue == "2" and ga.publisher == "DC Comics" and ga.year == 2026
    spidey = by_series["Spider-Man: Long Way Home"]
    assert spidey.issue == "1" and spidey.publisher == "Marvel"
    # Candidate carries no score/rating field at all.
    assert set(vars(ga)) == {"series", "issue", "publisher", "year"}
    # Non-entry nav links (most-pulled, publisher page) are ignored.
    assert all("Most Pulled" not in c.series for c in cands)


# --- select (independent: diversity + week rotation, reordered) ------------------------------


def _cands():
    return parse_list(HTML)  # input order: DC, DC, Marvel, Marvel, Image


def test_select_picks_is_publisher_diverse_and_reordered():
    picks = select_picks(_cands(), week=0)
    assert len(picks) == 5
    # First three picks span three different publishers (one-per-publisher spacing).
    assert len({p.publisher for p in picks[:3]}) == 3
    # Output order differs from CBR's input order.
    assert [p.series for p in picks] != [c.series for c in _cands()]


def test_select_picks_week_rotation_changes_order():
    assert [p.series for p in select_picks(_cands(), 0)] != [
        p.series for p in select_picks(_cands(), 1)
    ]


def test_select_picks_empty():
    assert select_picks([], week=3) == []


# --- assembly -------------------------------------------------------------------------------


class FakeGateway:
    def search_issues(self, series_name, issue_number=None):
        if "Green Arrow" in series_name and issue_number == "2":
            return [{
                "id": 900, "number": "2", "store_date": "2026-06-17", "publisher": "DC Comics",
                "cv_id": 770000, "isbn": None, "upc": None, "gcd_id": None, "image": None,
                "series": {"id": 555, "name": "Absolute Green Arrow", "year_began": 2026, "cv_id": 99001},
            }]
        if "Long Way Home" in series_name and issue_number == "1":
            return [{
                "id": 901, "number": "1", "store_date": "2026-06-24", "publisher": "Marvel",
                "cv_id": 770001, "isbn": None, "upc": None, "gcd_id": None, "image": None,
                "series": {"id": 556, "name": "Spider-Man: Long Way Home", "year_began": 2026, "cv_id": 99002},
            }]
        return []  # everything else unresolved -> skipped


def _run():
    src = CBRSource(FakeGateway(), today=date(2026, 6, 24), fetch=lambda url: HTML)
    return src.run()


def test_sections_are_riso_picks_personal_tier():
    out = _run()
    ids = {s.id for s in out.sections}
    assert ids == {"featured-books", "trending"}
    feat = next(s for s in out.sections if s.id == "featured-books")
    assert feat.type == "featured_picks"
    assert feat.source_tier == "personal"
    assert feat.source == "RISO"
    # Only the two resolvable picks survive; the rest were skipped (must be pullable).
    assert len(feat.items) == 2
    for it in feat.items:
        assert it.reason.type == "featured_pick"
        assert it.reason.source == "RISO"
        assert it.reason.url is None  # no CBR reference anywhere


def test_picks_resolve_to_pullable_entities():
    out = _run()
    assert "cv-issue-4000-770000" in out.entities  # Absolute Green Arrow #2
    e = out.entities["cv-issue-4000-770000"]
    assert e.ids.comicvine_issue == "4000-770000"

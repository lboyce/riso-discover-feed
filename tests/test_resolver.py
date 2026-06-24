"""Resolver tests — the disambiguation logic especially (CLAUDE.md Section 11)."""

import json
from datetime import date
from pathlib import Path

from riso_discover.resolver import (
    Candidate,
    disambiguate,
    resolve_metron_issue,
    resolve_query,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- direct path ----------------------------------------------------------------------------


def test_resolve_metron_issue_high_confidence_with_cv_id():
    detail = json.loads((FIXTURES / "metron_issue_detail.json").read_text("utf-8"))
    entity = resolve_metron_issue(detail, series_cv_id=145678)
    assert entity.resolution.confidence == "high"
    assert entity.ids.comicvine_issue == "4000-987654"
    assert entity.ids.comicvine_volume == "4050-145678"
    assert entity.ids.metron_issue == 56789
    assert entity.title == "Teenage Mutant Ninja Turtles: Shredder #9"
    assert entity.publisher == "IDW Publishing"
    assert entity.release_date == "2026-06-24"


def test_resolve_metron_issue_partial_without_cv_id():
    detail = {
        "id": 56999,
        "number": "1",
        "store_date": "2026-06-24",
        "publisher": "Small Press Co.",
        "cv_id": None,
        "series": {"id": 9876, "name": "Indie Spotlight", "year_began": 2026},
    }
    entity = resolve_metron_issue(detail)
    assert entity.resolution.confidence == "partial"
    assert entity.resolution.issue_pending is False  # nothing to wait on: only Metron ids
    assert entity.ids.comicvine_issue is None
    assert entity.ids.metron_issue == 56999


def test_resolve_metron_issue_freshness_lag_marks_issue_pending():
    # Brand-new issue: the volume is cross-referenced to ComicVine but the issue isn't yet (§6.5).
    detail = {
        "id": 170929,
        "number": "20",
        "store_date": "2026-06-24",
        "publisher": "DC Comics",
        "cv_id": None,
        "series": {"id": 160860, "name": "Absolute Superman", "year_began": 2024},
    }
    entity = resolve_metron_issue(detail, series_cv_id=160860)
    assert entity.resolution.confidence == "partial"
    assert entity.resolution.issue_pending is True
    assert entity.ids.comicvine_issue is None
    assert entity.ids.comicvine_volume == "4050-160860"


# --- disambiguation (the TMNT: Shredder trap) -----------------------------------------------


def _tmnt_candidates():
    # The 2019 five-issue miniseries vs the 2025 ongoing. A #9 cannot exist in a 5-issue mini.
    mini = Candidate(
        issue_id=1,
        number="9",
        store_date="2019-08-14",
        publisher="IDW Publishing",
        cv_id=None,
        series_id=900,
        series_name="Teenage Mutant Ninja Turtles: Shredder in Hell",
        volume_year=2019,
        issue_count=5,
    )
    ongoing = Candidate(
        issue_id=2,
        number="9",
        store_date="2026-06-24",
        publisher="IDW Publishing",
        cv_id=987654,
        series_id=1234,
        series_name="Teenage Mutant Ninja Turtles: Shredder",
        volume_year=2025,
        issue_count=None,
    )
    return mini, ongoing


def test_disambiguate_picks_ongoing_not_mini():
    mini, ongoing = _tmnt_candidates()
    best, confidence = disambiguate(
        [mini, ongoing],
        issue_number="9",
        publisher_hint="IDW Publishing",
        date_hint=date(2026, 6, 20),
    )
    assert best is ongoing
    assert confidence == "high"


def test_disambiguate_empty_is_unresolved():
    best, confidence = disambiguate([], issue_number="9")
    assert best is None
    assert confidence == "unresolved"


def test_disambiguate_wrong_number_is_unresolved():
    mini, _ = _tmnt_candidates()
    # Asking for #2 against only a candidate numbered #9 -> negative score -> declined.
    best, confidence = disambiguate([mini], issue_number="2", date_hint=date(2026, 6, 20))
    assert confidence == "unresolved"
    assert best is None


# --- fuzzy path through a fake searcher ------------------------------------------------------


class FakeSearcher:
    def __init__(self, rows):
        self.rows = rows

    def search_issues(self, series_name, issue_number):
        return self.rows


def test_resolve_query_resolves_via_searcher():
    rows = [
        {
            "id": 2,
            "number": "9",
            "store_date": "2026-06-24",
            "publisher": "IDW Publishing",
            "cv_id": 987654,
            "series": {"id": 1234, "name": "Teenage Mutant Ninja Turtles: Shredder",
                       "year_began": 2025, "cv_id": 145678},
        },
        {
            "id": 1,
            "number": "9",
            "store_date": "2019-08-14",
            "publisher": "IDW Publishing",
            "cv_id": None,
            "series": {"id": 900, "name": "TMNT: Shredder in Hell",
                       "year_began": 2019, "issue_count": 5},
        },
    ]
    entity, confidence = resolve_query(
        FakeSearcher(rows),
        "Teenage Mutant Ninja Turtles: Shredder",
        "9",
        publisher_hint="IDW Publishing",
        date_hint=date(2026, 6, 20),
    )
    assert confidence == "high"
    assert entity is not None
    assert entity.ids.comicvine_issue == "4000-987654"
    assert entity.ids.comicvine_volume == "4050-145678"


def test_resolve_query_unresolved_returns_none():
    entity, confidence = resolve_query(FakeSearcher([]), "Nonexistent Series", "1")
    assert entity is None
    assert confidence == "unresolved"

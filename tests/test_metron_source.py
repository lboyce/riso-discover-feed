"""New This Week assembled from a fake Metron client — offline, no network, no credentials."""

from datetime import date
from types import SimpleNamespace

from riso_discover.cache import JsonCache
from riso_discover.config import MetronCredentials
from riso_discover.sources.metron import MetronSource, upcoming_window, week_window


class FakeMetronClient:
    """Mimics the slice of mokkari the source uses: issues_list, issue(id), series(id)."""

    def __init__(self):
        self._issues = {
            101: SimpleNamespace(
                id=101,
                number="9",
                store_date=date(2026, 6, 24),
                image="https://static.metron.cloud/x/tmnt-9.jpg",
                publisher=SimpleNamespace(id=1, name="IDW Publishing"),
                cv_id=987654,
                gcd_id=None,
                isbn=None,
                upc="82771401601100911",
                series=SimpleNamespace(
                    id=1234, name="Teenage Mutant Ninja Turtles: Shredder", year_began=2025
                ),
            ),
            102: SimpleNamespace(
                id=102,
                number="1",
                store_date=date(2026, 6, 24),
                image=None,
                publisher=SimpleNamespace(id=2, name="Small Press Co."),
                cv_id=None,  # not yet cross-referenced to ComicVine
                gcd_id=None,
                isbn=None,
                upc=None,
                series=SimpleNamespace(id=9876, name="Indie Spotlight", year_began=2026),
            ),
        }
        self._series_cv = {1234: 145678, 9876: None}

    def issues_list(self, params):
        # New This Week window contains 2026-06-24; Upcoming windows return nothing.
        after = date.fromisoformat(params["store_date_range_after"])
        before = date.fromisoformat(params["store_date_range_before"])
        return [
            i for i in self._issues.values() if after <= i.store_date <= before
        ]

    def issue(self, issue_id):
        return self._issues[issue_id]

    def series(self, series_id):
        return SimpleNamespace(id=series_id, cv_id=self._series_cv.get(series_id))


def _source():
    return MetronSource(
        MetronCredentials("fake", "fake"),
        today=date(2026, 6, 24),
        cache=JsonCache("test", enabled=False),
        client=FakeMetronClient(),
    )


def test_windows():
    assert week_window(date(2026, 6, 24)) == (date(2026, 6, 22), date(2026, 6, 28))
    up_start, up_end = upcoming_window(date(2026, 6, 24))
    assert up_start == date(2026, 6, 29)
    assert up_end == date(2026, 7, 26)


def test_new_this_week_assembles_entities():
    out = _source().run()

    sections = {s.id: s for s in out.sections}
    assert "new-this-week" in sections
    assert "upcoming-releases" in sections

    new_this_week = sections["new-this-week"]
    assert new_this_week.type == "new_releases"
    assert new_this_week.source_tier == "distribution"
    assert len(new_this_week.items) == 2

    # The cv_id issue keys on its ComicVine id and resolves high; the other falls back to Metron.
    assert "cv-issue-4000-987654" in out.entities
    assert "metron-issue-102" in out.entities

    tmnt = out.entities["cv-issue-4000-987654"]
    assert tmnt.resolution.confidence == "high"
    assert tmnt.ids.comicvine_volume == "4050-145678"
    assert tmnt.publisher == "IDW Publishing"

    indie = out.entities["metron-issue-102"]
    assert indie.resolution.confidence == "partial"
    assert indie.ids.comicvine_issue is None

    # Upcoming window has no issues in this fixture.
    assert len(sections["upcoming-releases"].items) == 0

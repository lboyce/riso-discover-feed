"""Featured Classic — weekly rotation over the seed + series resolution. Fully offline."""

from datetime import date

from riso_discover.sources.classics import SEED, ClassicsSource, rotate_seed


# --- rotation -------------------------------------------------------------------------------


def test_rotate_seed_varies_by_week():
    w1 = rotate_seed(SEED, week=1)
    w2 = rotate_seed(SEED, week=2)
    assert len(w1) == len(SEED) == len(w2)  # full seed, just rotated
    assert [c.series for c in w1] != [c.series for c in w2]
    assert set(c.series for c in w1) == set(c.series for c in w2)  # same set, different order


def test_rotate_seed_wraps_and_handles_empty():
    assert len(rotate_seed(SEED, week=999)) == len(SEED)
    assert rotate_seed([], week=1) == []


# --- assembly -------------------------------------------------------------------------------


class FakeGateway:
    """Resolves only 'Saga' (a seed entry) to a series; everything else is unresolved."""

    def search_series(self, name):
        if name == "Saga":
            return [{"id": 10, "display_name": "Saga (2012)", "year_began": 2012,
                     "year_end": None, "issue_count": 60}]
        return []

    def series_detail(self, series_id):
        return {"id": 10, "name": "Saga", "year_began": 2012,
                "publisher": "Image Comics", "cv_id": 42042, "gcd_id": None}


def _run_all_featured():
    # Feature the whole seed so the one resolvable entry (Saga) is always included this run.
    src = ClassicsSource(FakeGateway(), today=date(2026, 1, 7), picks_per_week=len(SEED))
    return src.run()


def test_featured_classic_resolves_pullable_and_skips_unresolved():
    out = _run_all_featured()
    section = next(s for s in out.sections if s.id == "featured-classic")
    assert section.type == "featured_classic"
    assert section.source_tier == "distribution"
    # Only Saga resolves; the other unresolved seed entries are skipped.
    assert len(section.items) == 1
    e = out.entities["cv-volume-4050-42042"]
    assert e.kind == "series"
    assert e.ids.comicvine_volume == "4050-42042"
    assert section.items[0].reason.type == "classic"
    assert section.items[0].reason.source == "RISO"

"""Build orchestration: curation-first section ordering."""

from riso_discover.build import SECTION_ORDER, order_sections
from riso_discover.models import Section


def _section(sid):
    return Section(id=sid, type="featured_picks", title=sid, source_tier="distribution", source="x")


def test_order_sections_puts_curation_first():
    # Deliberately reversed input.
    given = [_section(s) for s in reversed(SECTION_ORDER)]
    ordered = [s.id for s in order_sections(given)]
    assert ordered == SECTION_ORDER
    # Curation shelves lead.
    assert ordered[:3] == ["riso-recommends", "critically-acclaimed", "popular"]
    # Raw new-release lists sink to the bottom.
    assert ordered[-2:] == ["new-this-week", "upcoming-releases"]


def test_unlisted_sections_sort_last_in_stable_order():
    given = [_section("new-this-week"), _section("mystery-a"), _section("riso-recommends"), _section("mystery-b")]
    ordered = [s.id for s in order_sections(given)]
    assert ordered[0] == "riso-recommends"
    assert ordered.index("new-this-week") < ordered.index("mystery-a")
    assert ordered.index("mystery-a") < ordered.index("mystery-b")  # stable for unlisted

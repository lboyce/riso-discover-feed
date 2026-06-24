"""Wikidata award source — parse SPARQL bindings and assemble award sections, fully offline.

A fake gateway resolves one winner (Something Is Killing the Children) to a Metron series and fails
the other (Lore Olympus, a webcomic) so we exercise both the pullable and editorial-fallback paths.
"""

import json
from datetime import date
from pathlib import Path

from riso_discover.sources.wikidata import WikidataSource

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BINDINGS = json.loads((FIXTURES / "wikidata_eisner.json").read_text("utf-8"))


class FakeGateway:
    """Implements the SeriesSearcher protocol; resolves only the SiKtC series."""

    def search_series(self, name):
        if "Killing" in name:
            return [{
                "id": 100,
                "display_name": "Something Is Killing the Children (2019)",
                "year_began": 2019, "year_end": None, "issue_count": 36,
            }]
        return []  # Lore Olympus -> no Metron series -> unresolved

    def series_detail(self, series_id):
        return {
            "id": 100, "name": "Something Is Killing the Children", "year_began": 2019,
            "publisher": "BOOM! Studios", "series_type": "Ongoing", "cv_id": 120000, "gcd_id": None,
        }


def _fake_sparql(query):
    # Only the Eisner query returns winners; Harvey/Ringo come back empty.
    return BINDINGS if "Eisner Award" in query else []


def _run():
    source = WikidataSource(FakeGateway(), today=date(2026, 6, 24), sparql=_fake_sparql)
    return source.run()


def test_sections_one_per_award():
    out = _run()
    ids = {s.id for s in out.sections}
    assert ids == {"eisner-winners", "harvey-winners", "ringo-winners"}
    by_id = {s.id: s for s in out.sections}
    assert by_id["eisner-winners"].type == "award_winners"
    assert len(by_id["eisner-winners"].items) == 2
    assert len(by_id["harvey-winners"].items) == 0
    assert len(by_id["ringo-winners"].items) == 0


def test_resolved_winner_is_pullable_series():
    out = _run()
    e = out.entities["cv-volume-4050-120000"]
    assert e.kind == "series"
    assert e.format is None
    assert e.ids.comicvine_volume == "4050-120000"
    assert e.ids.metron_series == 100
    assert e.ids.wikidata_id == "Q60799410"
    assert e.resolution.confidence == "high"


def test_unresolved_winner_degrades_to_editorial():
    out = _run()
    assert "wd-Q110881556" in out.entities
    e = out.entities["wd-Q110881556"]
    assert e.resolution.confidence == "unresolved"
    assert e.ids.comicvine_volume is None
    assert e.ids.wikidata_id == "Q110881556"

    # Its section item carries the award label + a Wikipedia link-back.
    eisner = next(s for s in out.sections if s.id == "eisner-winners")
    item = next(i for i in eisner.items if i.entity == "wd-Q110881556")
    assert item.reason.type == "award"
    assert item.reason.label == "Eisner Award for Best Webcomic (2022)"
    assert item.reason.url == "https://en.wikipedia.org/wiki/Lore_Olympus"

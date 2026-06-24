"""Classics — the Featured Classic evergreen seed (CLAUDE.md §10).

A one-time, hand-authored canon seed that rotates weekly — a seed, not ongoing curation. Each week
we feature a few canonical classics, chosen by a week-number offset over the seed, and resolve them
to Metron series so they're pullable. Evergreen and distribution-clean; the annual maintenance is
just curating the seed list.

Deferred (see CLAUDE.md §14): Trades / Collected Editions and New Editions / Reissues. Verified live
that Metron's store_date `issues_list` returns only single-issue-type series, its `series_type` filter
is ignored, and `series_list` by collected type + year yields nothing — so neither has a clean Metron
path yet. A dedicated collected-editions feed (candidate: the GCD data dumps, §7) is a follow-up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..metron_gateway import MetronGateway
from ..models import Item, Reason, Section, entity_key
from ..resolver import resolve_series
from .base import BaseSource, SourceOutput

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Classic:
    series: str
    year: Optional[int]  # volume year, to disambiguate the canonical run


# Evergreen canon seed. Hand-authored; rotates weekly. Curate annually (add to the canon); the year
# pins the disambiguation to the canonical run.
SEED = [
    Classic("Watchmen", 1986),
    Classic("The Sandman", 1989),
    Classic("Saga", 2012),
    Classic("Y: The Last Man", 2002),
    Classic("Preacher", 1995),
    Classic("Transmetropolitan", 1997),
    Classic("Fables", 2002),
    Classic("Hellboy", 1994),
    Classic("Sin City", 1991),
    Classic("Bone", 1991),
    Classic("Invincible", 2003),
    Classic("The Walking Dead", 2003),
    Classic("Sweet Tooth", 2009),
    Classic("Locke & Key", 2008),
    Classic("Paper Girls", 2015),
    Classic("Monstress", 2015),
    Classic("East of West", 2013),
    Classic("Descender", 2015),
    Classic("Daytripper", 2010),
    Classic("Black Hole", 1995),
]


def rotate_seed(seed: list[Classic], week: int) -> list[Classic]:
    """Rotate the seed so each week starts at a different point. Pure function. The source then
    resolves down this order until it has enough *resolved* picks, so the section stays full even
    when some canon entries don't resolve in a given run."""
    if not seed:
        return []
    start = week % len(seed)
    return seed[start:] + seed[:start]


class ClassicsSource(BaseSource):
    name = "classics"
    tier = "distribution"

    def __init__(
        self, gateway: MetronGateway, *, today: date, picks_per_week: int = 3
    ):
        self.gateway = gateway
        self.today = today
        self.picks_per_week = picks_per_week

    def run(self) -> SourceOutput:
        out = SourceOutput()
        section = Section(
            id="featured-classic",
            type="featured_classic",
            title="Featured Classic",
            source_tier="distribution",
            source="RISO",
        )
        week = self.today.isocalendar().week
        seen: set[str] = set()
        for classic in rotate_seed(SEED, week):
            if len(section.items) >= self.picks_per_week:
                break
            try:
                entity, _conf = resolve_series(
                    self.gateway, classic.series, year_hint=classic.year
                )
            except Exception as exc:  # one bad lookup must not abort the rest
                log.warning("Classic %s failed to resolve: %s", classic.series, exc)
                continue
            if entity is None:
                continue  # a featured classic must be pullable; skip if unresolved this run
            key = entity_key(entity.ids)
            if key in seen:
                continue
            seen.add(key)
            out.entities[key] = entity
            section.items.append(
                Item(entity=key, reason=Reason(type="classic", source="RISO", label="Featured Classic"))
            )
        out.sections.append(section)
        log.info("Classics featured-classic: %d picks", len(section.items))
        return out

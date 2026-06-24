"""Wikidata source — Award Winners (Eisner, Harvey, Ringo).

Reads award winners from Wikidata via SPARQL, then resolves each to a canonical Metron *series*
(the cv_id bridge) so they're pullable; unresolved winners (webcomics, prose, gaps) degrade to an
editorial-only entry with a Wikipedia link (CLAUDE.md §6 graceful degradation).

Modeling notes (verified against the live Wikidata Query Service):
- Winners are ``?work p:P166/ps:P166 ?awardCategory`` with a ``P585`` year qualifier.
- The raw result mixes works and people (craft categories like "Best Writer" go to humans), so we
  filter to works with ``?work wdt:P31 ?type . FILTER(?type != wd:Q5)`` (exclude humans).
- Coverage is sparse/uneven by year, so we take a multi-year window (default: last 5 years).

distribution-clean: Wikidata is openly licensed; we emit titles, an award label, and a link back.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from ..models import Entity, Ids, Item, Reason, Resolution, Section, entity_key
from ..resolver import SeriesSearcher, resolve_series
from .base import BaseSource, SourceOutput

log = logging.getLogger(__name__)

WQS_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "riso-discover-feed/0.1 (https://github.com/lboyce/riso-discover-feed; lukeslens@gmail.com)"


@dataclass(frozen=True)
class Award:
    label_contains: str  # matched against the award category's English label
    section_id: str
    section_title: str


# Eisner / Harvey / Ringo. The CONTAINS match plus the works-only filter keeps book/series/album
# categories and naturally drops craft/person categories ("Best Writer", "Best Cover Artist", ...).
AWARDS = [
    Award("Eisner Award", "eisner-winners", "Eisner Award Winners"),
    Award("Harvey Award", "harvey-winners", "Harvey Award Winners"),
    Award("Ringo Award", "ringo-winners", "Ringo Award Winners"),
]

_SPARQL_TEMPLATE = """
SELECT ?work ?workLabel ?awardLabel ?year ?publisherLabel ?isbn ?article WHERE {{
  ?work p:P166 ?st .
  ?st ps:P166 ?award .
  ?award rdfs:label ?al .
  FILTER(LANG(?al) = "en")
  FILTER(STRSTARTS(?al, "{label_contains}"))
  ?work wdt:P31 ?type .
  FILTER(?type != wd:Q5)
  ?st pq:P585 ?date .
  BIND(YEAR(?date) AS ?year)
  FILTER(?year >= {year_floor})
  OPTIONAL {{ ?work wdt:P123 ?publisher . }}
  OPTIONAL {{ ?work wdt:P212 ?isbn . }}
  OPTIONAL {{ ?article schema:about ?work ; schema:isPartOf <https://en.wikipedia.org/> . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
ORDER BY DESC(?year)
"""


@dataclass
class AwardWinner:
    qid: str
    title: str
    category: str
    year: Optional[int]
    publisher: Optional[str]
    isbn: Optional[str]
    url: Optional[str]


def _binding_value(binding: dict, key: str) -> Optional[str]:
    cell = binding.get(key)
    return cell.get("value") if cell else None


def _qid_from_uri(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


class WikidataSource(BaseSource):
    name = "wikidata"
    tier = "distribution"

    def __init__(
        self,
        searcher: SeriesSearcher,
        *,
        today: date,
        years_back: int = 5,
        sparql: Optional[Callable[[str], list[dict]]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.searcher = searcher
        self.today = today
        self.years_back = years_back
        self._sparql = sparql or self._default_sparql
        self._sleep = sleep  # injectable so tests never actually sleep

    # -- SPARQL -------------------------------------------------------------------------------
    def _default_sparql(self, query: str) -> list[dict]:
        """GET the WQS endpoint with bounded retry. WQS occasionally rate-limits (429) or times out,
        especially during incidents; a couple of backed-off retries recover transient failures
        without stalling the weekly run for long. Persistent failure raises and the caller logs +
        skips that award (graceful degradation)."""
        url = WQS_ENDPOINT + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
        )
        backoffs = [10.0, 30.0]  # two retries, then give up
        for attempt in range(len(backoffs) + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                return data.get("results", {}).get("bindings", [])
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= len(backoffs):
                    raise
                wait = backoffs[attempt]
                log.info("WQS request failed (%s); retrying in %.0fs", exc, wait)
                self._sleep(wait)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _winners(self, award: Award) -> list[AwardWinner]:
        year_floor = self.today.year - self.years_back + 1
        query = _SPARQL_TEMPLATE.format(
            label_contains=award.label_contains, year_floor=year_floor
        )
        bindings = self._sparql(query)

        # Dedupe by work QID (a work can appear under several years/types/publishers). Keep the most
        # recent year and the richest fields.
        by_qid: dict[str, AwardWinner] = {}
        for b in bindings:
            work_uri = _binding_value(b, "work")
            title = _binding_value(b, "workLabel")
            if not work_uri or not title:
                continue
            qid = _qid_from_uri(work_uri)
            year = _binding_value(b, "year")
            year_int = int(year) if year and year.isdigit() else None
            cand = AwardWinner(
                qid=qid,
                title=title,
                category=_binding_value(b, "awardLabel") or award.label_contains,
                year=year_int,
                publisher=_binding_value(b, "publisherLabel"),
                isbn=_binding_value(b, "isbn"),
                url=_binding_value(b, "article"),
            )
            prev = by_qid.get(qid)
            if prev is None or (cand.year or 0) > (prev.year or 0):
                # Carry forward any fields the newer row is missing.
                if prev:
                    cand = AwardWinner(
                        qid=qid,
                        title=cand.title,
                        category=cand.category,
                        year=cand.year,
                        publisher=cand.publisher or prev.publisher,
                        isbn=cand.isbn or prev.isbn,
                        url=cand.url or prev.url,
                    )
                by_qid[qid] = cand
        return list(by_qid.values())

    # -- entity building ----------------------------------------------------------------------
    def _entity_for(self, w: AwardWinner) -> tuple[str, Entity]:
        entity, _confidence = resolve_series(
            self.searcher, w.title, publisher_hint=w.publisher, year_hint=w.year
        )
        if entity is not None:
            # Augment with the Wikidata origin and any ISBN the award record carried.
            entity.ids.wikidata_id = w.qid
            if w.isbn and not entity.ids.isbn:
                entity.ids.isbn = w.isbn
            return entity_key(entity.ids), entity

        # Graceful degradation: editorial-only entry, keyed by Wikidata QID (not pullable).
        ids = Ids(wikidata_id=w.qid, isbn=w.isbn or None, series_name=w.title)
        entity = Entity(
            kind="series",
            title=w.title,
            series_name=w.title,
            publisher=w.publisher,
            format=None,
            ids=ids,
            resolution=Resolution(confidence="unresolved", issue_pending=False),
        )
        return entity_key(ids), entity

    # -- main entry ---------------------------------------------------------------------------
    def run(self) -> SourceOutput:
        out = SourceOutput()
        for award in AWARDS:
            section = Section(
                id=award.section_id,
                type="award_winners",
                title=award.section_title,
                source_tier="distribution",
                source="Wikidata",
            )
            try:
                winners = self._winners(award)
            except Exception as exc:  # a single award query failing must not crash the source
                log.warning("Wikidata query for %s failed: %s", award.section_title, exc)
                out.sections.append(section)
                continue

            for w in winners:
                try:
                    key, entity = self._entity_for(w)
                except Exception as exc:  # one bad winner must not abort the rest
                    log.warning("Wikidata winner %s (%s) failed to resolve: %s", w.title, w.qid, exc)
                    continue
                out.entities.setdefault(key, entity)
                section.items.append(
                    Item(
                        entity=key,
                        reason=Reason(
                            type="award",
                            source="Wikidata",
                            label=f"{w.category} ({w.year})" if w.year else w.category,
                            url=w.url,
                        ),
                    )
                )
            out.sections.append(section)
            log.info("Wikidata %s: %d winners", award.section_id, len(section.items))
        return out

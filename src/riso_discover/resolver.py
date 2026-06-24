"""The resolution pipeline — the hardest and most important component (CLAUDE.md Section 6).

Two entry points share one confidence model:

* ``resolve_metron_issue`` — the *direct* path. Given a full Metron issue record (already a canonical
  Metron entity, e.g. from a store_date query), lift the ComicVine id and assemble an Entity. Used by
  New This Week / Upcoming.
* ``resolve_query`` — the *fuzzy* path for editorial sources that only have a "Series #N" string. It
  queries Metron and disambiguates candidates by publisher, store-date proximity, and issue-number
  plausibility, then gates on confidence. The scoring is a pure function (``score_candidate`` /
  ``disambiguate``) so the disambiguation logic is unit-testable without the network.

Guiding rule: never emit a wrong match, never fail loudly. Unresolved candidates degrade to an
editorial-link-only entity (not pullable) rather than raising.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Protocol

from .models import (
    Confidence,
    Entity,
    Ids,
    Resolution,
    comicvine_issue_id,
    comicvine_volume_id,
)

# --- normalization helpers ------------------------------------------------------------------


def _publisher_name(value: Any) -> Optional[str]:
    """Metron publisher may be a {'id','name'} dict or a bare string."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("name")
    return str(value)


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _issue_title(series_name: Optional[str], number: Optional[str]) -> str:
    if series_name and number:
        return f"{series_name} #{number}"
    return series_name or (f"#{number}" if number else "Untitled")


# --- direct path ----------------------------------------------------------------------------


def resolve_metron_issue(issue: dict, *, series_cv_id: Optional[int] = None) -> Entity:
    """Build an Entity from a full Metron issue record.

    ``series_cv_id`` is the ComicVine id of the issue's *series* (fetched separately and cached),
    used to fill comicvine_volume. It is best-effort; absence does not lower confidence.
    """
    series = issue.get("series") or {}
    series_name = series.get("name")
    number = None if issue.get("number") is None else str(issue.get("number"))
    cv_id = issue.get("cv_id")

    ids = Ids(
        comicvine_issue=comicvine_issue_id(cv_id) if cv_id else None,
        comicvine_volume=comicvine_volume_id(series_cv_id) if series_cv_id else None,
        metron_issue=issue.get("id"),
        metron_series=series.get("id"),
        isbn=issue.get("isbn") or None,
        upc=issue.get("upc") or None,
        gcd_id=issue.get("gcd_id") or None,
        series_name=series_name,
        volume_year=series.get("year_began"),
    )

    # cv_id present -> Komga-matchable at the issue level -> high.
    # No issue cv_id but the volume resolved -> partial, and the issue is "pending": the volume is
    # known but ComicVine hasn't cross-referenced this issue yet (the freshness-lag case, §6.5).
    # Neither -> partial, but nothing to wait on (only Metron ids), so not pending.
    confidence: Confidence
    if cv_id:
        confidence, issue_pending = "high", False
    elif series_cv_id:
        confidence, issue_pending = "partial", True
    else:
        confidence, issue_pending = "partial", False

    return Entity(
        kind="issue",
        title=_issue_title(series_name, number),
        series_name=series_name,
        issue_number=number,
        publisher=_publisher_name(issue.get("publisher")),
        format="single_issue",
        cover_url=issue.get("image") or None,
        release_date=str(issue["store_date"]) if issue.get("store_date") else None,
        ids=ids,
        resolution=Resolution(confidence=confidence, issue_pending=issue_pending),
    )


# --- fuzzy path: disambiguation (pure, testable) --------------------------------------------


@dataclass
class Candidate:
    """A normalized Metron issue candidate for the fuzzy resolver."""

    issue_id: int
    number: Optional[str]
    store_date: Optional[str]
    publisher: Optional[str]
    cv_id: Optional[int]
    series_id: Optional[int]
    series_name: Optional[str]
    volume_year: Optional[int]
    series_cv_id: Optional[int] = None
    # Number of issues in the series, when known (a 5-issue mini -> 5). Used for plausibility.
    issue_count: Optional[int] = None

    @classmethod
    def from_metron(cls, issue: dict) -> "Candidate":
        series = issue.get("series") or {}
        number = None if issue.get("number") is None else str(issue.get("number"))
        return cls(
            issue_id=issue.get("id"),
            number=number,
            store_date=str(issue["store_date"]) if issue.get("store_date") else None,
            publisher=_publisher_name(issue.get("publisher")),
            cv_id=issue.get("cv_id"),
            series_id=series.get("id"),
            series_name=series.get("name"),
            volume_year=series.get("year_began"),
            series_cv_id=series.get("cv_id"),
            issue_count=series.get("issue_count"),
        )


def _number_value(number: Optional[str]) -> Optional[float]:
    if number is None:
        return None
    try:
        return float(number)
    except ValueError:
        return None


def score_candidate(
    cand: Candidate,
    *,
    issue_number: Optional[str] = None,
    publisher_hint: Optional[str] = None,
    date_hint: Optional[date] = None,
) -> float:
    """Score a candidate against the hints. Higher is better. Pure function.

    Signals (CLAUDE.md Section 6): publisher match, store-date proximity, issue-number plausibility.
    """
    score = 0.0

    # Issue number must match the requested one to be in the running at all.
    want = _number_value(issue_number)
    have = _number_value(cand.number)
    if want is not None and have is not None:
        if want == have:
            score += 3.0
        else:
            score -= 5.0  # different issue number -> almost certainly the wrong record

    # Issue-number plausibility: a #9 cannot exist in a known 5-issue miniseries. This is the
    # TMNT: Shredder trap — the 2019 five-issue mini vs the 2025 ongoing.
    if want is not None and cand.issue_count is not None and want > cand.issue_count:
        score -= 6.0

    # Publisher match.
    if publisher_hint and cand.publisher:
        if publisher_hint.strip().lower() == cand.publisher.strip().lower():
            score += 2.0
        elif publisher_hint.strip().lower() in cand.publisher.strip().lower():
            score += 1.0

    # Store-date proximity: reward candidates shipping near when the title was reviewed.
    cand_date = _parse_date(cand.store_date)
    if date_hint and cand_date:
        days = abs((cand_date - date_hint).days)
        if days <= 14:
            score += 3.0
        elif days <= 60:
            score += 2.0
        elif days <= 365:
            score += 1.0
        else:
            score -= 1.0

    # Tie-breaker: prefer candidates already cross-referenced to ComicVine (pullable).
    if cand.cv_id:
        score += 0.5

    return score


# Minimum score and margin over the runner-up required to call a match "high" confidence.
_HIGH_SCORE = 4.0
_HIGH_MARGIN = 1.5


def disambiguate(
    candidates: list[Candidate],
    *,
    issue_number: Optional[str] = None,
    publisher_hint: Optional[str] = None,
    date_hint: Optional[date] = None,
) -> tuple[Optional[Candidate], Confidence]:
    """Pick the best candidate and assign a confidence. Never guesses wildly: an ambiguous or
    weak field returns (best, 'partial') or (None, 'unresolved')."""
    if not candidates:
        return None, "unresolved"

    scored = sorted(
        candidates,
        key=lambda c: score_candidate(
            c, issue_number=issue_number, publisher_hint=publisher_hint, date_hint=date_hint
        ),
        reverse=True,
    )
    best = scored[0]
    best_score = score_candidate(
        best, issue_number=issue_number, publisher_hint=publisher_hint, date_hint=date_hint
    )
    runner_score = (
        score_candidate(
            scored[1],
            issue_number=issue_number,
            publisher_hint=publisher_hint,
            date_hint=date_hint,
        )
        if len(scored) > 1
        else float("-inf")
    )

    if best_score < 0:
        return None, "unresolved"
    if best_score >= _HIGH_SCORE and (best_score - runner_score) >= _HIGH_MARGIN:
        return best, "high"
    return best, "partial"


# --- fuzzy path: the network-touching entry point -------------------------------------------


class IssueSearcher(Protocol):
    """Minimal interface the fuzzy resolver needs from a Metron client (keeps it testable)."""

    def search_issues(self, series_name: str, issue_number: Optional[str]) -> list[dict]: ...


def resolve_query(
    searcher: IssueSearcher,
    series_name: str,
    issue_number: Optional[str] = None,
    *,
    publisher_hint: Optional[str] = None,
    date_hint: Optional[date] = None,
) -> tuple[Optional[Entity], Confidence]:
    """Resolve a "Series #N" string from an editorial source to an Entity.

    Returns (entity, confidence). On 'unresolved' the entity is None and the caller falls back to an
    editorial link only (graceful degradation). Never raises on a bad match — it declines instead.
    """
    raw = searcher.search_issues(series_name, issue_number)
    candidates = [Candidate.from_metron(r) for r in raw]
    best, confidence = disambiguate(
        candidates,
        issue_number=issue_number,
        publisher_hint=publisher_hint,
        date_hint=date_hint,
    )
    if best is None:
        return None, "unresolved"

    # Reconstruct a minimal issue dict for the shared builder. (A real run would fetch full detail;
    # for confidence='partial' we keep what the search gave us.)
    issue_dict = {
        "id": best.issue_id,
        "number": best.number,
        "store_date": best.store_date,
        "publisher": best.publisher,
        "cv_id": best.cv_id,
        "isbn": None,
        "upc": None,
        "gcd_id": None,
        "series": {
            "id": best.series_id,
            "name": best.series_name,
            "year_began": best.volume_year,
        },
        "image": None,
    }
    entity = resolve_metron_issue(issue_dict, series_cv_id=best.series_cv_id)
    # The fuzzy path's confidence is bounded by the disambiguation result, not just cv_id presence.
    entity.resolution.confidence = confidence
    return entity, confidence


# --- series path: for awards and other series-level signals ---------------------------------
#
# Award winners ("Best Continuing Series") resolve to a *series*, not an issue. Metron's
# series_list returns lightweight BaseSeries (no cv_id); we disambiguate those, then fetch the
# series detail for the cv_id. Mirrors the issue path above.


def _normalize(text: Optional[str]) -> str:
    """Lowercase, drop a trailing '(YYYY)', strip punctuation, collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"\(\d{4}\)\s*$", "", text)  # Metron display names like "Saga (2012)"
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())


def _token_overlap(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class SeriesSearcher(Protocol):
    """What the series resolver needs from the Metron gateway."""

    def search_series(self, name: str) -> list[dict]: ...
    def series_detail(self, series_id: int) -> dict: ...


def score_series_candidate(
    cand: dict, *, title: str, year_hint: Optional[int] = None
) -> float:
    """Score a BaseSeries candidate against an award-winner title. Pure function."""
    score = 0.0
    nt = _normalize(title)
    cn = _normalize(cand.get("display_name"))

    if nt and nt == cn:
        score += 5.0
    elif nt and cn and (nt in cn or cn in nt):
        score += 2.5
    else:
        score += 3.0 * _token_overlap(nt, cn)  # partial-name credit

    year_began = cand.get("year_began")
    if year_hint and year_began:
        if year_began > year_hint + 1:
            score -= 6.0  # a series can't win an award before it began
        elif abs(year_began - year_hint) <= 20:
            score += 1.0  # plausible era

    return score


_SERIES_HIGH_SCORE = 4.5
_SERIES_HIGH_MARGIN = 1.5


def disambiguate_series(
    candidates: list[dict], *, title: str, year_hint: Optional[int] = None
) -> tuple[Optional[dict], Confidence]:
    if not candidates:
        return None, "unresolved"
    scored = sorted(
        candidates, key=lambda c: score_series_candidate(c, title=title, year_hint=year_hint),
        reverse=True,
    )
    best = scored[0]
    best_score = score_series_candidate(best, title=title, year_hint=year_hint)
    runner = (
        score_series_candidate(scored[1], title=title, year_hint=year_hint)
        if len(scored) > 1
        else float("-inf")
    )
    if best_score < 2.0:
        return None, "unresolved"  # too weak a name match to trust
    if best_score >= _SERIES_HIGH_SCORE and (best_score - runner) >= _SERIES_HIGH_MARGIN:
        return best, "high"
    return best, "partial"


def resolve_series(
    searcher: SeriesSearcher,
    title: str,
    *,
    publisher_hint: Optional[str] = None,
    year_hint: Optional[int] = None,
) -> tuple[Optional[Entity], Confidence]:
    """Resolve a series-level title (e.g. an award winner) to a series Entity.

    Returns (entity, confidence). 'unresolved' -> entity is None and the caller falls back to an
    editorial-only entry. Never emits a wrong match."""
    candidates = searcher.search_series(title)
    best, confidence = disambiguate_series(candidates, title=title, year_hint=year_hint)
    if best is None:
        return None, "unresolved"

    detail = searcher.series_detail(best["id"])
    cv_id = detail.get("cv_id")
    series_name = detail.get("name") or _normalize(best.get("display_name")) or title
    # Confidence is bounded by both the name match and whether we got a ComicVine id.
    final: Confidence = "high" if (confidence == "high" and cv_id) else "partial"

    ids = Ids(
        comicvine_volume=comicvine_volume_id(cv_id) if cv_id else None,
        metron_series=detail.get("id"),
        gcd_id=detail.get("gcd_id") or None,
        series_name=series_name,
        volume_year=detail.get("year_began"),
    )
    entity = Entity(
        kind="series",
        title=series_name,
        series_name=series_name,
        issue_number=None,
        publisher=detail.get("publisher") or publisher_hint,
        format=None,  # a series has no single physical format
        cover_url=None,
        release_date=None,
        ids=ids,
        resolution=Resolution(confidence=final, issue_pending=False),
    )
    return entity, final

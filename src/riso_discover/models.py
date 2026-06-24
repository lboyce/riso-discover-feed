"""Pydantic models for discover.json — the single source of truth for the schema.

These models ARE the contract between this service and RISO (CLAUDE.md Section 9). Constructing
them validates; ``DiscoverFeed.model_dump_json()`` writes the file. There is deliberately no
separate JSON-schema file: the models are both writer and validator.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# --- Enums (kept as Literal so they serialize as plain strings) -----------------------------

SectionType = Literal[
    "new_releases",
    "upcoming",
    "award_winners",
    "featured_picks",
    "trending",
    "story_arc",
    "event",
    "rss_department",
    "editorial_best_of",
    "featured_classic",
    "new_editions",
    "trades",
]
SourceTier = Literal["distribution", "personal"]
EntityKind = Literal["issue", "collection", "series"]
EntityFormat = Literal[
    "single_issue",
    "trade_paperback",
    "hardcover",
    "omnibus",
    "digital",
]
ReasonType = Literal[
    "new_release",
    "editorial",
    "review_signal",
    "award",
    "featured_pick",
    "trending",
    "classic",
    "reissue",
]
Confidence = Literal["high", "partial", "unresolved"]


class _Model(BaseModel):
    # Reject unknown keys so a drifting schema fails loudly in tests rather than silently.
    model_config = ConfigDict(extra="forbid")


class Ids(_Model):
    """The load-bearing ID block. comicvine_issue (or at least comicvine_volume) is the priority
    key for Komga matching; the metron_* / series_name / volume_year fields are fallbacks."""

    comicvine_issue: Optional[str] = None
    comicvine_volume: Optional[str] = None
    metron_issue: Optional[int] = None
    metron_series: Optional[int] = None
    isbn: Optional[str] = None
    upc: Optional[str] = None
    gcd_id: Optional[int] = None
    wikidata_id: Optional[str] = None  # e.g. "Q123456" — fallback key for editorial-only entities
    series_name: Optional[str] = None
    volume_year: Optional[int] = None


class Resolution(_Model):
    confidence: Confidence
    issue_pending: bool = False


class Entity(_Model):
    kind: EntityKind
    title: str
    series_name: Optional[str] = None
    issue_number: Optional[str] = None  # null for collections / series-level entities
    publisher: Optional[str] = None
    format: Optional[EntityFormat] = None  # null for series-level entities (no single format)
    cover_url: Optional[str] = None
    release_date: Optional[str] = None  # YYYY-MM-DD
    ids: Ids
    resolution: Resolution


class Reason(_Model):
    type: ReasonType
    source: str
    label: Optional[str] = None  # human label, e.g. "AIPT's Best of 2025"
    url: Optional[str] = None  # citation / link-back if editorial
    snippet: Optional[str] = None  # short excerpt if editorial, never full text


class Item(_Model):
    entity: str  # reference into the entities map
    reason: Reason


class Section(_Model):
    id: str
    type: SectionType
    title: str
    subtitle: Optional[str] = None
    source_tier: SourceTier
    source: str
    items: list[Item] = Field(default_factory=list)


class FeedWindow(_Model):
    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD


class DiscoverFeed(_Model):
    schema_version: str
    generated_at: str  # ISO-8601 UTC, stamped by the caller
    feed_window: FeedWindow
    sections: list[Section] = Field(default_factory=list)
    entities: dict[str, Entity] = Field(default_factory=dict)


# --- Entity-key helpers ---------------------------------------------------------------------
#
# Keep entity ids stable across weekly runs so RISO's persisted follow/pull/want state survives a
# refresh. Prefer the ComicVine id; fall back to the Metron id only when no cv_id is available.

CV_ISSUE_PREFIX = "4000"  # ComicVine issue resource prefix
CV_VOLUME_PREFIX = "4050"  # ComicVine volume resource prefix


def comicvine_issue_id(cv_id: int | str) -> str:
    """Format a Metron ``cv_id`` (bare ComicVine issue number) as a full ComicVine issue id."""
    return f"{CV_ISSUE_PREFIX}-{cv_id}"


def comicvine_volume_id(series_cv_id: int | str) -> str:
    """Format a series' ComicVine id as a full ComicVine volume id."""
    return f"{CV_VOLUME_PREFIX}-{series_cv_id}"


def entity_key(ids: Ids, *, kind: EntityKind = "issue") -> str:
    """Canonical, stable key for the entities map.

    Order of preference: ComicVine issue > ComicVine volume > Metron issue > Metron series.
    """
    if ids.comicvine_issue:
        return f"cv-issue-{ids.comicvine_issue}"
    if ids.comicvine_volume:
        return f"cv-volume-{ids.comicvine_volume}"
    if ids.metron_issue is not None:
        return f"metron-issue-{ids.metron_issue}"
    if ids.metron_series is not None:
        return f"metron-series-{ids.metron_series}"
    if ids.wikidata_id:
        return f"wd-{ids.wikidata_id}"  # editorial-only entity (e.g. unresolved award winner)
    raise ValueError("Cannot build an entity key: no usable id present in the Ids block.")

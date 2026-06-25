"""Orchestrator + schema writer + CLI.

Runs the enabled, tier-permitted sources, merges their entities (deduplicated by key), concatenates
their sections, stamps the feed window, and writes a single discover.json. A failing source is
logged and skipped — the run always emits a valid feed.

Usage:
    python -m riso_discover.build [--output discover.json] [--today 2026-06-23]
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .config import REPO_ROOT, Config, load_config, load_metron_credentials
from .metron_gateway import MetronGateway
from .models import DiscoverFeed, Entity, FeedWindow, Section
from .sources.base import BaseSource, SourceOutput
from .sources.cbr import CBRSource
from .sources.classics import ClassicsSource
from .sources.metron import MetronSource, week_window
from .sources.rss import RSSSource
from .sources.wikidata import WikidataSource

log = logging.getLogger(__name__)

DEFAULT_OUTPUT = REPO_ROOT / "discover.json"

# Curation leads the feed. Sections are ordered by this list (by id); anything unlisted sorts last.
SECTION_ORDER = [
    "riso-recommends",
    "critically-acclaimed",
    "popular",
    "featured-classic",
    "eisner-winners",
    "harvey-winners",
    "ringo-winners",
    "aipt-reviews",
    "new-this-week",
    "upcoming-releases",
]

#: Cap for the raw Metron new-release lists so curation dominates the feed.
METRON_MAX_PER_WINDOW = 24


def build_sources(config: Config, *, today: date, upcoming_weeks: int = 4) -> list[BaseSource]:
    """Instantiate the active sources named in config. Unknown/not-yet-built names are skipped.

    A single Metron gateway (client + cache + rate-limit retry) is built once and shared by every
    source that resolves through Metron. Per-source `options` from config.toml are threaded in."""
    gateway: MetronGateway | None = None

    def metron_gateway() -> MetronGateway:
        nonlocal gateway
        if gateway is None:
            gateway = MetronGateway(load_metron_credentials())
        return gateway

    sources: list[BaseSource] = []
    for spec in config.active_sources():
        opts = spec.options
        if spec.name == "metron":
            sources.append(
                MetronSource(
                    metron_gateway(), today=today, upcoming_weeks=upcoming_weeks,
                    max_per_window=opts.get("max_per_window", METRON_MAX_PER_WINDOW),
                )
            )
        elif spec.name == "wikidata":
            sources.append(WikidataSource(metron_gateway(), today=today))
        elif spec.name == "rss":
            sources.append(
                RSSSource(
                    metron_gateway(), today=today,
                    include_verdict=bool(opts.get("include_verdict", False)),
                )
            )
        elif spec.name == "cbr":
            sources.append(
                CBRSource(
                    metron_gateway(), today=today,
                    show_rating=bool(opts.get("show_rating", True)),
                )
            )
        elif spec.name == "classics":
            sources.append(ClassicsSource(metron_gateway(), today=today))
        else:
            log.info("Source '%s' is enabled but not yet implemented; skipping.", spec.name)
    return sources


def order_sections(sections: list[Section]) -> list[Section]:
    """Sort sections curation-first per SECTION_ORDER; unlisted sections keep their order, last."""
    rank = {sid: i for i, sid in enumerate(SECTION_ORDER)}
    return sorted(sections, key=lambda s: rank.get(s.id, len(SECTION_ORDER)))


def assemble(
    outputs: list[SourceOutput],
    *,
    generated_at: str,
    window: FeedWindow,
) -> DiscoverFeed:
    """Merge source outputs into one feed. Entities are deduplicated by key across sources; a book
    that appears in several sections exists once in `entities` and is referenced by key."""
    entities: dict[str, Entity] = {}
    sections: list[Section] = []
    for out in outputs:
        for key, entity in out.entities.items():
            entities.setdefault(key, entity)
        sections.extend(out.sections)
    return DiscoverFeed(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        feed_window=window,
        sections=order_sections(sections),  # curation leads
        entities=entities,
    )


def write_feed(feed: DiscoverFeed, path: Path) -> None:
    """Serialize the feed. Constructing/validating happened at model creation; this just writes."""
    path.write_text(feed.model_dump_json(indent=2) + "\n", "utf-8")


def run_build(
    *,
    today: date,
    output: Path = DEFAULT_OUTPUT,
    upcoming_weeks: int = 4,
    build_tier: str | None = None,
) -> DiscoverFeed:
    config = load_config(build_tier_override=build_tier)
    log.info("Build tier: %s", config.build_tier)
    sources = build_sources(config, today=today, upcoming_weeks=upcoming_weeks)

    outputs: list[SourceOutput] = []
    for source in sources:
        try:
            outputs.append(source.run())
        except Exception as exc:  # a single failing source must never crash the run
            log.warning("Source '%s' failed and was skipped: %s", source.name, exc)

    start, end = week_window(today)
    feed = assemble(
        outputs,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        window=FeedWindow(start=start.isoformat(), end=end.isoformat()),
    )
    write_feed(feed, output)
    log.info(
        "Wrote %s: %d sections, %d entities", output, len(feed.sections), len(feed.entities)
    )
    return feed


def main() -> None:
    parser = argparse.ArgumentParser(description="Build discover.json for RISO.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output path.")
    parser.add_argument(
        "--today",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Override today's date (YYYY-MM-DD), for the feed window. Defaults to the system date.",
    )
    parser.add_argument(
        "--upcoming-weeks",
        type=int,
        default=4,
        help="How many weeks of Upcoming Releases to include after this week (0 to skip).",
    )
    parser.add_argument(
        "--build-tier",
        choices=["distribution", "personal"],
        default=None,
        help="Override the config build tier. 'personal' includes personal-only sources (e.g. CBR).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    today = args.today or datetime.now(timezone.utc).date()
    run_build(
        today=today,
        output=args.output,
        upcoming_weeks=args.upcoming_weeks,
        build_tier=args.build_tier,
    )


if __name__ == "__main__":
    main()

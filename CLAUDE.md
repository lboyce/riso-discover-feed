# riso-discover-feed — CLAUDE.md

This file is the authoritative brief for the project. Read it fully before writing any code.
You (Claude Code) have no memory of the planning conversations that produced it, so treat this
document as ground truth for the architecture, the data model, the source rules, and the guardrails.

---

## 1. What this project is

`riso-discover-feed` is a small server-side ingestion service that produces the content for the
**Discover** page of RISO, a native SwiftUI iPad comic app. It runs on a schedule (weekly), pulls
from a set of comic-data sources, resolves everything to canonical book entities with stable IDs,
and writes a single `discover.json` file. RISO fetches that file and renders it.

This project does NOT contain any app code, any Swift, and any user-facing UI. It is a data pipeline.
Its only output is `discover.json`. The JSON schema (Section 9) is the entire contract between this
project and RISO.

## 2. The product idea behind it (so your choices serve it)

RISO's Reading page is the user's own library, served beautifully. The **Discover page is the comic
shop**: a place to browse, pull, and (eventually) buy. The model the app uses is Browse, Pull, Buy,
minus the Buy. So the single most important property of everything this service emits is that every
book it surfaces must be a real, pullable entity, not just a headline.

The north star for curation is: **RISO is an independent editor that reads many signals and makes its
own picks.** It is not a mirror of any other site's lists. It reads new-release data, critical
signals, and editorial buzz, then builds its own sections. This matters for both originality and for
staying clean with the sources (see Section 8).

## 3. Relationship to RISO (the division of labor)

This service does ALL of the heavy lifting:
- fetching and parsing pages and feeds
- resolving titles to canonical IDs
- disambiguation and confidence scoring
- assembling the final, deduplicated entity set and the section views

RISO does NONE of that. RISO only:
- fetches `discover.json`
- matches entity IDs against the user's Komga library to compute owned vs missing
- overlays the user's local pull / follow / want state
- renders

Never assume RISO will "figure out" a title. If an entity is not resolved to IDs here, RISO cannot
make it pullable. Resolution is this project's job.

## 4. Architecture

- Language: **Python**. (Best ecosystem for RSS, HTTP, and JSON. You may choose specific libraries,
  see Section 10.)
- Runtime: a scheduled **GitHub Action** (cron, weekly). No server to rent.
- Output: one `discover.json`, committed to the repo (or published to a release / Pages path). RISO
  fetches its raw URL.
- Secrets (API keys, Metron credentials) live in **GitHub Secrets**, never in the repo.
- The whole run is idempotent: each weekly run regenerates the full `discover.json` from scratch.

## 5. The core data model: book-as-entity, reasons-as-context

The atomic unit is a **book entity**, deduplicated by canonical ID. A single book can appear in many
sections (for example "new this week" and "an outlet's pick" and "critically acclaimed"). It is ONE
entity that carries one or more **reasons**, and sections are just views that reference entities.

The load-bearing part of every entity is its `ids` block. Those IDs are what let RISO match against
the Komga library (owned vs missing) and persist the user's pull / follow / want state across weekly
refreshes. Without stable IDs, none of the app's collection features work. Treat ID resolution as the
core deliverable, not a nice-to-have.

Note: pull, follow, want, and owned/missing are all PER-USER state computed client-side in RISO. They
are never in `discover.json`. This service supplies the resolvable catalog and the editorial reasons.
The personal reasons (you own this, this is a gap in your run) are computed in RISO.

## 6. The resolution pipeline (the hardest and most important component)

For each candidate book (from any source), resolve it to canonical IDs:

1. **Extract** series name + issue number + type from structured signals. For editorial pages, use
   the page's metadata (title pattern, URL slug, OG tags, WordPress tags, category), not prose.
   Single-book review pages follow a rigid "Series #N review" title pattern and resolve cleanly.
2. **Resolve through Metron** (metron.cloud). Metron has a clean, commercial-friendly API and, crucially,
   cross-references ComicVine IDs via a `cv_id` field. Query Metron for the series and issue, and
   **disambiguate** using publisher, store date proximity, and issue-number plausibility. (Example
   trap: "TMNT: Shredder" can match a 2019 miniseries and a 2025 ongoing. A #9 reviewed in 2026 is the
   ongoing, not the five-issue mini. Use date and number to choose.)
3. **Lift the ComicVine ID** from Metron's record (`cv_id`). ComicVine is the ID Komga stores in
   ComicInfo.xml, so it is the primary key for library matching. Getting it via Metron avoids hitting
   ComicVine's API directly, which matters because ComicVine is non-commercial-only (Section 8).
4. **Confidence gate.** High-confidence match: emit the entity with full IDs, pullable. Low confidence
   or unresolved: degrade gracefully (see below). Never emit a wrong match. Never fail loudly.
5. **Freshness lag handling.** A book reviewed the day it ships may not be indexed yet. If you find the
   volume but not the issue, emit the series-level IDs and mark the issue pending, so Follow still works.

**Graceful degradation rule:** if a book cannot be resolved to IDs, it may still appear as an editorial
link (the news-reader fallback), but it is not pullable. Best case the user gets the comic shop; worst
case they get a link. It never breaks.

**Reliability ladder (build accordingly):**
- Single-book reviews: high yield. Backbone-quality.
- Ranked lists of discrete titles (critical lists, awards): highest yield. Backbone-quality.
- Structured columns (for example weekly trade columns): medium yield, especially if they link to
  purchase pages carrying ISBNs or ASINs. Best-effort, test before promoting to a section.
- Free-prose essays: low yield. Do not rely on these.

When in doubt, prefer sources where each item is already a discrete, resolvable title.

## 7. Sources (each is a swappable, flag-gated module)

Build every source as an independent module behind a config flag, so any one can be turned off without
breaking the rest. Each module fetches, normalizes, runs candidates through the resolution pipeline,
and contributes entities + reasons.

- **Metron** (metron.cloud): the backbone. Powers New This Week and Upcoming Releases (via store_date),
  and is the resolution + cv_id bridge for every other source. Clean, commercial-friendly API. Requires
  credentials (GitHub Secrets). Rate-limited, so cache and be polite.
- **Wikidata / Wikipedia**: Award Winners (Eisner, Harvey, Ringo, Hugo Best Graphic Story). Clean,
  openly licensed, queryable via SPARQL. Evergreen with an annual refresh.
- **RSS feeds** (editorial): per-outlet "departments" and editorial best-of. Pull headline, the
  feed-provided summary or excerpt, the feed image, and a link back. NEVER reproduce full article text.
  Bias toward reviews and recommendations over general news. Use category-specific feed URLs where
  available (for example an outlet's reviews feed) so each department self-curates. Confirmed clean feeds
  include AIPT (`aiptcomics.com/category/comic-books/feed/`), Multiversity (`multiversitycomics.com/feed`),
  The Comics Beat (`comicsbeat.com/feed`), and The Comics Journal (`tcj.com/feed`).
- **Comic Book Roundup** (the automated curator): see Section 8. Personal-tier, swappable. Used as a
  SIGNAL to make RISO's own picks, not reproduced.
- **ComicVine**: deepens classic / back-catalog coverage. Non-commercial-only, so personal-tier and
  swappable. Prefer resolving through Metron's cv_id rather than hitting ComicVine directly.
- **GCD (comics.org)**: cleaner-licensed classic and bibliographic data (API plus downloadable data
  dumps). A distribution-safe fallback for classic data.

## 8. Source cleanliness and the distribution rule (non-negotiable)

Every source is tagged as one of two tiers, and the build target controls which run:

- **distribution-clean**: Metron, Wikidata, RSS (headline + excerpt + link only), GCD. These may run in
  any build, including a distributed App Store build.
- **personal-only**: Comic Book Roundup, ComicVine. These run only in personal / testing builds and are
  excluded from any distributed build, pending permission from those sources.

The `source_tier` flag on each section (Section 9) drives this. A distributed build simply omits
personal-only sections. Build this in from day one. Do not make a personal-only source load-bearing for
a section that needs to exist in the distributed build.

**The "automated curator" rules for Comic Book Roundup (read carefully):**
- The service may READ CBR's public ranked lists (Highest Rated Current Issues, Most Pulled Current
  Issues) as a SIGNAL.
- It then makes its OWN independent selection: take the top candidates, apply RISO's own selection logic
  (for example one per major publisher, rotated, excluding recently featured), resolve via Metron, and
  present as RISO's own picks (for example "Featured Books of the Week").
- It must NOT reproduce CBR's ranked list in their order, must NOT reproduce their scores as a dataset,
  and must NOT present itself as showing CBR's lists. It uses an objective fact (which titles are highly
  rated this week) to build its own curation.
- This is the line: fact-as-signal is fine, compilation-as-copy is not. Stay on the "select a few,
  reframe independently" side.
- Optional and friendly: attribute with a link back to CBR. Not required, but generous.

**Hard exclusions:** League of Comic Geeks is OFF LIMITS for scraping. Their Terms of Use explicitly
prohibit it. Do not build any LoCG ingestion. A partnership may be pursued separately by the owner later;
until then, no LoCG.

**RSS copyright rule:** headline + feed-provided excerpt + image + link back. Never the full article body.

## 9. The discover.json schema (the contract)

Emit one object with this shape. Sections reference entities by id; entities are deduplicated.

```jsonc
{
  "schema_version": "1.0",
  "generated_at": "2026-06-23T12:00:00Z",
  "feed_window": { "start": "2026-06-23", "end": "2026-06-29" },

  "sections": [
    {
      "id": "new-this-week",
      "type": "new_releases",            // new_releases | upcoming | award_winners | featured_picks |
                                         // trending | story_arc | event | rss_department |
                                         // editorial_best_of | featured_classic | new_editions | trades
      "title": "New This Week",
      "subtitle": null,                  // optional
      "source_tier": "distribution",     // distribution | personal
      "source": "Metron",
      "items": [
        {
          "entity": "cv-issue-4000-987654",   // reference into entities map
          "reason": {
            "type": "new_release",            // new_release | editorial | review_signal | award |
                                              // featured_pick | trending | classic | reissue
            "source": "Metron",
            "label": null,                    // human label, e.g. "AIPT's Best of 2025"
            "url": null,                      // citation / link-back if editorial
            "snippet": null                   // short excerpt if editorial, never full text
          }
        }
      ]
    }
  ],

  "entities": {
    "cv-issue-4000-987654": {
      "kind": "issue",                   // issue | collection | series
      "title": "Teenage Mutant Ninja Turtles: Shredder #9",
      "series_name": "Teenage Mutant Ninja Turtles: Shredder",
      "issue_number": "9",              // null for collections / series-level entities
      "publisher": "IDW Publishing",
      "format": "single_issue",          // single_issue | trade_paperback | hardcover | omnibus | digital
      "cover_url": "https://...",
      "release_date": "2026-06-24",
      "ids": {
        "comicvine_issue": "4000-987654",
        "comicvine_volume": "4050-XXXXXX",
        "metron_issue": 56789,
        "metron_series": 1234,
        "isbn": null,
        "upc": null,
        "gcd_id": null,
        "series_name": "Teenage Mutant Ninja Turtles: Shredder",
        "volume_year": 2025
      },
      "resolution": {
        "confidence": "high",            // high | partial | unresolved
        "issue_pending": false           // true if series resolved but issue not yet indexed
      }
    }
  }
}
```

Rules for the schema:
- Every entity must carry the `ids` block. `comicvine_issue` (or at least `comicvine_volume`) is the
  priority key for Komga matching. Include `metron_*`, `series_name`, and `volume_year` as fallbacks.
- A book that belongs in multiple sections appears once in `entities` and is referenced by multiple
  section items, each with its own `reason`.
- `source_tier` on each section is what a distributed build uses to drop personal-only sections.
- Keep entity ids stable across weekly runs (prefer the ComicVine id as the entity key) so RISO's
  persisted follow / pull / want state survives refreshes.

## 10. v1 sections (build these)

New-book spine: **New This Week**, **Upcoming Releases** (Metron).
Quality signal: **Award Winners** (Wikidata), **Featured Picks / Trending** (CBR automated curator,
personal-tier).
Editorial: per-outlet **RSS departments** and **Editorial Best-Of** (RSS).
Classics: **Featured Classic** (a one-time evergreen canon seed that rotates weekly; this is a seed,
not ongoing curation), **New Editions / Reissues** (rides the new-release pipeline), **Trades /
Collected Editions**.
Spotlights: **Story Arc** (single-series arc) and **Event** (line-wide crossover, distinct from a
story arc), from Metron / ComicVine story-arc data.

Deferred / not in v1: Creator Spotlight (the feed is book-focused, not creator-focused), Publisher /
Imprint Spotlight (maybe later), This Week in Comics History (replaced by Milestone Anniversaries only,
if at all).

## 11. Engineering conventions

- Python 3. Suggested libraries (you may choose alternatives): `feedparser` for RSS, `httpx` or
  `requests` for HTTP, `mokkari` for the Metron API, `simyan` for ComicVine if needed, a SPARQL client
  or plain HTTP for Wikidata. Resolve dependencies with a `requirements.txt` or `pyproject.toml`.
- Cache aggressively. Resolution lookups are the expensive part. Cache Metron / ComicVine responses so
  re-runs and retries do not re-hit the APIs unnecessarily, and respect rate limits.
- Make the pipeline resilient: a single failing source or a single unresolved book must never crash the
  run. Log and continue. Always emit a valid `discover.json`.
- Keep modules isolated: one module per source, a shared resolver, a shared schema writer.
- Write tests for the resolver (the disambiguation logic especially) and for schema validity.

## 12. Secrets and config

- API keys and Metron credentials: GitHub Secrets, injected as environment variables in the Action.
  Never commit them. Never print them.
- A config file controls which sources run and the build tier (distribution vs personal). The default
  committed config should be distribution-clean. Personal-tier sources are enabled only locally / in a
  personal build.

## 13. How to work with the owner

- The owner (Luke) is the product lead and is comfortable running Claude Code, but is not a programmer.
  Explain Terminal steps explicitly when needed and never assume command-line familiarity.
- Work in plan mode: show your plan and pause before executing.
- Build in small, committed steps. Suggested sequencing:
  1. Repo skeleton + the schema writer + a hand-written sample `discover.json` (so RISO has something to
     build against immediately).
  2. The Metron module + the resolver (New This Week). This proves the core pipeline end to end.
  3. Awards (Wikidata). Clean and quick.
  4. RSS departments + the resolver's editorial path.
  5. The CBR automated curator (personal-tier).
  6. Classic sections.
  7. The GitHub Action (weekly schedule) and publishing the file.
- After each working step, commit, and confirm the emitted JSON validates against the schema.

## 14. Session log / gotchas

(Append platform-specific mistakes and their fixes here the moment they happen, so they do not recur.)

- **Metron rate limit (20 req/min on this account) and `mokkari` behavior.** The live account is
  capped at 20 requests/minute (lower than the 30 in mokkari's own docstrings). `mokkari` enforces
  the limit *locally* and **raises `mokkari.exceptions.RateLimitError` instead of sleeping** when the
  window fills; the exception carries a `retry_after` (seconds). The New This Week pipeline makes
  ~2 calls per issue (issue detail + series lookup), so a full week (80+ issues) blows past 20/min.
  Fix in place: `MetronSource._with_retry` catches `RateLimitError`, sleeps `retry_after` (+1s
  buffer), and retries up to 8 times; on-disk caching (`.cache/`) means successful calls are never
  re-fetched, so retries and re-runs resume where they left off. Net effect: the weekly batch just
  waits the limit out. If a run feels slow, that's expected — it's sleeping, not stuck.
- **Freshness lag is normal for the current week.** A `discover.json` built for the current week is
  expected to contain mostly `partial` / `issue_pending` entities with only `comicvine_volume` set,
  because ComicVine hasn't cross-referenced the brand-new *issues* yet. Older weeks resolve to full
  `comicvine_issue` IDs at `high` confidence. Don't mistake an all-`partial` current week for a bug.
- **Wikidata Query Service (WQS) is finicky.** It requires a **descriptive `User-Agent`** (project +
  contact URL) or it blocks you. It also **rate-limits and has periodic outages** — during an
  incident it returns `429 ... 1 req/min` or `502`. Keep SPARQL **lean** (use `STRSTARTS` not
  `CONTAINS`, filter works-only with `?type != wd:Q5`, minimize `OPTIONAL`/`SERVICE` joins) so
  queries finish inside the 60s server limit. The Wikidata source has bounded retry/backoff and the
  per-source try/except degrades to empty award sections, so an outage never breaks the run — awards
  simply fill in on the next successful weekly run (Metron calls come from `.cache`, so it's cheap to
  rerun). Seen 2026-06-23: a multi-hour WQS outage blocked live award verification entirely.
- **RSS feeds: several traps (all seen live 2026-06).**
  - **CDN User-Agent blocking.** AIPT/TCJ return 403 (and tools like WebFetch get blocked) without a
    real `User-Agent`. `feedparser` fetched with a descriptive UA succeeds. Always set the UA.
  - **Review titles often lack the word "review".** AIPT titles read like
    "'In Your Skin' #3 blurs the line..." and are only tagged with a "Reviews" *category*. So the
    reviews filter must check category tags, not just the title, and `parse_review_title` must handle
    quoted "'Series' #N ..." headlines (smart quotes included).
  - **"Previews" vs "Reviews" regex.** `"review" in "previews"` is True (substring) and `\breview\b`
    fails to match the plural "Reviews". Use `\breviews?\b` — matches review/reviews, not preview(s).
  - **Use reviews-category feeds, not the main feed.** Main outlet feeds are news-heavy. AIPT's
    `…/comic-book-reviews/feed/` yields clean single-book reviews; the general `…/comic-books/feed/`
    does not. Per outlet, find the reviews-category URL before shipping it as a review department.
  - **Not every outlet fits single-book review departments.** The Comics Beat "reviews" are multi-book
    Rundown/Round-Up columns (→ Editorial Best-Of, not single-book); TCJ is long-form graphic-novel
    essays with no `#N`; Multiversity's documented feed URL 404s. Only AIPT shipped in v1.

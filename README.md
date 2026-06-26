# riso-discover-feed

Server-side ingestion service that builds **`discover.json`** — the content for the Discover page of
**RISO**, a native SwiftUI iPad comic app. It pulls from comic-data sources, resolves every title to
canonical IDs (the key to RISO's owned/missing matching), and emits one JSON file. RISO fetches that
file and renders it.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture, data model, source rules, and guardrails —
that document is the authoritative brief.

## How it works

- **Schema models** (`src/riso_discover/models.py`) — pydantic models that *are* the `discover.json`
  contract (Section 9). Constructing them validates; there is no separate JSON-schema file.
- **Resolver** (`src/riso_discover/resolver.py`) + **Metron gateway** (`metron_gateway.py`) — resolves
  titles to ComicVine IDs via Metron (issue and series paths), with disambiguation, a confidence gate,
  and freshness-lag handling. Every source resolves through this shared, cached, rate-limit-aware layer.
- **Build orchestrator** (`src/riso_discover/build.py`) — runs the enabled, tier-permitted sources,
  deduplicates entities, and writes `discover.json`. A failing source is logged and skipped; the run
  always emits a valid feed.
- **Sample feed** (`samples/discover.sample.json`) — a hand-written, schema-valid example RISO can
  build against.

### Sources & sections

Curation leads the feed (`build.py` `SECTION_ORDER`):

| Source | Tier | Sections |
| --- | --- | --- |
| Comic Book Roundup | **personal** | **RISO Recommends**, **Critically Acclaimed** (with scores), **Popular This Week** |
| Classics | distribution | Featured Classic (evergreen seed) |
| Wikidata | distribution | Eisner / Harvey / Ringo Award Winners |
| RSS (AIPT) | distribution | AIPT Reviews (score + verdict + likes/dislikes + Read-more link) |
| Metron | distribution | New This Week, Upcoming Releases (capped to 24 each) |

Each source is a flag-gated module (`config.toml`). Personal-tier sources run only in a personal
build and are excluded from the distributed feed.

**CBR — permission granted.** Comic Book Roundup has confirmed scraping + reproducing their content is
fine (the link back to CBR is the agreed credit). So CBR shelves show the aggregate score and, in the
card's empty space, the **two highest-scored critic-review excerpts** (`reason.quotes`: outlet,
reviewer, full excerpt, score, link to the full review). `[sources.cbr] show_rating` still toggles the
aggregate score; CBR stays personal-tier, so `--build-tier distribution` drops it entirely.

**AIPT — owner-approved, written permission pending.** The feed shows AIPT's score + verdict +
likes/dislikes with a prominent "Read more at AIPT" link, behind `[sources.rss] include_verdict`
(off → feed excerpt + link only) for a one-line clean ship.

Trades / Collected Editions and New Editions / Reissues remain deferred (see `CLAUDE.md` §14).

## Setup

You need **Python 3.10 or newer** and a free account at [metron.cloud](https://metron.cloud).

Open Terminal, then run these commands one at a time from inside this folder:

```sh
# 1. Create an isolated Python environment (one time)
python3 -m venv .venv

# 2. Turn it on (do this every new Terminal session)
source .venv/bin/activate

# 3. Install the project and its tools (one time)
pip install -e ".[dev]"
```

> If `python3` is older than 3.10, use a newer one explicitly, e.g. `python3.13 -m venv .venv`.

### Add your Metron credentials

Copy the example file and paste your real metron.cloud username and password into it:

```sh
cp .env.example .env
```

Then open `.env` in any text editor and fill in:

```
METRON_USERNAME=your_username
METRON_PASSWORD=your_password
```

`.env` is gitignored and must never be committed. The credentials are read only at runtime.

## Run

```sh
# Build discover.json for the current week
python -m riso_discover.build

# Or pin the week (useful for testing a specific window)
python -m riso_discover.build --today 2026-06-24 -v
```

This writes `discover.json` in the repo root. Responses from Metron are cached under `.cache/`
(gitignored) so repeat runs are fast and stay within the API rate limit (~20 requests/minute; the
pipeline waits out rate limits automatically, so a slow run is sleeping, not stuck).

To run a **personal** build (adds the Comic Book Roundup picks, which are excluded from the
distributed feed):

```sh
python -m riso_discover.build --build-tier personal
```

## Automated weekly build (GitHub Action)

`.github/workflows/build-discover.yml` regenerates and commits `discover.json` every **Wednesday**
(and on demand from the Actions tab). During the testing phase it builds the **personal** tier so the
curation shelves (CBR) appear; switch the workflow's build step to `--build-tier distribution` for a
clean public ship once permissions are secured.

**One-time setup:** in the GitHub repo, go to **Settings → Secrets and variables → Actions → New
repository secret** and add two secrets:

- `METRON_USERNAME`
- `METRON_PASSWORD`

That's it — the workflow injects them as environment variables at run time (never committed). RISO
fetches the raw URL of the committed `discover.json`.

## Test

```sh
pytest
```

The test suite runs fully offline (no network, no credentials) against fixtures — it covers schema
validity, the resolver's disambiguation logic, each source's assembly, and the build-tier exclusion.

## Configuration

`config.toml` controls which sources run and the build tier:

- `build_tier = "distribution"` — only distribution-clean sources (safe for an App Store build).
- `build_tier = "personal"` — also runs personal-only sources (Comic Book Roundup) for local/testing.

A personal-only source is skipped automatically in a distribution build. The committed default is
distribution-clean. Override per run with `--build-tier personal`. See `CLAUDE.md` Section 8.

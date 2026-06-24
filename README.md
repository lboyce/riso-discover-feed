# riso-discover-feed

Server-side ingestion service that builds **`discover.json`** — the content for the Discover page of
**RISO**, a native SwiftUI iPad comic app. It pulls from comic-data sources, resolves every title to
canonical IDs (the key to RISO's owned/missing matching), and emits one JSON file. RISO fetches that
file and renders it.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture, data model, source rules, and guardrails —
that document is the authoritative brief.

## What's built so far

- **Schema models** (`src/riso_discover/models.py`) — pydantic models that *are* the `discover.json`
  contract (Section 9). Constructing them validates; there is no separate JSON-schema file.
- **Sample feed** (`samples/discover.sample.json`) — a hand-written, schema-valid example so RISO can
  build against the contract today.
- **Resolver** (`src/riso_discover/resolver.py`) — resolves titles to ComicVine IDs via Metron, with
  the disambiguation + confidence logic (the TMNT: Shredder trap, etc.).
- **Metron source** (`src/riso_discover/sources/metron.py`) — **New This Week** and **Upcoming
  Releases** via `store_date`.
- **Build orchestrator** (`src/riso_discover/build.py`) — runs enabled sources, deduplicates entities,
  and writes `discover.json`.

Other sources (Wikidata awards, RSS, CBR, ComicVine, GCD) come later in the build sequence.

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
(gitignored) so repeat runs are fast and stay within the API rate limit (20 requests/minute).

## Test

```sh
pytest
```

The test suite runs fully offline (no network, no credentials) against fixtures — it covers schema
validity, the resolver's disambiguation logic, and New This Week assembly.

## Configuration

`config.toml` controls which sources run and the build tier:

- `build_tier = "distribution"` — only distribution-clean sources (safe for an App Store build).
- `build_tier = "personal"` — also runs personal-only sources (ComicVine, Comic Book Roundup) for
  local/testing builds.

A personal-only source is skipped automatically in a distribution build. See `CLAUDE.md` Section 8.

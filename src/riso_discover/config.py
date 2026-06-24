"""Configuration loading: config.toml (which sources run, build tier) + .env (secrets)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Repo root = two levels up from this file (src/riso_discover/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"


@dataclass(frozen=True)
class SourceConfig:
    name: str
    enabled: bool
    tier: str  # "distribution" | "personal"


@dataclass(frozen=True)
class Config:
    build_tier: str  # "distribution" | "personal"
    sources: dict[str, SourceConfig]

    def active_sources(self) -> list[SourceConfig]:
        """Sources that should run: enabled, and tier-permitted by the current build_tier.

        A personal-only source is dropped from a distribution build (CLAUDE.md Section 8)."""
        out = []
        for src in self.sources.values():
            if not src.enabled:
                continue
            if self.build_tier == "distribution" and src.tier == "personal":
                continue
            out.append(src)
        return out


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    build_tier = raw.get("build_tier", "distribution")
    sources = {
        name: SourceConfig(
            name=name,
            enabled=bool(spec.get("enabled", False)),
            tier=spec.get("tier", "distribution"),
        )
        for name, spec in raw.get("sources", {}).items()
    }
    return Config(build_tier=build_tier, sources=sources)


@dataclass(frozen=True)
class MetronCredentials:
    username: str
    password: str


def load_metron_credentials() -> MetronCredentials:
    """Load Metron credentials from the environment (populated from a gitignored .env).

    Raises a clear error if they are missing rather than letting mokkari fail obscurely."""
    load_dotenv(REPO_ROOT / ".env")
    username = os.environ.get("METRON_USERNAME", "").strip()
    password = os.environ.get("METRON_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError(
            "Metron credentials missing. Copy .env.example to .env and set "
            "METRON_USERNAME and METRON_PASSWORD."
        )
    return MetronCredentials(username=username, password=password)

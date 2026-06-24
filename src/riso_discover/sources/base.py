"""Shared source contract.

A source fetches, normalizes, runs candidates through the resolver, and contributes Sections (with
items referencing entity keys) plus the Entity objects those keys point at. The orchestrator merges
entities across sources (dedupe) and concatenates sections.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import Entity, Section


@dataclass
class SourceOutput:
    sections: list[Section] = field(default_factory=list)
    entities: dict[str, Entity] = field(default_factory=dict)


class BaseSource(ABC):
    #: Stable identifier, matches the key in config.toml [sources.<name>].
    name: str = ""
    #: "distribution" | "personal" — must agree with config; used to set Section.source_tier.
    tier: str = "distribution"

    @abstractmethod
    def run(self) -> SourceOutput:
        """Produce this source's sections and entities. Must not raise for routine data problems;
        log and skip individual items instead so one bad book never crashes the run."""
        raise NotImplementedError

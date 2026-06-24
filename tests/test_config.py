"""Build-tier exclusion (CLAUDE.md §8): personal-only sources are dropped from a distribution build."""

from riso_discover.config import Config, SourceConfig


def _config(build_tier):
    return Config(
        build_tier=build_tier,
        sources={
            "metron": SourceConfig("metron", enabled=True, tier="distribution"),
            "cbr": SourceConfig("cbr", enabled=True, tier="personal"),
        },
    )


def test_personal_source_excluded_in_distribution_build():
    names = {s.name for s in _config("distribution").active_sources()}
    assert "metron" in names
    assert "cbr" not in names  # personal-only -> dropped from a distributed build


def test_personal_source_included_in_personal_build():
    names = {s.name for s in _config("personal").active_sources()}
    assert {"metron", "cbr"} <= names


def test_disabled_source_never_runs():
    cfg = Config(
        build_tier="personal",
        sources={"cbr": SourceConfig("cbr", enabled=False, tier="personal")},
    )
    assert cfg.active_sources() == []

"""Schema validity: the hand-written sample validates, round-trips, and the key helper behaves."""

import json
from pathlib import Path

import pytest

from riso_discover.models import (
    DiscoverFeed,
    Ids,
    Reason,
    comicvine_issue_id,
    comicvine_volume_id,
    entity_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = REPO_ROOT / "samples" / "discover.sample.json"


def test_sample_validates_against_models():
    data = json.loads(SAMPLE.read_text("utf-8"))
    feed = DiscoverFeed.model_validate(data)
    # Every section item must reference an entity that exists in the entities map.
    for section in feed.sections:
        for item in section.items:
            assert item.entity in feed.entities


def test_sample_round_trips():
    data = json.loads(SAMPLE.read_text("utf-8"))
    feed = DiscoverFeed.model_validate(data)
    reparsed = DiscoverFeed.model_validate(json.loads(feed.model_dump_json()))
    assert reparsed == feed


def test_unknown_field_is_rejected():
    data = json.loads(SAMPLE.read_text("utf-8"))
    data["surprise"] = "nope"
    with pytest.raises(Exception):
        DiscoverFeed.model_validate(data)


def test_cv_id_formatting():
    assert comicvine_issue_id(987654) == "4000-987654"
    assert comicvine_volume_id(145678) == "4050-145678"


def test_entity_key_prefers_comicvine_issue():
    ids = Ids(comicvine_issue="4000-987654", metron_issue=56789)
    assert entity_key(ids) == "cv-issue-4000-987654"


def test_entity_key_falls_back_to_metron():
    ids = Ids(metron_issue=56999, metron_series=9876)
    assert entity_key(ids) == "metron-issue-56999"


def test_entity_key_falls_back_to_wikidata():
    ids = Ids(wikidata_id="Q110881556", series_name="Lore Olympus")
    assert entity_key(ids) == "wd-Q110881556"


def test_entity_key_falls_back_to_source_url():
    ids = Ids(source_url="https://aiptcomics.com/x/saga-66-review/")
    key = entity_key(ids)
    assert key.startswith("rss-")
    # Stable for the same URL, distinct for a different one.
    assert key == entity_key(Ids(source_url="https://aiptcomics.com/x/saga-66-review/"))
    assert key != entity_key(Ids(source_url="https://aiptcomics.com/x/other-review/"))


def test_reason_rating_fields_round_trip():
    r = Reason(
        type="review_signal", source="AIPT", score=9.0, score_max=10.0, review_count=8,
        pros=["Great art"], cons=["Too short"],
    )
    reparsed = Reason.model_validate(json.loads(r.model_dump_json()))
    assert reparsed == r
    assert reparsed.score == 9.0 and reparsed.pros == ["Great art"]


def test_reason_quotes_round_trip():
    r = Reason(
        type="featured_pick", source="Comic Book Roundup",
        quotes=[
            {"outlet": "Geek Dad", "reviewer": "Ray Goldfield", "excerpt": "Stunning.",
             "score": 10.0, "url": "https://geekdad.example/r"},
            {"outlet": "Comic Watch", "reviewer": "C. Laspada", "excerpt": "Can't miss.",
             "score": 9.5, "url": "https://comic-watch.example/r"},
        ],
    )
    reparsed = Reason.model_validate(json.loads(r.model_dump_json()))
    assert reparsed == r
    assert reparsed.quotes[0].outlet == "Geek Dad" and reparsed.quotes[0].score == 10.0


def test_entity_key_requires_some_id():
    with pytest.raises(ValueError):
        entity_key(Ids())

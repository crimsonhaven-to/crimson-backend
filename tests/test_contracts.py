"""The /watch NDJSON wire contract (core/contracts.py).

These pin the line shapes the frontend + crimson-sources depend on. They cover:
  1. the builders emit lines that validate against the canonical JSON Schema,
  2. the committed schema file matches what the code generates (drift guard —
     run `python -m core.contracts` to refresh after a deliberate change),
  3. the app imports + produces an OpenAPI document (an import-smoke test for the
     whole route layer, which the pure-unit tests don't otherwise exercise).
"""

import json

import pytest
from jsonschema import Draft202012Validator

from core import contracts


@pytest.fixture(scope="module")
def validator():
    return Draft202012Validator(contracts.WATCH_NDJSON_SCHEMA)


def _validates(validator, line):
    errors = sorted(validator.iter_errors(line), key=str)
    assert not errors, f"{line} failed: {[e.message for e in errors]}"


def test_meta_line_validates(validator):
    line = contracts.build_meta_line(
        tmdb_id=1429, season_number=1, episode_number=1, anilist_id=16498, title="AoT",
    )
    assert line["type"] == "meta" and line["success"] is True
    _validates(validator, line)


def test_meta_line_allows_movie_nulls(validator):
    # Movies carry no season/episode/anilist — those are null, and the schema
    # must accept that (it's exactly what the movie /watch path emits).
    _validates(validator, contracts.build_meta_line(
        tmdb_id=550, season_number=None, episode_number=None, anilist_id=None, title="Fight Club",
    ))


def test_unaired_line_validates(validator):
    _validates(validator, contracts.build_unaired_line(
        air_date="2099-01-01", title="Future", season_number=1, episode_number=99,
    ))


def test_stream_line_validates_without_cache_ticket(validator):
    line = contracts.build_stream_line(
        {"source": "Voe", "type": "hls", "url": "https://x/y.m3u8",
         "language": "en", "subtitles": None},
    )
    assert "cacheTicket" not in line  # omitted, not null, when absent
    assert line["streamType"] == "hls"
    _validates(validator, line)


def test_stream_line_includes_cache_ticket_when_present(validator):
    line = contracts.build_stream_line(
        {"source": "Voe", "type": "mp4", "url": "https://x/y.mp4",
         "language": None, "subtitles": [{"url": "https://s/en.vtt", "lang": "en"}],
         "cacheTicket": "signed-ticket"},
    )
    assert line["cacheTicket"] == "signed-ticket"
    _validates(validator, line)


def test_done_line_validates(validator):
    _validates(validator, contracts.build_done_line(7))


def test_unknown_type_is_rejected(validator):
    # A line the client doesn't know about must NOT pass the schema (guards the
    # discriminated-union completeness).
    assert list(validator.iter_errors({"type": "surprise"}))


def test_committed_schema_matches_generated():
    # The vendored copy (and the frontend's) is generated from the code; if this
    # fails, run `python -m core.contracts` to refresh it.
    with open(contracts.export_path(), encoding="utf-8") as fh:
        on_disk = fh.read()
    assert on_disk == contracts.schema_json(), (
        "contracts/watch_ndjson.schema.json is stale — run `python -m core.contracts`"
    )


def test_app_imports_and_openapi_generates():
    # Import-smoke test for the entire route layer: catches a broken import /
    # decorator / route registration in CI *before* the image is built.
    from api import app

    doc = app.openapi()
    paths = doc.get("paths", {})
    assert paths, "OpenAPI produced no paths"
    # A few load-bearing endpoints the frontend contract depends on must exist.
    joined = json.dumps(list(paths.keys()))
    for needle in ["/watch/", "/info/", "/trending", "/search/"]:
        assert needle in joined, f"missing expected path containing {needle}"

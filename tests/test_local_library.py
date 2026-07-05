"""
Browsable local-media library: the scanner's metadata precedence + show/movie
classification, the token-scoped detail lookup, filename search, and the signed
/local_art token round-trip.

Pure logic on a temp fixture tree with a monkeypatched enabled-roots config, so no
database (or ffmpeg/TMDB) is needed — matching the suite's no-fixtures philosophy.
"""

import local_engine.library as lib
from local_engine import fs


def _point_roots_at(monkeypatch, root, *, label="Media", encoding=False):
    """Make the Local store report exactly one enabled root (no DB). library._store
    IS fs._store, so this single patch covers scan + playability + labels."""
    monkeypatch.setattr(
        fs._store, "enabled_roots_config",
        lambda: [{"path": str(root), "encoding": encoding, "label": label}],
    )


def _mp4(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00")


# --- classification ---------------------------------------------------------
def test_show_folder_with_episodes_is_a_show(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path)
    _mp4(tmp_path / "Cowboy Bebop" / "S01E01.mp4")
    _mp4(tmp_path / "Cowboy Bebop" / "S01E02.mp4")

    items = lib.scan_library()
    assert len(items) == 1
    it = items[0]
    assert it["media_kind"] == "show"
    assert it["episode_count"] == 2
    assert it["source_label"] == "Media"
    assert it["has_metadata"] is False  # filename-only
    assert it["title"] == "Cowboy Bebop"


def test_single_file_folder_is_a_movie(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path)
    _mp4(tmp_path / "Akira (1988)" / "Akira.1988.1080p.BluRay.mp4")

    items = lib.scan_library()
    assert len(items) == 1
    it = items[0]
    assert it["media_kind"] == "movie"
    assert it["year"] == 1988
    # Release junk stripped down to a clean title.
    assert it["title"].lower().startswith("akira")


def test_loose_file_in_root_is_a_movie_title(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path)
    _mp4(tmp_path / "Some Random Clip.mp4")

    items = lib.scan_library()
    assert len(items) == 1
    assert items[0]["media_kind"] == "movie"
    assert items[0]["title"] == "Some Random Clip"


# --- metadata precedence ----------------------------------------------------
def test_nfo_metadata_wins_over_filename(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path)
    d = tmp_path / "junk.folder.name.2001"
    _mp4(d / "video.mp4")
    (d / "movie.nfo").write_text(
        """<?xml version="1.0"?>
        <movie>
          <title>The Real Title</title>
          <year>1999</year>
          <plot>A plot.</plot>
          <genre>Action</genre>
          <genre>Sci-Fi</genre>
          <uniqueid type="tmdb">603</uniqueid>
        </movie>""",
        encoding="utf-8",
    )

    it = lib.scan_library()[0]
    assert it["media_kind"] == "movie"
    assert it["title"] == "The Real Title"
    assert it["year"] == 1999
    assert it["tmdb_id"] == 603
    assert set(it["genres"]) == {"Action", "Sci-Fi"}
    assert it["has_metadata"] is True


def test_sidecar_json_used_when_no_nfo(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path)
    d = tmp_path / "Whatever"
    _mp4(d / "ep.mp4")
    (d / "metadata.json").write_text(
        '{"title": "JSON Title", "year": 2010, "genres": ["Drama"], "tmdb_id": 42}',
        encoding="utf-8",
    )

    it = lib.scan_library()[0]
    assert it["title"] == "JSON Title"
    assert it["year"] == 2010
    assert it["genres"] == ["Drama"]
    assert it["tmdb_id"] == 42
    assert it["has_metadata"] is True


# --- detail lookup ----------------------------------------------------------
def test_overview_lists_episodes_grouped_by_season(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path)
    show = tmp_path / "Frieren"
    _mp4(show / "S01E01.mp4")
    _mp4(show / "S01E02.mp4")
    _mp4(show / "S02E01.mp4")

    it = next(i for i in lib.scan_library() if i["title"] == "Frieren")
    detail = lib.get_library_item(it["id"])
    assert detail["media_kind"] == "show"
    seasons = {s["season_number"]: s for s in detail["seasons"]}
    assert set(seasons) == {1, 2}
    assert [e["episode_number"] for e in seasons[1]["episodes"]] == [1, 2]
    # Every episode carries a playable file token.
    assert all(e["id"] for s in detail["seasons"] for e in s["episodes"])


def test_movie_overview_exposes_a_play_token(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path)
    _mp4(tmp_path / "Spirited Away" / "Spirited.Away.mp4")

    it = lib.scan_library()[0]
    detail = lib.get_library_item(it["id"])
    assert detail["seasons"] == []
    assert detail["play"] and detail["play"]["id"]
    # The play token resolves back to the real file inside the enabled root.
    assert fs.safe_resolve(detail["play"]["id"]) is not None


def test_get_item_rejects_token_outside_enabled_root(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path / "lib")
    outside = tmp_path / "outside"
    _mp4(outside / "x.mp4")
    token = fs.encode_token(str(outside))
    assert lib.get_library_item(token) is None


# --- search -----------------------------------------------------------------
def test_search_matches_on_title_substring(tmp_path, monkeypatch):
    _point_roots_at(monkeypatch, tmp_path)
    _mp4(tmp_path / "Attack on Titan" / "S01E01.mp4")
    _mp4(tmp_path / "Death Note" / "S01E01.mp4")

    items = lib.scan_library()
    hits = lib.search_library("titan", items=items)
    assert len(hits) == 1
    assert hits[0]["title"] == "Attack on Titan"
    assert lib.search_library("nonexistent", items=items) == []


# --- signed artwork ---------------------------------------------------------
def test_art_url_is_signed_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_SECRET", "test-secret")
    _point_roots_at(monkeypatch, tmp_path)
    d = tmp_path / "Movie"
    _mp4(d / "movie.mp4")
    poster = d / "poster.jpg"
    poster.write_bytes(b"\xff\xd8\xff")  # jpeg-ish

    it = lib.scan_library()[0]
    assert it["poster"] and it["poster"].startswith("/local_art?f=")
    # Pull f + s out of the signed URL and confirm it resolves.
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(it["poster"]).query)
    assert fs.safe_resolve_art(q["f"][0], q["s"][0]) == str(poster)
    # A tampered signature is rejected.
    assert fs.safe_resolve_art(q["f"][0], "deadbeef") is None

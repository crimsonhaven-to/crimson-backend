"""The share layout: Windows-safe names, stable paths, nothing outside the root."""

import os

import pytest

from music_engine import fs


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path))
    return tmp_path


def test_names_are_cleaned_for_windows():
    assert fs.safe_segment('AC/DC: "Live"?', "x") == "AC_DC_ _Live__"
    assert fs.safe_segment("Trailing dots...", "x") == "Trailing dots"
    assert fs.safe_segment("CON", "fallback") == "fallback"
    assert fs.safe_segment("   ", "fallback") == "fallback"
    assert len(fs.safe_segment("a" * 500, "x")) == 100


def test_path_uses_album_artist_album_and_number():
    track = {"title": "Song", "artists": ["Feat", "Other"], "album_artist": "Band",
             "album": "Record", "track_number": 3, "disc_number": 1}
    assert fs.plan_rel_path(track) == os.path.join("Band", "Record", "03 - Song.m4a")


def test_second_disc_is_prefixed_and_missing_album_is_singles():
    assert fs.plan_rel_path(
        {"title": "Song", "artists": ["Band"], "album": "Record", "track_number": 5,
         "disc_number": 2}
    ).endswith("2-05 - Song.m4a")
    assert fs.plan_rel_path({"title": "Song", "artists": ["Band"]}) == os.path.join(
        "Band", "Singles", "Song.m4a"
    )


def test_a_taken_name_gets_a_counter(root):
    rel = os.path.join("Band", "Record", "01 - Song.m4a")
    (root / "Band" / "Record").mkdir(parents=True)
    (root / rel).write_bytes(b"x")
    assert fs.unique_rel_path(rel) == os.path.join("Band", "Record", "01 - Song (2).m4a")


def test_absolute_refuses_escapes(root):
    assert fs.absolute("Band/song.m4a") == os.path.realpath(root / "Band" / "song.m4a")
    assert fs.absolute("../etc/passwd") is None
    assert fs.absolute("") is None


def test_publish_moves_the_work_file_into_place(root):
    work = fs.work_dir(7)
    source = os.path.join(work, "tagged.m4a")
    with open(source, "wb") as handle:
        handle.write(b"audio")
    dest = fs.publish(source, os.path.join("Band", "Record", "01 - Song.m4a"))
    assert open(dest, "rb").read() == b"audio"
    assert not os.path.exists(source)
    fs.remove_work_dir(7)
    assert not os.path.exists(work)


def test_an_album_keeps_its_first_cover(root):
    work = fs.work_dir(1)
    cover = os.path.join(work, "cover.jpg")
    open(cover, "wb").write(b"first")
    rel = os.path.join("Band", "Record", "01 - Song.m4a")
    (root / "Band" / "Record").mkdir(parents=True)
    assert fs.place_cover(cover, rel) == os.path.join("Band", "Record", "cover.jpg")
    open(cover, "wb").write(b"second")
    fs.place_cover(cover, rel)
    assert (root / "Band" / "Record" / "cover.jpg").read_bytes() == b"first"


def test_playlist_file_lists_relative_paths(root):
    rel = fs.write_playlist_file(
        "ray",
        "Road Trip",
        [{"rel_path": os.path.join("Band", "Record", "01 - Song.m4a"), "title": "Song",
          "artists": ["Band"], "duration_ms": 200_500}],
    )
    assert rel == os.path.join("Playlists", "ray", "Road Trip.m3u8")
    lines = (root / rel).read_text().splitlines()
    assert lines == ["#EXTM3U", "#EXTINF:200,Band - Song", "../../Band/Record/01 - Song.m4a"]

"""The ffmpeg tag pass: copy AAC, encode anything else, tags from the import."""

from music_engine.tagging import tag_args

TRACK = {
    "title": "Song",
    "artists": ["Band", "Guest"],
    "album_artist": "",
    "track_number": 3,
    "disc_number": None,
    "release_date": "2019-05-01",
}


def _metadata(args):
    return [args[i + 1] for i, a in enumerate(args) if a == "-metadata"]


def test_aac_is_copied_and_tagged_with_cover():
    args = tag_args("in.m4a", "cover.jpg", "out.m4a", TRACK, "Record")
    assert args[args.index("-c:a") + 1] == "copy"
    assert "attached_pic" in args
    assert _metadata(args) == [
        "title=Song", "artist=Band, Guest", "album=Record", "album_artist=Band",
        "track=3", "date=2019",
    ]
    assert args[-1] == "out.m4a"


def test_other_codecs_are_encoded_and_no_cover_means_no_video_map():
    args = tag_args("in.webm", None, "out.m4a", TRACK, "")
    assert args[args.index("-c:a") + 1] == "aac"
    assert "1:v:0" not in args
    assert not any(m.startswith("album=") for m in _metadata(args))

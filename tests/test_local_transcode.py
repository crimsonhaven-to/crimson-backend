"""
Local-source on-the-fly HLS transcoding: playlist math, the segment ffmpeg
command, and the encoding-aware playability/resolve choke points.

Pure logic only — the playlist/segment helpers never touch the network, and the
fs gating is exercised with a temp file + a monkeypatched enabled-roots config so
no database is needed (matching the suite's no-fixtures philosophy).
"""

import local_engine.transcode as t
from local_engine import fs


# --- playlist + segmentation ------------------------------------------------
def test_segment_count_covers_duration_with_a_short_tail():
    seg = t.SEGMENT_SECONDS
    assert t.segment_count(0.5) == 1                 # always at least one
    assert t.segment_count(seg) == 1                 # exactly one segment
    assert t.segment_count(seg + 0.1) == 2           # spills into a second
    assert t.segment_count(seg * 3) == 3


def test_media_playlist_is_a_complete_vod_list():
    seg = t.SEGMENT_SECONDS
    duration = seg * 2 + 2.0
    pl = t.build_media_playlist(duration)
    lines = pl.strip().splitlines()

    assert lines[0] == "#EXTM3U"
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines
    assert lines[-1] == "#EXT-X-ENDLIST"                 # VOD is fully terminated

    # One EXTINF + one seg URI per segment, numbered from 0.
    seg_uris = [l for l in lines if l.endswith(".ts")]
    assert seg_uris == ["seg0.ts", "seg1.ts", "seg2.ts"]

    # The final segment carries only the remainder (here 2.0s), not a full window.
    extinfs = [l for l in lines if l.startswith("#EXTINF:")]
    assert extinfs[-1].startswith("#EXTINF:2.000000")


def test_segment_args_fast_seek_and_timeline_offset_match_the_index():
    seg = t.SEGMENT_SECONDS
    args = t._segment_args("/lib/movie.mkv", 3)
    start = str(3 * seg)
    # Input (fast) seek BEFORE -i, window bound, and a matching timeline offset so
    # hls.js stitches the segment at its real position.
    assert args[args.index("-i") - 1] == start          # -ss <start> -i
    assert args[args.index("-i") + 1] == "/lib/movie.mkv"
    assert args[args.index("-t") + 1] == str(seg)
    assert args[args.index("-output_ts_offset") + 1] == start
    # Web-universal codecs + an independently-decodable keyframe per segment.
    assert "libx264" in args and "aac" in args
    assert "-force_key_frames" in args


# --- encoding-aware playability + resolve gating ----------------------------
def _point_roots_at(monkeypatch, root, *, encoding):
    """Make the Local store report exactly one enabled root, with the given
    encoding flag, without a DB."""
    monkeypatch.setattr(
        fs._store, "enabled_roots_config",
        lambda: [{"path": str(root), "encoding": encoding}],
    )


def test_mkv_only_playable_when_its_source_has_encoding_on(tmp_path, monkeypatch):
    mkv = tmp_path / "Show" / "S01E01.mkv"
    mkv.parent.mkdir(parents=True)
    mkv.write_bytes(b"\x00")
    mp4 = tmp_path / "Show" / "S01E02.mp4"
    mp4.write_bytes(b"\x00")

    # Encoding OFF: web-native still plays, transcodable does not.
    _point_roots_at(monkeypatch, tmp_path, encoding=False)
    assert fs.is_playable_path(str(mp4)) is True
    assert fs.is_playable_path(str(mkv)) is False

    # Encoding ON: the mkv becomes playable (via transcode).
    _point_roots_at(monkeypatch, tmp_path, encoding=True)
    assert fs.is_playable_path(str(mkv)) is True


def test_safe_resolve_transcode_enforces_encoding_and_extension(tmp_path, monkeypatch):
    mkv = tmp_path / "movie.mkv"
    mkv.write_bytes(b"\x00")
    mp4 = tmp_path / "movie.mp4"
    mp4.write_bytes(b"\x00")

    mkv_token = fs.encode_token(str(mkv))
    mp4_token = fs.encode_token(str(mp4))

    # Encoding ON: the transcode route resolves the mkv but NOT the (direct-play) mp4.
    _point_roots_at(monkeypatch, tmp_path, encoding=True)
    assert fs.safe_resolve_transcode(mkv_token) == str(mkv)
    assert fs.safe_resolve_transcode(mp4_token) is None

    # Encoding OFF: nothing transcodes, even a valid token for an in-root mkv.
    _point_roots_at(monkeypatch, tmp_path, encoding=False)
    assert fs.safe_resolve_transcode(mkv_token) is None

    # Outside any enabled root -> rejected regardless.
    _point_roots_at(monkeypatch, tmp_path / "other", encoding=True)
    assert fs.safe_resolve_transcode(mkv_token) is None

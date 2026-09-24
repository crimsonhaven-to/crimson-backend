"""One track through the worker: match, fetch, tag, publish, with the store and
the tag pass stubbed so no database or ffmpeg is needed."""

import os

import pytest

from music_engine import worker as worker_module
from music_engine.provider import Candidate, Fetched, Match, ProviderError

TRACK = {
    "id": 9,
    "title": "Song",
    "artists": ["Band"],
    "album": "",
    "album_artist": "Band",
    "track_number": 1,
    "disc_number": 1,
    "release_date": None,
    "duration_ms": 200_000,
    "isrc": None,
    "cover_url": None,
    "match_url": None,
}


class FakeProvider:
    def __init__(self, match):
        self._match = match
        self.fetched = []

    async def match(self, track):
        return self._match

    async def search(self, query):
        return []

    async def fetch(self, url, work_dir):
        self.fetched.append(url)
        path = os.path.join(work_dir, "source.m4a")
        with open(path, "wb") as handle:
            handle.write(b"audio")
        return Fetched(path=path, album="Found Album")

    async def public_playlist(self, playlist_id):
        raise NotImplementedError


class FakeStore:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return [] if name == "playlists_for_track" else None

        return record

    def names(self):
        return [name for name, _a, _k in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path))
    store = FakeStore()
    monkeypatch.setattr(worker_module, "store", store)

    async def fake_tag(source, cover, dest, track, album):
        with open(dest, "wb") as handle:
            handle.write(open(source, "rb").read())
        return None

    monkeypatch.setattr(worker_module.tagging, "write_tagged", fake_tag)
    return tmp_path, store


async def test_an_auto_match_is_downloaded_and_published(env):
    root, store = env
    provider = FakeProvider(Match(kind="auto", url="https://yt/1", candidates=[]))
    await worker_module.MusicWorker()._process(provider, dict(TRACK), "Band - Song")

    assert provider.fetched == ["https://yt/1"]
    assert store.names() == ["set_matched", "mark_ready", "playlists_for_track"]
    ready = store.calls[1][2]
    assert ready["album"] == "Found Album"
    assert (root / ready["rel_path"]).read_bytes() == b"audio"
    assert ready["rel_path"] == os.path.join("Band", "Found Album", "01 - Song.m4a")


async def test_a_known_url_skips_matching(env):
    _root, store = env
    provider = FakeProvider(Match(kind="none"))
    await worker_module.MusicWorker()._process(
        provider, {**TRACK, "match_url": "https://yt/manual"}, "x"
    )
    assert provider.fetched == ["https://yt/manual"]
    assert "set_matched" not in store.names()


async def test_an_unsure_match_goes_to_review_without_downloading(env):
    _root, store = env
    candidates = [Candidate(url="https://yt/2", title="Song", channel="c", duration_ms=1)]
    provider = FakeProvider(Match(kind="review", candidates=candidates))
    await worker_module.MusicWorker()._process(provider, dict(TRACK), "x")
    assert provider.fetched == []
    assert store.names() == ["mark_review"]


async def test_no_match_is_unmatched(env):
    _root, store = env
    await worker_module.MusicWorker()._process(FakeProvider(Match(kind="none")), dict(TRACK), "x")
    name, _args, kwargs = store.calls[0]
    assert (name, kwargs["status"]) == ("mark_failed", "unmatched")


async def test_a_retryable_provider_error_requeues_and_pauses(env):
    _root, store = env

    class Limited(FakeProvider):
        async def fetch(self, url, work_dir):
            raise ProviderError("rate limited", retryable=True)

    worker = worker_module.MusicWorker()
    await worker._run(Limited(Match(kind="auto", url="https://yt/1")), dict(TRACK))
    assert "requeue" in store.names()
    assert worker._paused_until > 0


async def test_a_permanent_provider_error_fails_the_track(env):
    _root, store = env

    class Gone(FakeProvider):
        async def fetch(self, url, work_dir):
            raise ProviderError("video removed")

    await worker_module.MusicWorker()._run(Gone(Match(kind="auto", url="u")), dict(TRACK))
    assert store.names()[-1] == "mark_failed"


class SourceTagged(FakeProvider):
    async def fetch(self, url, work_dir):
        fetched = await super().fetch(url, work_dir)
        fetched.title, fetched.artists = "Real Title", ["Real Artist"]
        return fetched


async def test_a_song_added_by_search_takes_the_sources_tags(env):
    _root, store = env
    track = {**TRACK, "album_artist": "", "match_url": "https://yt/3", "tags_from_source": True}
    await worker_module.MusicWorker()._process(SourceTagged(Match(kind="none")), track, "x")
    ready = store.calls[0][2]
    assert (ready["title"], ready["artists"]) == ("Real Title", ["Real Artist"])
    assert ready["rel_path"].startswith("Real Artist")


async def test_an_imported_song_keeps_spotifys_tags(env):
    _root, store = env
    track = {**TRACK, "match_url": "https://yt/3"}
    await worker_module.MusicWorker()._process(SourceTagged(Match(kind="none")), track, "x")
    assert store.calls[0][2]["title"] == "Song"


async def test_a_song_added_by_search_uses_the_sources_full_size_cover(env, monkeypatch):
    asked = []

    async def fake_cover(url, work):
        asked.append(url)
        return None

    monkeypatch.setattr(worker_module, "_download_cover", fake_cover)

    class WithCover(SourceTagged):
        async def fetch(self, url, work_dir):
            fetched = await super().fetch(url, work_dir)
            fetched.cover_url = "https://img/full.jpg"
            return fetched

    track = {**TRACK, "match_url": "u", "cover_url": "https://img/small.jpg"}
    await worker_module.MusicWorker()._process(WithCover(Match(kind="none")), track, "x")
    await worker_module.MusicWorker()._process(
        WithCover(Match(kind="none")), {**track, "tags_from_source": True}, "x"
    )
    assert asked == ["https://img/small.jpg", "https://img/full.jpg"]

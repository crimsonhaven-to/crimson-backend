"""Genre normalization and the worker's paced Spotify lookups, with Spotify and
the database stubbed."""

from pathlib import Path

import pytest

from music_engine import genres
from music_engine.spotify import SpotifyError

MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "015_music_genres.sql"
).read_text()


def test_genres_are_lowercased_split_and_deduplicated():
    assert genres.normalize(["J-Pop", "  city   pop ", "j-pop; Anime, ,", ""]) == [
        "j-pop", "city pop", "anime",
    ]


def test_existing_spotify_playlists_are_read_again_for_their_artist_ids():
    assert "snapshot_id = NULL, last_synced_at = NULL" in MIGRATION
    assert "WHERE source = 'spotify'" in MIGRATION


class FakeDb:
    def __init__(self, unknown=(), unresolved=(), user=7):
        self.user = user
        self.unknown = list(unknown)
        self.unresolved = list(unresolved)
        self.artists = {}
        self.artist_ids = {}

    def lookup_user(self):
        return self.user

    def unknown_artist_ids(self, limit):
        return self.unknown[:limit]

    def tracks_without_artist_ids(self, limit):
        return self.unresolved[:limit]

    def save_artist(self, spotify_id, name, genre_list):
        self.artists[spotify_id] = (name, genre_list)

    def set_artist_ids(self, track_id, ids):
        self.artist_ids[track_id] = ids


class FakeClient:
    def __init__(self, artists=None, found=None, error=None):
        self.artists = artists or {}
        self.found = found or {}
        self.error = error
        self.requests = 0

    async def artist(self, spotify_id):
        self.requests += 1
        if self.error:
            raise self.error
        return self.artists[spotify_id]

    async def find_artist(self, name):
        self.requests += 1
        return self.found.get(name)


@pytest.fixture
def fakes(monkeypatch):
    def install(db, client):
        monkeypatch.setattr(genres, "genres_db", db)
        monkeypatch.setattr(genres, "SpotifyClient", lambda user_id: client)
        return db, client

    return install


async def test_unknown_artists_are_looked_up_and_saved(fakes):
    db, client = fakes(
        FakeDb(unknown=["a1"]),
        FakeClient(artists={"a1": {"name": "Band", "genres": ["J-Pop", "City Pop"]}}),
    )
    assert await genres.enrich(3) == 1
    assert db.artists == {"a1": ("Band", ["j-pop", "city pop"])}


async def test_a_song_without_ids_gets_its_first_artist_found_by_name(fakes):
    db, _client = fakes(
        FakeDb(unresolved=[{"id": 5, "artists": ["Band", "Guest"]},
                           {"id": 6, "artists": ["Nobody"]}]),
        FakeClient(found={"Band": {"id": "a1", "name": "Band"}}),
    )
    assert await genres.enrich(3) == 2
    # Found or not, each track is stamped so it is not searched again.
    assert db.artist_ids == {5: ["a1"], 6: []}


async def test_the_budget_caps_requests_across_both_passes(fakes):
    _db, client = fakes(
        FakeDb(unknown=["a1", "a2"], unresolved=[{"id": 5, "artists": ["Band"]}]),
        FakeClient(artists={"a1": {"name": "A"}, "a2": {"name": "B"}}),
    )
    assert await genres.enrich(2) == 2
    assert client.requests == 2


async def test_an_artist_spotify_does_not_know_is_saved_without_genres(fakes):
    db, _client = fakes(FakeDb(unknown=["bad"]), FakeClient(error=SpotifyError("gone", 404)))
    await genres.enrich(3)
    assert db.artists == {"bad": ("", [])}


async def test_a_rate_limit_propagates_so_the_worker_backs_off(fakes):
    db, _client = fakes(FakeDb(unknown=["a1"]), FakeClient(error=SpotifyError("slow", 429)))
    with pytest.raises(SpotifyError):
        await genres.enrich(3)
    assert db.artists == {}


async def test_nothing_happens_without_a_spotify_link(fakes):
    _db, client = fakes(FakeDb(unknown=["a1"], user=None), FakeClient())
    assert await genres.enrich(3) == 0
    assert client.requests == 0

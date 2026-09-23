"""The windowed AniList airing fetch: one paged query for the whole window, and a
partial window kept rather than discarded.
"""

from datetime import datetime, timezone

import pytest




from notify_engine.schedule import fetch_window


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _page(schedules, has_next=False):
    return _Resp({"data": {"Page": {
        "pageInfo": {"hasNextPage": has_next},
        "airingSchedules": schedules,
    }}})


class _Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.variables = []

    async def post(self, url, json=None, timeout=None):
        self.variables.append((json or {}).get("variables"))
        return self.pages.pop(0)


async def test_the_window_is_one_query_not_one_per_subscription(monkeypatch):
    """Driving fetch_anilist_metadata per follow would be a round trip each and
    would churn the shared response cache every refresh."""
    client = _Client([_page([{"mediaId": 21, "episode": 1100, "airingAt": 1757000000}])])
    monkeypatch.setattr("notify_engine.schedule.anilist_post",
                        lambda c, q, v=None, **k: c.post("", json={"variables": v}))

    rows = await fetch_window(client, lookback_hours=36, horizon_days=7)

    assert len(client.variables) == 1
    assert rows == [(21, 1100, datetime.fromtimestamp(1757000000, tz=timezone.utc), None)]


async def test_paging_follows_has_next_page(monkeypatch):
    client = _Client([
        _page([{"mediaId": 1, "episode": 1, "airingAt": 1757000000}], has_next=True),
        _page([{"mediaId": 2, "episode": 2, "airingAt": 1757000600}], has_next=False),
    ])
    monkeypatch.setattr("notify_engine.schedule.anilist_post",
                        lambda c, q, v=None, **k: c.post("", json={"variables": v}))

    rows = await fetch_window(client, 36, 7)

    assert [r[0] for r in rows] == [1, 2]
    assert [v["page"] for v in client.variables] == [1, 2]


async def test_a_partial_window_is_kept_not_discarded(monkeypatch):
    """A refresh that dies halfway leaves the calendar mostly right; raising
    would leave it empty until the next tick."""
    class _Down(_Resp):
        status_code = 503

    client = _Client([
        _page([{"mediaId": 1, "episode": 1, "airingAt": 1757000000}], has_next=True),
        _Down({}),
    ])
    monkeypatch.setattr("notify_engine.schedule.anilist_post",
                        lambda c, q, v=None, **k: c.post("", json={"variables": v}))

    rows = await fetch_window(client, 36, 7)
    assert [r[0] for r in rows] == [1]


async def test_the_title_rides_along_with_the_schedule(monkeypatch):
    """anime_entries is filled by the Fribb resync and lags a new season, which
    is exactly when a title is most worth following. AniList can name the show in
    the request the poller already makes, so the calendar never has to show a
    bare id."""
    client = _Client([_page([{
        "mediaId": 21, "episode": 1100, "airingAt": 1757000000,
        "media": {"title": {"romaji": "One Piece", "english": "ONE PIECE"}},
    }])])
    monkeypatch.setattr("notify_engine.schedule.anilist_post",
                        lambda c, q, v=None, **k: c.post("", json={"variables": v}))

    rows = await fetch_window(client, 36, 7)
    assert rows[0][3] == "ONE PIECE", "English is preferred when AniList has one"


async def test_the_romaji_title_is_used_when_there_is_no_english(monkeypatch):
    client = _Client([_page([{
        "mediaId": 21, "episode": 1, "airingAt": 1757000000,
        "media": {"title": {"romaji": "Sousou no Frieren", "english": None}},
    }])])
    monkeypatch.setattr("notify_engine.schedule.anilist_post",
                        lambda c, q, v=None, **k: c.post("", json={"variables": v}))

    rows = await fetch_window(client, 36, 7)
    assert rows[0][3] == "Sousou no Frieren"


@pytest.mark.parametrize("media", [
    None, {}, {"title": None}, {"title": {}},
    {"title": {"romaji": None, "english": None}},
    {"title": {"romaji": "   ", "english": ""}},
])
async def test_an_airing_with_no_usable_title_still_counts(monkeypatch, media):
    """A schedule row is the point; the name is a bonus. Dropping the airing
    because AniList had no title for it would lose it from the calendar."""
    client = _Client([_page([
        {"mediaId": 21, "episode": 1, "airingAt": 1757000000, "media": media},
    ])])
    monkeypatch.setattr("notify_engine.schedule.anilist_post",
                        lambda c, q, v=None, **k: c.post("", json={"variables": v}))

    rows = await fetch_window(client, 36, 7)
    assert len(rows) == 1 and rows[0][3] is None


async def test_malformed_schedule_entries_are_skipped(monkeypatch):
    client = _Client([_page([
        {"mediaId": 21, "episode": None, "airingAt": 1757000000},   # unnumbered
        {"mediaId": None, "episode": 1, "airingAt": 1757000000},    # no media
        {"mediaId": 22, "episode": 3, "airingAt": None},            # unscheduled
        {"mediaId": 23, "episode": 4, "airingAt": 1757000000},      # good
    ])])
    monkeypatch.setattr("notify_engine.schedule.anilist_post",
                        lambda c, q, v=None, **k: c.post("", json={"variables": v}))

    rows = await fetch_window(client, 36, 7)
    assert [r[0] for r in rows] == [23]

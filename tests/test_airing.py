"""
The airing calendar and its notifications.

The part worth testing hardest is the ordering in ``notifier.send_due_notifications``:
claim, then send, then record. Everything else here degrades into a missing
calendar row; getting that ordering wrong sends a subscriber the same email on
every tick for as long as the episode stays inside the lookback window.

No database and no SMTP: the store and the mailer are both faked, which is
exactly the seam the real code is built around.
"""

from datetime import datetime, timedelta, timezone

import pytest

from account_engine import mailer
from core.config import get_settings
from notify_engine import notifier
from notify_engine.schedule import fetch_window


# --- fakes ------------------------------------------------------------------

class FakeStore:
    """Records claims and outcomes; claims succeed once per key, like the real
    INSERT ... ON CONFLICT DO NOTHING."""

    def __init__(self, pending=None, already_claimed=()):
        self._pending = list(pending or [])
        self.claimed = set(already_claimed)
        self.claim_calls = []
        self.outcomes = []

    def pending_notifications(self, lookback_hours, limit):
        self.lookback_hours = lookback_hours
        self.limit = limit
        return list(self._pending[:limit])

    def claim(self, user_id, anilist_id, episode):
        key = (user_id, anilist_id, episode)
        self.claim_calls.append(key)
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def record_outcome(self, user_id, anilist_id, episode, sent):
        self.outcomes.append((user_id, anilist_id, episode, sent))


def _pending(user_id=1, anilist_id=21, episode=1100, email="a@b.c", title="One Piece"):
    return {
        "user_id": user_id,
        "anilist_id": anilist_id,
        "episode": episode,
        "airing_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        "email": email,
        "username": "mortal",
        "title": title,
    }


@pytest.fixture
def wired(monkeypatch):
    """A store and a mailer whose send order is observable."""
    sent = []

    def _send(messages, progress=None):
        for message in messages:
            sent.append(message)
            if progress:
                progress(message, True)
        return {"sent": len(messages), "failed": 0}

    monkeypatch.setattr(mailer, "is_configured", lambda: True)
    monkeypatch.setattr(notifier.mailer, "send_airing_batch", _send)
    monkeypatch.setattr(get_settings(), "airing_notify_dry_run", False)
    return sent


# --- the ordering that matters ----------------------------------------------

def test_every_notification_is_claimed_before_anything_is_sent(monkeypatch, wired):
    """Claim first, always. Sending first and recording after would resend the
    same mail on every tick if the process died in between."""
    order = []
    store = FakeStore([_pending(episode=1), _pending(episode=2)])

    real_claim = store.claim

    def _claim(*args):
        order.append(("claim", args[2]))
        return real_claim(*args)
    store.claim = _claim

    def _send(messages, progress=None):
        for message in messages:
            order.append(("send", message["_episode"]))
            if progress:
                progress(message, True)
        return {"sent": len(messages), "failed": 0}
    monkeypatch.setattr(notifier.mailer, "send_airing_batch", _send)
    monkeypatch.setattr(notifier, "store", store)

    notifier.send_due_notifications()

    assert order == [("claim", 1), ("claim", 2), ("send", 1), ("send", 2)]


def test_an_already_claimed_episode_is_never_sent_again(monkeypatch, wired):
    """The ledger is what makes a second replica, or a second tick inside the
    lookback window, harmless."""
    store = FakeStore([_pending(episode=7)], already_claimed={(1, 21, 7)})
    monkeypatch.setattr(notifier, "store", store)

    result = notifier.send_due_notifications()

    assert wired == [], "a claimed episode must produce no mail at all"
    assert result["claimed"] == 0
    assert result["skipped"] == 1


def test_a_failed_send_is_recorded_as_failed_not_unclaimed(monkeypatch, wired):
    """Un-claiming to retry would reopen the resend hole the claim closes. A mail
    nobody can deliver is better lost than sent forever."""
    store = FakeStore([_pending(episode=3)])
    monkeypatch.setattr(notifier, "store", store)

    def _send(messages, progress=None):
        for message in messages:
            progress(message, False)
        return {"sent": 0, "failed": len(messages)}
    monkeypatch.setattr(notifier.mailer, "send_airing_batch", _send)

    notifier.send_due_notifications()

    assert store.outcomes == [(1, 21, 3, False)]
    assert (1, 21, 3) in store.claimed, "the claim must survive the failure"


def test_each_outcome_is_recorded_individually(monkeypatch, wired):
    """One bad address must not mark the whole batch failed, nor the whole batch
    sent. That is why the mailer reports per recipient."""
    store = FakeStore([_pending(episode=i) for i in (1, 2, 3)])
    monkeypatch.setattr(notifier, "store", store)

    def _send(messages, progress=None):
        for i, message in enumerate(messages):
            progress(message, i != 1)  # the middle one bounces
        return {"sent": 2, "failed": 1}
    monkeypatch.setattr(notifier.mailer, "send_airing_batch", _send)

    notifier.send_due_notifications()

    assert [(o[2], o[3]) for o in store.outcomes] == [(1, True), (2, False), (3, True)]


# --- the dry run ------------------------------------------------------------

def test_a_dry_run_claims_and_logs_but_opens_no_connection(monkeypatch):
    store = FakeStore([_pending(episode=9)])
    monkeypatch.setattr(notifier, "store", store)
    monkeypatch.setattr(get_settings(), "airing_notify_dry_run", True)

    def _explode(*args, **kwargs):
        raise AssertionError("a dry run must not reach the mailer")
    monkeypatch.setattr(notifier.mailer, "send_airing_batch", _explode)

    result = notifier.send_due_notifications()

    assert result["claimed"] == 1
    assert result["sent"] == 0
    # Claiming for real is the point: the claim path is what a rehearsal is for.
    assert (1, 21, 9) in store.claimed


# --- the bounds -------------------------------------------------------------

def test_the_run_is_bounded_and_uses_the_lookback(monkeypatch, wired):
    store = FakeStore([])
    monkeypatch.setattr(notifier, "store", store)

    notifier.send_due_notifications()

    assert store.limit == notifier.MAX_PER_RUN
    assert store.lookback_hours == notifier.LOOKBACK_HOURS


def test_nothing_pending_does_no_work(monkeypatch, wired):
    store = FakeStore([])
    monkeypatch.setattr(notifier, "store", store)

    result = notifier.send_due_notifications()

    assert result == {"claimed": 0, "sent": 0, "failed": 0, "skipped": 0}
    assert store.claim_calls == []
    assert wired == []


def test_the_lookback_is_wide_enough_to_survive_an_outage():
    """The window has to cover more than one notify interval, or a few hours of
    downtime silently drops every notification it spanned."""
    assert notifier.LOOKBACK_HOURS >= 12


# --- the copy ---------------------------------------------------------------

def test_the_email_says_aired_in_japan_not_available_now():
    """Load-bearing wording. AniList gives the broadcast time; the backend cannot
    know when a source has the episode, because third-party sources resolve in the
    viewer's own browser. Promising availability is a support burden the
    architecture cannot pay off."""
    text, html_body = mailer.airing_bodies("One Piece", 1100, "mortal", "https://x/anime/21")

    assert "aired in Japan" in text
    assert "aired in Japan" in html_body
    for promise in ("available now", "watch now", "ready to watch"):
        assert promise not in text.lower()
        assert promise not in html_body.lower()


def test_the_email_escapes_the_title():
    """The title is a snapshot the client supplied at subscribe time, so it is
    user input on its way into an HTML email."""
    _, html_body = mailer.airing_bodies("<script>alert(1)</script>", 1, None, "https://x")
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body


def test_a_missing_title_still_produces_a_usable_notice():
    text, _ = mailer.airing_bodies("", 5, None, "https://x")
    assert "Episode 5" in text


# --- the windowed AniList fetch ---------------------------------------------

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

"""The airing notifier's ordering: claim, then send, then record. Getting it wrong
mails a subscriber the same episode on every tick while it stays inside the
lookback window. The store and the mailer are faked.
"""

from datetime import datetime, timedelta, timezone

import pytest

from account_engine import mailer

from core.config import get_settings

from notify_engine import notifier



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

    def claim(self, keys):
        self.claim_calls.append(list(keys))
        won = {key for key in keys if key not in self.claimed}
        self.claimed |= won
        return won

    def record_outcomes(self, outcomes):
        self.outcomes.extend((*key, sent) for key, sent in outcomes)


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


def test_every_notification_is_claimed_before_anything_is_sent(monkeypatch, wired):
    """Claim first, always. Sending first and recording after would resend the
    same mail on every tick if the process died in between."""
    order = []
    store = FakeStore([_pending(episode=1), _pending(episode=2)])

    real_claim = store.claim

    def _claim(keys):
        order.append(("claim", [episode for _, _, episode in keys]))
        return real_claim(keys)
    store.claim = _claim

    def _send(messages, progress=None):
        for message in messages:
            order.append(("send", message["subject"]))
            if progress:
                progress(message, True)
        return {"sent": len(messages), "failed": 0}
    monkeypatch.setattr(notifier.mailer, "send_airing_batch", _send)
    monkeypatch.setattr(notifier, "store", store)

    notifier.send_due_notifications()

    assert order == [
        ("claim", [1, 2]),
        ("send", "One Piece - episode 1 has aired"),
        ("send", "One Piece - episode 2 has aired"),
    ]


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

"""Outgoing mail without an SMTP server. Unconfigured mail fails soft, one bad
recipient never aborts the rest of a fan-out, and the airing notice says the
episode aired in Japan rather than that it is available now.
"""

import contextlib

import pytest

from account_engine import mailer






@pytest.fixture
def no_smtp(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)


def test_unconfigured_send_email_is_soft(no_smtp):
    assert mailer.is_configured() is False
    assert mailer.send_email("a@b.c", "hi", "text") is False


def test_unconfigured_broadcast_is_soft(no_smtp):
    recipients = [{"email": "a@b.c", "username": None}, {"email": "d@e.f", "username": "Lumi"}]
    result = mailer.send_broadcast(recipients, "subject", "message")
    assert result == {"sent": 0, "failed": 2}


def test_broadcast_bodies_personalisation():
    text, html_body = mailer._broadcast_bodies("Line one\nLine <two>", "Lumi")
    assert text.startswith("Greetings, Lumi.")
    assert "Line one\nLine <two>" in text
    # HTML variant escapes markup and turns newlines into <br>
    assert "Line one<br>Line &lt;two&gt;" in html_body
    assert "Greetings, Lumi." in html_body

    text_anon, html_anon = mailer._broadcast_bodies("msg", None)
    assert text_anon.startswith("Greetings, mortal.")
    assert "Greetings, mortal." in html_anon


def test_broadcast_skips_bad_recipient(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")

    sent_to = []

    class FakeServer:
        def send_message(self, msg):
            if msg["To"] == "broken@b.c":
                raise RuntimeError("mailbox unavailable")
            sent_to.append(msg["To"])

    @contextlib.contextmanager
    def fake_connection():
        yield FakeServer()

    monkeypatch.setattr(mailer, "_connection", fake_connection)

    progress_calls = []
    recipients = [
        {"email": "a@b.c", "username": "Alice"},
        {"email": "broken@b.c", "username": None},
        {"email": "c@b.c", "username": None},
    ]
    result = mailer.send_broadcast(
        recipients, "subject", "message",
        progress=lambda s, f: progress_calls.append((s, f)),
    )
    assert result == {"sent": 2, "failed": 1}
    assert sent_to == ["a@b.c", "c@b.c"]
    assert progress_calls[-1] == (2, 1)


def test_broadcast_connection_failure_counts_rest_as_failed(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")

    @contextlib.contextmanager
    def dead_connection():
        raise RuntimeError("connect timeout")
        yield  # pragma: no cover

    monkeypatch.setattr(mailer, "_connection", dead_connection)
    result = mailer.send_broadcast([{"email": "a@b.c"}, {"email": "b@b.c"}], "s", "m")
    assert result == {"sent": 0, "failed": 2}


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

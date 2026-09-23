"""
Transactional email: verification and password reset links.

Pure stdlib so it adds no dependency to the slim image. Configured entirely
from SMTP_* plus FRONTEND_BASE_URL; see .env.example for the full list. An
unset SMTP_HOST disables emailing.

Sending is blocking, so callers go through ``run_in_threadpool``. It fails soft:
an SMTP error is logged and returns False rather than raising, so registration
still succeeds while mail is down and the user can ask for a resend later.
"""

import html
import logging
import smtplib
import ssl
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr

from core.config import get_settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(get_settings().smtp_host)


def frontend_base_url() -> str:
    return get_settings().frontend_base_url


def _from_address() -> str:
    settings = get_settings()
    return settings.smtp_from or settings.smtp_user or "service@agony.ch"


@contextmanager
def _connection():
    """A logged-in SMTP connection. Raises on any failure; callers decide how
    soft to fail."""
    settings = get_settings()
    host = settings.smtp_host
    if not host:
        raise RuntimeError("SMTP_HOST unset")
    port = settings.smtp_port
    security = settings.smtp_security
    user = settings.smtp_user or _from_address()
    password = settings.smtp_password
    context = ssl.create_default_context()
    if security == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
            if password:
                server.login(user, password)
            yield server
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            if security == "starttls":
                server.starttls(context=context)
            if password:
                server.login(user, password)
            yield server


def _build_message(to: str, subject: str, text: str, html_body: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((get_settings().smtp_from_name, _from_address()))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Send one message. False, with a log line, on failure or missing config."""
    if not is_configured():
        logger.warning("[mailer] SMTP_HOST unset, skipping email to %s (%r)", to, subject)
        return False
    try:
        with _connection() as server:
            server.send_message(_build_message(to, subject, text, html))
        logger.info("[mailer] sent %r to %s", subject, to)
        return True
    except Exception as e:  # noqa: BLE001 - fail soft, never break the request
        logger.error("[mailer] failed sending to %s: %s", to, e)
        return False


# --- branded templates -----------------------------------------------------
def _wrap(
    title: str,
    body_html: str,
    footer: str = "If you didn't request this, you can safely ignore this message.",
) -> str:
    return f"""\
<div style="background:#0a0305;padding:40px 0;font-family:Inter,Segoe UI,Arial,sans-serif">
  <div style="max-width:480px;margin:0 auto;background:#15080c;border:1px solid #3a0d18;
              border-radius:24px;padding:40px;color:#f4d9df">
    <h1 style="margin:0 0 8px;font-size:26px;font-weight:900;letter-spacing:-1px;color:#fff">
      crimson<span style="color:#ff2d55;font-weight:300">haven</span>
    </h1>
    <p style="margin:0 0 28px;font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#b03050">
      {title}
    </p>
    {body_html}
    <p style="margin:32px 0 0;font-size:11px;color:#6b1f2e;line-height:1.6">
      {footer}
    </p>
  </div>
</div>"""


def _button(href: str, label: str) -> str:
    return (
        f'<a href="{href}" style="display:inline-block;background:#e11d48;color:#fff;'
        'text-decoration:none;padding:14px 28px;border-radius:14px;font-weight:800;'
        'font-size:13px;letter-spacing:1px;text-transform:uppercase">'
        f"{label}</a>"
    )


def send_verification_email(to: str, token: str) -> bool:
    link = f"{frontend_base_url()}/verify?token={token}"
    text = (
        "Welcome to CrimsonHaven.\n\n"
        "Confirm your email to activate your account:\n"
        f"{link}\n\n"
        "This link expires in 24 hours."
    )
    html = _wrap(
        "Confirm your descent",
        f'<p style="font-size:14px;line-height:1.7;color:#d9aab4;margin:0 0 24px">'
        "Welcome, mortal. Confirm your email to unlock the haven."
        "</p>"
        f"{_button(link, 'Verify Email')}"
        f'<p style="font-size:11px;color:#6b1f2e;margin:24px 0 0">This link expires in 24 hours.</p>',
    )
    return send_email(to, "Verify your CrimsonHaven account", text, html)


def send_reset_email(to: str, token: str) -> bool:
    link = f"{frontend_base_url()}/reset?token={token}"
    text = (
        "A password reset was requested for your CrimsonHaven account.\n\n"
        f"Reset it here:\n{link}\n\n"
        "This link expires in 1 hour. If you didn't request it, ignore this email."
    )
    html = _wrap(
        "Reset your key",
        f'<p style="font-size:14px;line-height:1.7;color:#d9aab4;margin:0 0 24px">'
        "A password reset was requested. Choose a new password below."
        "</p>"
        f"{_button(link, 'Reset Password')}"
        f'<p style="font-size:11px;color:#6b1f2e;margin:24px 0 0">This link expires in 1 hour.</p>',
    )
    return send_email(to, "Reset your CrimsonHaven password", text, html)


# --- admin broadcast ---------------------------------------------------------
def _broadcast_bodies(message: str, username: str | None) -> tuple[str, str]:
    """(text, html) for one recipient, personalised when they set a display name."""
    greeting = f"Greetings, {username}." if username else "Greetings, mortal."
    text = f"{greeting}\n\n{message}"
    body = html.escape(message).replace("\n", "<br>")
    html_body = _wrap(
        "A message from the haven",
        f'<p style="font-size:14px;line-height:1.7;color:#d9aab4;margin:0 0 16px">{html.escape(greeting)}</p>'
        f'<p style="font-size:14px;line-height:1.7;color:#d9aab4;margin:0">{body}</p>',
        footer="You're receiving this because you're a member of CrimsonHaven.",
    )
    return text, html_body


def send_broadcast(recipients: list[dict], subject: str, message: str, progress=None) -> dict:
    """Send ``message`` to every recipient over one SMTP connection. Fails soft
    per recipient, so one bad address doesn't abort the rest, and overall, so a
    dead server yields sent=0 rather than an exception. ``progress`` is called as
    progress(sent, failed) after each attempt. Blocking; run in a threadpool."""
    sent, failed = 0, 0
    if not is_configured():
        logger.warning("[mailer] SMTP_HOST unset, broadcast %r skipped", subject)
        return {"sent": 0, "failed": len(recipients)}
    try:
        with _connection() as server:
            for r in recipients:
                text, html_body = _broadcast_bodies(message, r.get("username"))
                try:
                    server.send_message(_build_message(r["email"], subject, text, html_body))
                    sent += 1
                except Exception as e:  # noqa: BLE001 - skip the bad address, keep going
                    logger.error("[mailer] broadcast to %s failed: %s", r.get("email"), e)
                    failed += 1
                if progress:
                    progress(sent, failed)
    except Exception as e:  # noqa: BLE001 - connection died, the rest never sent
        logger.error("[mailer] broadcast %r aborted: %s", subject, e)
        failed = len(recipients) - sent
        if progress:
            progress(sent, failed)
    logger.info("[mailer] broadcast %r: %d sent, %d failed", subject, sent, failed)
    return {"sent": sent, "failed": failed}


# --- airing notifications ----------------------------------------------------
# One connection for the whole burst, like send_broadcast: a popular seasonal
# title notifies all of its subscribers within one poll tick, and transactional
# SMTP providers rate limit per connection as well as per minute.

def airing_bodies(title: str, episode: int, username: str | None, link: str) -> tuple[str, str]:
    """(text, html) for one "a new episode aired" notice.

    The copy says *aired in Japan*, not "available now", and that wording is
    load-bearing. AniList gives the broadcast time; the backend genuinely cannot
    know when a source has the episode, because third-party sources resolve in
    the viewer's own browser by design. Promising availability would be a support
    burden the architecture cannot pay off.
    """
    greeting = f"Greetings, {username}." if username else "Greetings, mortal."
    safe_title = html.escape(title or "A title you follow")
    text = (
        f"{greeting}\n\n"
        f"Episode {episode} of {title} has aired in Japan.\n\n"
        f"{link}\n\n"
        "Sources may take a little while to catch up.\n"
        "You're receiving this because you follow this title on CrimsonHaven."
    )
    html_body = _wrap(
        "A new episode has aired",
        f'<p style="font-size:14px;line-height:1.7;color:#d9aab4;margin:0 0 8px">'
        f"{html.escape(greeting)}</p>"
        f'<p style="font-size:18px;line-height:1.5;color:#fff;font-weight:700;margin:0 0 4px">'
        f"{safe_title}</p>"
        f'<p style="font-size:14px;line-height:1.7;color:#d9aab4;margin:0 0 24px">'
        f"Episode {episode} has aired in Japan. Sources may take a little while to catch up."
        "</p>"
        f"{_button(link, 'Open in the Haven')}",
        footer="You're receiving this because you follow this title. "
               "Manage what you follow in your account settings.",
    )
    return text, html_body


def send_airing_batch(messages: list[dict], progress=None) -> dict:
    """Send every notice over one SMTP connection, failing soft per recipient.

    ``messages`` are ``{email, subject, text, html}``. ``progress`` is called as
    progress(message, sent: bool) after each attempt, which is how the caller
    records each outcome in the ledger as it happens rather than assuming the
    whole batch shared one fate.

    Mirrors send_broadcast: one bad address does not abort the rest, and a dead
    server yields sent=0 rather than an exception.
    """
    sent, failed = 0, 0
    if not is_configured():
        logger.warning("[mailer] SMTP_HOST unset, %d airing notice(s) skipped", len(messages))
        if progress:
            for message in messages:
                progress(message, False)
        return {"sent": 0, "failed": len(messages)}
    try:
        with _connection() as server:
            for message in messages:
                ok = False
                try:
                    server.send_message(_build_message(
                        message["email"], message["subject"], message["text"], message.get("html")
                    ))
                    ok = True
                    sent += 1
                except Exception as e:  # noqa: BLE001 - skip the bad address, keep going
                    logger.error("[mailer] airing notice to %s failed: %s", message["email"], e)
                    failed += 1
                if progress:
                    progress(message, ok)
    except Exception as e:  # noqa: BLE001 - connection died, the rest never sent
        logger.error("[mailer] airing batch aborted after %d sent: %s", sent, e)
        remaining = messages[sent + failed:]
        failed += len(remaining)
        if progress:
            for message in remaining:
                progress(message, False)
    logger.info("[mailer] airing notices: %d sent, %d failed", sent, failed)
    return {"sent": sent, "failed": failed}

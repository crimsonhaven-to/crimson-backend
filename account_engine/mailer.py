"""
Transactional email for the email+password sign-in path (verification + password
reset links).

Pure stdlib (``smtplib`` + ``email.message``) so it adds no dependency on the
``python:3.14-slim`` image. Configuration is env-driven (see .env.example):

    SMTP_HOST            e.g. mail.infomaniak.com   (unset => emailing disabled)
    SMTP_PORT            default 587
    SMTP_SECURITY        starttls (default) | ssl | none
    SMTP_USER            login user (optional; defaults to SMTP_FROM)
    SMTP_PASSWORD        login password
    SMTP_FROM            envelope/From address      (defaults to SMTP_USER)
    SMTP_FROM_NAME       display name, default "CrimsonHaven"
    FRONTEND_BASE_URL    used to build the links, e.g. https://crimsonhaven.to

``send_email`` is synchronous and blocking; callers invoke it through Starlette's
threadpool (``run_in_threadpool``) so it never stalls the event loop. It fails
soft — a misconfiguration or SMTP error is logged and returns False rather than
raising into the request — so registration still succeeds even if mail is down
(the user can use "resend verification" once mail is fixed).
"""

import html
import logging
import os
import smtplib
import ssl
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def frontend_base_url() -> str:
    return (os.getenv("FRONTEND_BASE_URL") or "https://crimsonhaven.to").rstrip("/")


def _from_address() -> str:
    return os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "service@agony.ch"


@contextmanager
def _connection():
    """A logged-in SMTP connection per the env config. Raises on any failure
    (missing config, connect/auth error) — callers decide how soft to fail."""
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST unset")
    port = int(os.getenv("SMTP_PORT", "587"))
    security = (os.getenv("SMTP_SECURITY") or "starttls").lower()
    user = os.getenv("SMTP_USER") or _from_address()
    password = os.getenv("SMTP_PASSWORD") or ""
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
    msg["From"] = formataddr((os.getenv("SMTP_FROM_NAME", "CrimsonHaven"), _from_address()))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Send one message. Returns True on success, False (logged) on any failure
    or when SMTP isn't configured."""
    if not is_configured():
        logger.warning("[mailer] SMTP_HOST unset — skipping email to %s (%r)", to, subject)
        return False
    try:
        with _connection() as server:
            server.send_message(_build_message(to, subject, text, html))
        logger.info("[mailer] sent %r to %s", subject, to)
        return True
    except Exception as e:  # noqa: BLE001 — fail soft, never break the request
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
    """(text, html) for one broadcast recipient: the admin's plaintext message,
    personalised with the account's display name when they've set one."""
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
    """Send the admin's plaintext ``message`` to every recipient (dicts with
    ``email`` + optional ``username``) over ONE SMTP connection. Fails soft per
    recipient (one bad address doesn't abort the rest) and entirely (a dead/
    unconfigured server yields sent=0, not an exception). ``progress``, if given,
    is called as progress(sent, failed) after each attempt so the caller can
    surface live status. Blocking — run through a threadpool."""
    sent, failed = 0, 0
    if not is_configured():
        logger.warning("[mailer] SMTP_HOST unset — broadcast %r skipped", subject)
        return {"sent": 0, "failed": len(recipients)}
    try:
        with _connection() as server:
            for r in recipients:
                text, html_body = _broadcast_bodies(message, r.get("username"))
                try:
                    server.send_message(_build_message(r["email"], subject, text, html_body))
                    sent += 1
                except Exception as e:  # noqa: BLE001 — skip the bad address, keep going
                    logger.error("[mailer] broadcast to %s failed: %s", r.get("email"), e)
                    failed += 1
                if progress:
                    progress(sent, failed)
    except Exception as e:  # noqa: BLE001 — connection/auth died; the rest never sent
        logger.error("[mailer] broadcast %r aborted: %s", subject, e)
        failed = len(recipients) - sent
        if progress:
            progress(sent, failed)
    logger.info("[mailer] broadcast %r: %d sent, %d failed", subject, sent, failed)
    return {"sent": sent, "failed": failed}

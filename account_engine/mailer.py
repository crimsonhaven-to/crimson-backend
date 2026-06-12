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

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def frontend_base_url() -> str:
    return (os.getenv("FRONTEND_BASE_URL") or "https://crimsonhaven.to").rstrip("/")


def _from_address() -> str:
    return os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "service@agony.ch"


def send_email(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Send one message. Returns True on success, False (logged) on any failure
    or when SMTP isn't configured."""
    host = os.getenv("SMTP_HOST")
    if not host:
        logger.warning("[mailer] SMTP_HOST unset — skipping email to %s (%r)", to, subject)
        return False

    port = int(os.getenv("SMTP_PORT", "587"))
    security = (os.getenv("SMTP_SECURITY") or "starttls").lower()
    user = os.getenv("SMTP_USER") or _from_address()
    password = os.getenv("SMTP_PASSWORD") or ""
    from_name = os.getenv("SMTP_FROM_NAME", "CrimsonHaven")

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, _from_address()))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        context = ssl.create_default_context()
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
                if password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                if security == "starttls":
                    server.starttls(context=context)
                if password:
                    server.login(user, password)
                server.send_message(msg)
        logger.info("[mailer] sent %r to %s", subject, to)
        return True
    except Exception as e:  # noqa: BLE001 — fail soft, never break the request
        logger.error("[mailer] failed sending to %s: %s", to, e)
        return False


# --- branded templates -----------------------------------------------------
def _wrap(title: str, body_html: str) -> str:
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
      If you didn't request this, you can safely ignore this message.
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

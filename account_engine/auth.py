"""Sign-in, registration and ownership checks for both identity types.

A mnemonic account is an Ed25519 public key derived in the browser from a
12-word BIP39 mnemonic; it proves itself by signing a one-time challenge, so a
database leak exposes no credential. An email account uses a PBKDF2 password
hash and must verify its address before it can sign in.

Everything here is synchronous (hashing, SMTP, database), so route handlers run
each flow in one thread-pool hop.
"""

import re
from typing import Optional

from fastapi import HTTPException, Request

from core.config import get_settings

from . import audit, ed25519, mailer, passwords
from .db import RESET_TOKEN_TTL, VERIFY_TOKEN_TTL, store
from .schemas import DeleteAccountRequest, EmailRegisterRequest

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX128 = re.compile(r"^[0-9a-fA-F]{128}$")
# Shape only: deliverability is proven by the verification link.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHALLENGE_PURPOSE = "auth"
# Hashed when the account does not exist, so response time is no oracle.
_DUMMY_HASH = "pbkdf2_sha256$1$AAAA$AAAA"


# --- invites --------------------------------------------------------------------
def check_invite_code(code: str, request: Optional[Request] = None,
                      identity: Optional[str] = None, flow: Optional[str] = None) -> bool:
    """Accept a reusable SIGNUP_INVITE_CODE (returns True) or an available
    single-use token from the Discord bot (returns False), else 403.

    Does not burn the token: ``consume_invite_code`` does that once the account
    is committed, so a later 409 does not waste it."""
    # Demo growth is bounded by the nightly reset instead.
    if get_settings().demo_mode:
        return True
    code = (code or "").strip()
    is_static = code in get_settings().signup_invite_code
    if not is_static and not store.invite_token_is_available(code):
        audit.log_event(
            "invite_invalid", outcome="failure", request=request, identity=identity,
            detail={"flow": flow, "reason": "unknown_code"},
        )
        raise HTTPException(status_code=403, detail="Invalid invite code")
    return is_static


def consume_invite_code(code: str, is_static: bool, used_by: str,
                        request: Optional[Request] = None, flow: Optional[str] = None) -> None:
    """Burn a single-use token. A concurrent signup that took it first gets 403."""
    if is_static:
        return
    if not store.consume_invite_token((code or "").strip(), used_by=used_by):
        audit.log_event(
            "invite_invalid", outcome="failure", request=request, identity=used_by,
            detail={"flow": flow, "reason": "already_used"},
        )
        raise HTTPException(status_code=403, detail="This invite code has already been used")


# --- sessions ------------------------------------------------------------------
def _device(request: Optional[Request]) -> tuple:
    """(user_agent, ip) for a new session row, clipped like the audit log's."""
    if request is None:
        return (None, None)
    ua = (request.headers.get("user-agent") or "").strip()[:300] or None
    return (ua, audit.client_ip(request))


def open_session(account: dict, request: Optional[Request]) -> tuple:
    token, expires_at = store.create_session(account["user_id"], *_device(request))
    store.touch_login(account["user_id"])
    return token, expires_at


def _email_session(account: dict, created: bool, request: Optional[Request]) -> dict:
    token, expires_at = open_session(account, request)
    return {
        "success": True,
        "email": account.get("email"),
        "label": account.get("label"),
        "session_token": token,
        "expires_at": expires_at,
        "created": created,
    }


# --- mnemonic -------------------------------------------------------------------
def normalize_public_key(public_key: str) -> str:
    pk = (public_key or "").lower()
    if not _HEX64.match(pk):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars")
    return pk


def verify_signed_challenge(public_key: str, challenge: str, signature: str,
                            request: Optional[Request] = None, flow: Optional[str] = None) -> None:
    """Consume the one-time challenge and verify the signature over it, or 401."""
    if not _HEX64.match(public_key or ""):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars (32-byte Ed25519 key)")
    if not _HEX128.match(signature or ""):
        raise HTTPException(status_code=400, detail="signature must be 128 hex chars (64-byte Ed25519 signature)")

    public_key = public_key.lower()
    # Consumed before verifying, so a failed attempt cannot be replayed.
    if not store.consume_challenge(challenge, public_key, CHALLENGE_PURPOSE):
        audit.log_event(
            "login_failed", outcome="failure", request=request,
            identity=audit.key_prefix(public_key),
            detail={"method": "mnemonic", "flow": flow, "reason": "bad_challenge"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired challenge")

    if not ed25519.verify(bytes.fromhex(public_key), challenge.encode("utf-8"), bytes.fromhex(signature)):
        audit.log_event(
            "login_failed", outcome="failure", request=request,
            identity=audit.key_prefix(public_key),
            detail={"method": "mnemonic", "flow": flow, "reason": "bad_signature"},
        )
        raise HTTPException(status_code=401, detail="Signature verification failed")


def register_mnemonic(pk: str, challenge: str, signature: str, invite_code: str,
                      label: Optional[str], request: Request) -> dict:
    """Create the account for a public key and open a session.

    The 409 and the invite check both run before the challenge is consumed: a
    409 leaves it intact for the client's register-then-login fallback, and a
    single-use invite burns only once the signature has verified."""
    if store.get_account_by_public_key(pk):
        raise HTTPException(status_code=409, detail="Account already exists; use /auth/login")
    is_static = check_invite_code(invite_code, request, audit.key_prefix(pk), "mnemonic_register")
    verify_signed_challenge(pk, challenge, signature, request, "register")
    consume_invite_code(invite_code, is_static, used_by=f"mnemonic:{pk}",
                        request=request, flow="mnemonic_register")
    account = store.create_account(pk, label)
    token, expires_at = open_session(account, request)
    audit.log_event(
        "register_success", outcome="success", request=request,
        user_id=account["user_id"], identity=audit.key_prefix(pk),
        detail={"method": "mnemonic"},
    )
    return {"public_key": pk, "label": account.get("label"), "session_token": token,
            "expires_at": expires_at, "created": True}


def login_mnemonic(pk: str, challenge: str, signature: str, request: Request) -> dict:
    """The 404 runs before the challenge is consumed, leaving it intact for the
    client's login-then-register fallback. It is no security event: a 256-bit
    keyspace makes probing keys meaningless."""
    account = store.get_account_by_public_key(pk)
    if not account:
        raise HTTPException(status_code=404, detail="No account for this key; use /auth/register")
    verify_signed_challenge(pk, challenge, signature, request, "login")
    token, expires_at = open_session(account, request)
    audit.log_event(
        "login_success", outcome="success", request=request,
        user_id=account["user_id"], identity=audit.key_prefix(pk),
        detail={"method": "mnemonic"},
    )
    return {"public_key": pk, "label": account.get("label"), "session_token": token,
            "expires_at": expires_at, "created": False}


# --- email ------------------------------------------------------------------------
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _validate_email(email: str) -> str:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    return email


def validate_password(password: str) -> None:
    if not isinstance(password, str) or not (
        passwords.MIN_PASSWORD_LENGTH <= len(password) <= passwords.MAX_PASSWORD_LENGTH
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Password must be {passwords.MIN_PASSWORD_LENGTH} to "
                   f"{passwords.MAX_PASSWORD_LENGTH} characters",
        )


def register_email(body: EmailRegisterRequest, request: Request) -> dict:
    """Create an unverified account and mail the verification link. No session
    until the address is verified, except in demo mode, which has no SMTP."""
    email = _validate_email(body.email)
    validate_password(body.password)
    is_static = check_invite_code(body.invite_code, request, email, "email_register")

    if store.get_account_by_email(email):
        audit.log_event(
            "register_blocked", outcome="failure", request=request, identity=email,
            detail={"method": "email", "reason": "email_taken"},
        )
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    consume_invite_code(body.invite_code, is_static, used_by=email,
                        request=request, flow="email_register")
    account = store.create_email_account(email, passwords.hash_password(body.password), body.label)
    audit.log_event(
        "register_success", outcome="success", request=request,
        user_id=account["user_id"], identity=email, detail={"method": "email"},
    )

    if get_settings().demo_mode:
        store.set_email_verified(account["user_id"], True)
        account = store.get_account(account["user_id"])
        return {**_email_session(account, True, request), "requires_verification": False}

    mailer.send_verification_email(
        email, store.create_email_token(account["user_id"], "verify", VERIFY_TOKEN_TTL)
    )
    return {
        "success": True,
        "requires_verification": True,
        "email": email,
        "message": "Account created. Check your email to verify your account.",
    }


def login_email(email: str, password: str, request: Request) -> dict:
    """401 on bad credentials, kept generic so it is no account-existence
    oracle; 403 while unverified."""
    email = normalize_email(email)
    account = store.get_account_by_email(email)
    stored_hash = account.get("password_hash") if account else None
    ok = passwords.verify_password(password, stored_hash or _DUMMY_HASH)
    if not account or not stored_hash or not ok:
        audit.log_event(
            "login_failed", outcome="failure", request=request, identity=email,
            user_id=account["user_id"] if account else None,
            detail={"method": "email", "reason": "bad_credentials"},
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not account.get("email_verified"):
        audit.log_event(
            "login_unverified", outcome="failure", request=request, identity=email,
            user_id=account["user_id"], detail={"method": "email"},
        )
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before signing in. Check your inbox or request a new link.",
        )

    if passwords.needs_rehash(stored_hash):
        store.set_password(account["user_id"], passwords.hash_password(password))

    audit.log_event(
        "login_success", outcome="success", request=request, identity=email,
        user_id=account["user_id"], detail={"method": "email"},
    )
    return _email_session(account, False, request)


def verify_email(token: str, request: Request) -> dict:
    """Mark the address verified and sign straight in."""
    user_id = store.consume_email_token(token, "verify")
    if user_id is None:
        audit.log_event("verify_failed", outcome="failure", request=request)
        raise HTTPException(status_code=400, detail="This verification link is invalid or has expired")
    store.set_email_verified(user_id, True)
    account = store.get_account(user_id)
    audit.log_event(
        "email_verified", outcome="success", request=request,
        user_id=user_id, identity=account.get("email"),
    )
    return _email_session(account, True, request)


def resend_verification(email: str, request: Request) -> None:
    """Sends only for an existing unverified account, but always logs: a burst of
    resends for unknown addresses is exactly the probing the ledger is for."""
    email = normalize_email(email)
    account = store.get_account_by_email(email)
    sent = bool(account and account.get("email") and not account.get("email_verified"))
    if sent:
        mailer.send_verification_email(
            email, store.create_email_token(account["user_id"], "verify", VERIFY_TOKEN_TTL)
        )
    audit.log_event("verify_resend_requested", request=request, identity=email, detail={"sent": sent})


def request_password_reset(email: str, request: Request) -> None:
    email = normalize_email(email)
    account = store.get_account_by_email(email)
    sent = bool(account and account.get("password_hash"))
    if sent:
        mailer.send_reset_email(
            email, store.create_email_token(account["user_id"], "reset", RESET_TOKEN_TTL)
        )
    audit.log_event("password_reset_requested", request=request, identity=email, detail={"sent": sent})


def reset_password(token: str, password: str, request: Request) -> None:
    """Set the new password and revoke every session. Holding the inbox also
    proves the address, so it is marked verified."""
    validate_password(password)
    user_id = store.consume_email_token(token, "reset")
    if user_id is None:
        audit.log_event("password_reset_failed", outcome="failure", request=request)
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired")
    store.set_password(user_id, passwords.hash_password(password))
    store.set_email_verified(user_id, True)
    store.revoke_user_sessions(user_id)
    audit.log_event("password_reset_success", outcome="success", request=request, user_id=user_id)


# --- ownership --------------------------------------------------------------------
def confirm_owner(user: dict, body: DeleteAccountRequest, request: Request) -> None:
    """Re-prove ownership before an irreversible action. A bearer token alone is
    not enough: it is the one credential an attacker can hold without being the
    owner."""
    stored_hash = user.get("password_hash")
    if stored_hash:
        if not body.password or not passwords.verify_password(body.password, stored_hash):
            audit.log_event(
                "account_delete_failed", outcome="failure", request=request,
                user_id=user["user_id"], detail={"reason": "bad_password"},
            )
            raise HTTPException(status_code=401, detail="Password is incorrect")
        return

    public_key = user.get("public_key")
    if not public_key:
        raise HTTPException(
            status_code=400,
            detail="This account has no password or key to confirm with; contact an admin",
        )
    if not body.challenge or not body.signature:
        raise HTTPException(
            status_code=400,
            detail="Sign a challenge from /auth/challenge to confirm deletion",
        )
    verify_signed_challenge(public_key, body.challenge, body.signature, request, "delete_account")

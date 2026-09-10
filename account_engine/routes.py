"""
Account API: sign-in, favorites, watchlists and watch progress.

Two ways in, both invite-gated at registration:

  * Mnemonic. The client derives an Ed25519 keypair from a 12-word BIP39
    mnemonic and the public key *is* the account. Identity is proven by signing
    a one-time challenge, so the server only ever verifies and a DB leak exposes
    no credential.

        POST /auth/challenge {public_key} -> {challenge}
        POST /auth/register  {public_key, challenge, signature, invite_code}
        POST /auth/login     {public_key, challenge, signature}

  * Email + password, with a PBKDF2 hash and mandatory email verification
    (/auth/email/*).

Both return a session token passed as ``Authorization: Bearer``. Favorites are
show-level and belong to a named list (default 'favorites'); progress is
per-episode. Both are structured rows so the backend can serve continue-watching.
"""

import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from . import audit, ed25519, mailer, passwords
from .db import AccountStore, QuotaExceeded, VERIFY_TOKEN_TTL, RESET_TOKEN_TTL
from core.config import Config
from core.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])
store = AccountStore()

# Injected by api.py to avoid a circular import. Annotates deduped progress rows
# with TMDB "next episode" hints so the frontend never points at an episode that
# does not exist or has not aired.
_episode_enricher = None  # async callable(rows) -> None, mutates in place


def set_episode_enricher(handler) -> None:
    """Register the progress-row enricher; called by api.py at startup."""
    global _episode_enricher
    _episode_enricher = handler


# Injected like the enricher. On a progress save it scrapes and resolves the NEXT
# episode ahead of time so Continue Watching plays instantly off the NAS.
# Best-effort and fire-and-forget; a no-op when unset.
_warmup_handler = None  # callable(request, *, tmdb_id, season_number, episode_number, preferences)


def set_warmup_handler(handler) -> None:
    """Register the continue-watching warmup scheduler; called by api.py."""
    global _warmup_handler
    _warmup_handler = handler


async def _enrich(rows: List[dict]) -> List[dict]:
    """Run the injected enricher. Best-effort: the metadata is additive, so rows
    come back unchanged if it is unset or raises."""
    if _episode_enricher and rows:
        try:
            await _episode_enricher(rows)
        except Exception as e:
            logger.warning(f"progress enrichment failed: {e}")
    return rows

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")   # 32-byte public key
_HEX128 = re.compile(r"^[0-9a-fA-F]{128}$")  # 64-byte signature
# Shape check only, avoiding the email-validator dependency. Deliverability is
# proven by the verification link, not by this regex.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHALLENGE_PURPOSE = "auth"


def _allowed_invite_codes() -> set:
    """Shared invite codes from SIGNUP_INVITE_CODE. Unset means no code matches,
    closing registration, which fails safe for an invite-only site."""
    raw = os.getenv("SIGNUP_INVITE_CODE", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def _check_invite_code(code: str, request: Optional[Request] = None,
                       identity: Optional[str] = None, flow: Optional[str] = None) -> bool:
    """Validate an invite code for new-account creation, shared by the email and
    mnemonic flows. The one field accepts either a reusable SIGNUP_INVITE_CODE or
    a single-use token minted by the Discord bot.

    True for a static code, False for an available single-use token, 403 for
    neither. Does NOT consume the token: burn it with _consume_invite_code only
    once committed, so a later 409 doesn't waste it. request/identity/flow are
    audit context for the ``invite_invalid`` event."""
    # Demo deployments accept any code so anyone can try the site; growth is
    # bounded by the nightly reset instead. True keeps _consume_invite_code a no-op.
    if Config.DEMO_MODE:
        return True
    code = (code or "").strip()
    static_codes = _allowed_invite_codes()
    is_static = bool(static_codes) and code in static_codes
    if not is_static and not store.invite_token_is_available(code):
        audit.log_event(
            "invite_invalid", outcome="failure", request=request, identity=identity,
            detail={"flow": flow, "reason": "unknown_code"},
        )
        raise HTTPException(status_code=403, detail="Invalid invite code")
    return is_static


def _consume_invite_code(code: str, is_static: bool, used_by: str,
                         request: Optional[Request] = None, flow: Optional[str] = None) -> None:
    """Burn a single-use invite token; a no-op for a static code. Race-safe: if a
    concurrent signup took it since _check_invite_code, this fails closed with 403."""
    if is_static:
        return
    if not store.consume_invite_token((code or "").strip(), used_by=used_by):
        audit.log_event(
            "invite_invalid", outcome="failure", request=request, identity=used_by,
            detail={"flow": flow, "reason": "already_used"},
        )
        raise HTTPException(status_code=403, detail="This invite code has already been used")


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# --- helpers ---------------------------------------------------------------
def _verify_signed_challenge(public_key: str, challenge: str, signature: str,
                             request: Optional[Request] = None, flow: Optional[str] = None) -> None:
    """Consume the one-time challenge and verify the signature over it. 401 on any
    failure, recorded as a ``login_failed`` event; request/flow are audit context."""
    if not _HEX64.match(public_key or ""):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars (32-byte Ed25519 key)")
    if not _HEX128.match(signature or ""):
        raise HTTPException(status_code=400, detail="signature must be 128 hex chars (64-byte Ed25519 signature)")

    public_key = public_key.lower()
    # Consume first so a failed attempt can't be replayed.
    if not store.consume_challenge(challenge, public_key, CHALLENGE_PURPOSE):
        audit.log_event(
            "login_failed", outcome="failure", request=request,
            identity=audit.key_prefix(public_key),
            detail={"method": "mnemonic", "flow": flow, "reason": "bad_challenge"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired challenge")

    ok = ed25519.verify(
        bytes.fromhex(public_key),
        challenge.encode("utf-8"),
        bytes.fromhex(signature),
    )
    if not ok:
        audit.log_event(
            "login_failed", outcome="failure", request=request,
            identity=audit.key_prefix(public_key),
            detail={"method": "mnemonic", "flow": flow, "reason": "bad_signature"},
        )
        raise HTTPException(status_code=401, detail="Signature verification failed")


def require_user(authorization: Optional[str] = Header(None)) -> dict:
    """Resolve the Bearer session token to an account."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    user = store.get_user_by_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


def _favorite_item_key(
    tmdb_id: Optional[int], anilist_id: Optional[int], media_type: Optional[str] = None
) -> str:
    """Stable dedup key for a show-level favorite, preferring the AniList id.

    Movies get their own ``movie:`` namespace because TMDB movie and tv ids share
    one numeric space and would otherwise collide. Anime and TV keys are
    unchanged, so existing favorites need no migration."""
    # Manga gets its own namespace so the frontend can route the row to the manga
    # overview, and so it reads unambiguously next to an anime favorite.
    if media_type == "manga" and anilist_id is not None:
        return f"manga:{anilist_id}"
    if anilist_id is not None:
        return f"anilist:{anilist_id}"
    if media_type == "movie":
        return f"movie:{tmdb_id}"
    return f"tmdb:{tmdb_id}"


def _progress_item_key(
    tmdb_id: Optional[int], anilist_id: Optional[int],
    season_number: Optional[int], episode_number: Optional[int],
    media_type: Optional[str] = None, local_id: Optional[str] = None,
) -> str:
    """Stable dedup key for one episode's progress, or a whole movie.

    Movies are ``movie:{tmdb_id}`` with no season/episode, for the same
    id-collision reason as _favorite_item_key."""
    # Local media has no tmdb/anilist id, so it keys off the on-disk path token,
    # per-episode like TV. That is what gives it resume and continue-watching;
    # _dedup_by_show collapses the episodes on the shared local_id.
    if media_type == "local" and local_id:
        base = f"local:{local_id}"
        if season_number is not None:
            base += f":s{season_number}"
        if episode_number is not None:
            base += f":e{episode_number}"
        return base
    if anilist_id is None and media_type == "movie":
        return f"movie:{tmdb_id}"
    # Manga keeps one row per title, not per chapter: the chapter rides in
    # episode_number and the page in position_seconds, so the key omits both and
    # each save updates the single "where you're reading" row.
    if media_type == "manga" and anilist_id is not None:
        return f"manga:{anilist_id}"
    base = f"anilist:{anilist_id}" if anilist_id is not None else f"tmdb:{tmdb_id}"
    if season_number is not None:
        base += f":s{season_number}"
    if episode_number is not None:
        base += f":e{episode_number}"
    return base


# --- models ----------------------------------------------------------------
class ChallengeRequest(BaseModel):
    public_key: str


class ChallengeResponse(BaseModel):
    public_key: str
    challenge: str
    expires_at: str


class RegisterRequest(BaseModel):
    public_key: str
    challenge: str
    signature: str
    # Required: a freshly minted keypair must not bypass the invite gate.
    # Existing accounts log in via /auth/login and need no code.
    invite_code: str
    label: Optional[str] = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    public_key: str
    challenge: str
    signature: str


class AuthResponse(BaseModel):
    public_key: str
    label: Optional[str]
    session_token: str
    expires_at: str
    created: bool


class FavoriteIn(BaseModel):
    tmdb_id: Optional[int] = None
    anilist_id: Optional[int] = None
    season_number: Optional[int] = None
    media_type: Optional[str] = None
    title: Optional[str] = None
    poster: Optional[str] = None
    # Omitted means the default 'favorites' list, so legacy clients are unchanged.
    list_name: str = Field(default="favorites", min_length=1, max_length=100)

    @model_validator(mode="after")
    def _need_an_id(self):
        if self.tmdb_id is None and self.anilist_id is None:
            raise ValueError("Provide at least one of tmdb_id or anilist_id")
        return self


class ProgressIn(BaseModel):
    tmdb_id: Optional[int] = None
    anilist_id: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    position_seconds: Optional[float] = None
    duration_seconds: Optional[float] = None
    status: Optional[str] = None  # 'in_progress' | 'completed' (auto if omitted)
    title: Optional[str] = None
    poster: Optional[str] = None
    # 'movie' namespaces the key and routes history rows to /watch-movie; 'local'
    # is on-disk media keyed by local_id below.
    media_type: Optional[str] = None
    # On-disk path token, the identity for local rows. Ignored otherwise.
    local_id: Optional[str] = None

    @model_validator(mode="after")
    def _need_an_id(self):
        if self.tmdb_id is None and self.anilist_id is None and self.local_id is None:
            raise ValueError("Provide at least one of tmdb_id, anilist_id or local_id")
        return self


# --- auth endpoints --------------------------------------------------------
@router.post("/auth/challenge", response_model=ChallengeResponse)
@limiter.limit("20/minute")
def auth_challenge(request: Request, body: ChallengeRequest):
    """Issue a one-time challenge for a public key. The client signs the
    returned ``challenge`` string with its Ed25519 private key, then calls
    /auth/register or /auth/login."""
    pk = (body.public_key or "").lower()
    if not _HEX64.match(pk):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars")
    challenge, expires_at = store.create_challenge(pk, CHALLENGE_PURPOSE)
    return ChallengeResponse(public_key=pk, challenge=challenge, expires_at=expires_at)


@router.post("/auth/register", response_model=AuthResponse)
@limiter.limit("10/minute")
def auth_register(request: Request, body: RegisterRequest):
    """Create the account for a public key, proving possession via the signed
    challenge, and return a session. 409 if the key is already registered, 403 on
    a bad ``invite_code``.

    Ordering is deliberate so nothing one-time is wasted on a doomed attempt. The
    409 and 403 checks both run before the challenge is consumed, so a 409 leaves
    it intact for the frontend's register-then-login fallback, and a single-use
    invite is only burned once the signature has verified."""
    pk = (body.public_key or "").lower()
    if not _HEX64.match(pk):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars")

    # Before invite validation, so a register-then-login fallback for a known key
    # still 409s cleanly whatever the code says.
    if store.get_account_by_public_key(pk):
        raise HTTPException(status_code=409, detail="Account already exists; use /auth/login")

    # Validate before the one-time challenge so a bad code doesn't burn it.
    is_static = _check_invite_code(body.invite_code, request, audit.key_prefix(pk), "mnemonic_register")

    _verify_signed_challenge(pk, body.challenge, body.signature, request, "register")
    # Committed now, so burn the single-use token.
    _consume_invite_code(body.invite_code, is_static, used_by=f"mnemonic:{pk}",
                         request=request, flow="mnemonic_register")
    account = store.create_account(pk, body.label)
    token, expires_at = store.create_session(account["user_id"])
    store.touch_login(account["user_id"])
    audit.log_event(
        "register_success", outcome="success", request=request,
        user_id=account["user_id"], identity=audit.key_prefix(pk),
        detail={"method": "mnemonic"},
    )
    return AuthResponse(
        public_key=pk, label=account.get("label"),
        session_token=token, expires_at=expires_at, created=True,
    )


@router.post("/auth/login", response_model=AuthResponse)
@limiter.limit("10/minute")
def auth_login(request: Request, body: LoginRequest):
    """Log in by signing the challenge. 404 if the key isn't registered yet.

    The existence check runs before the challenge is consumed, so a 404 leaves it
    intact for the frontend's login-then-register fallback."""
    pk = (body.public_key or "").lower()
    if not _HEX64.match(pk):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars")

    # Not a security event: this is a normal step of the client's fallback for a
    # brand-new key, and a 256-bit keyspace makes key probing meaningless anyway.
    account = store.get_account_by_public_key(pk)
    if not account:
        raise HTTPException(status_code=404, detail="No account for this key; use /auth/register")

    _verify_signed_challenge(pk, body.challenge, body.signature, request, "login")
    token, expires_at = store.create_session(account["user_id"])
    store.touch_login(account["user_id"])
    audit.log_event(
        "login_success", outcome="success", request=request,
        user_id=account["user_id"], identity=audit.key_prefix(pk),
        detail={"method": "mnemonic"},
    )
    return AuthResponse(
        public_key=pk, label=account.get("label"),
        session_token=token, expires_at=expires_at, created=False,
    )


@router.post("/auth/logout")
def auth_logout(authorization: Optional[str] = Header(None)):
    """Revoke the current session token."""
    if authorization and authorization.lower().startswith("bearer "):
        store.delete_session(authorization.split(" ", 1)[1].strip())
    return {"success": True}


# --- email + password auth -------------------------------------------------
# Registration is invite-gated and requires email verification before login.
# These handlers must stay `async def`, unlike their plain-`def` siblings, so
# every blocking step (password hashing at ~0.3s, SMTP, DB calls) can go through
# run_in_threadpool and keep the event loop free.
class EmailRegisterRequest(BaseModel):
    email: str
    password: str
    invite_code: str
    label: Optional[str] = Field(default=None, max_length=100)


class EmailLoginRequest(BaseModel):
    email: str
    password: str


class EmailTokenRequest(BaseModel):
    token: str


class EmailOnlyRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


def _validate_email(email: str) -> str:
    email = _normalize_email(email)
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    return email


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or not (
        passwords.MIN_PASSWORD_LENGTH <= len(password) <= passwords.MAX_PASSWORD_LENGTH
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Password must be {passwords.MIN_PASSWORD_LENGTH} to {passwords.MAX_PASSWORD_LENGTH} characters",
        )


def _session_payload(account: dict, created: bool) -> dict:
    token, expires_at = store.create_session(account["user_id"])
    store.touch_login(account["user_id"])
    return {
        "success": True,
        "email": account.get("email"),
        "label": account.get("label"),
        "session_token": token,
        "expires_at": expires_at,
        "created": created,
    }


@router.post("/auth/email/register")
@limiter.limit("5/minute")
async def email_register(request: Request, body: EmailRegisterRequest):
    """Create an unverified email+password account and email a verification link.
    403 on a bad invite code, 409 if the email is taken. No session is issued
    until the email is verified."""
    email = _validate_email(body.email)
    _validate_password(body.password)

    # Validated here but consumed only once committed, so a 409 for a taken email
    # doesn't burn a single-use token.
    is_static = await run_in_threadpool(
        _check_invite_code, body.invite_code, request, email, "email_register"
    )

    if await run_in_threadpool(store.get_account_by_email, email):
        await run_in_threadpool(
            audit.log_event,
            "register_blocked", outcome="failure", request=request, identity=email,
            detail={"method": "email", "reason": "email_taken"},
        )
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    await run_in_threadpool(
        _consume_invite_code, body.invite_code, is_static, used_by=email,
        request=request, flow="email_register",
    )

    pw_hash = await run_in_threadpool(passwords.hash_password, body.password)
    account = await run_in_threadpool(store.create_email_account, email, pw_hash, body.label)
    await run_in_threadpool(
        audit.log_event,
        "register_success", outcome="success", request=request,
        user_id=account["user_id"], identity=email, detail={"method": "email"},
    )

    # Demo deployments have no SMTP, so skip verification and sign the user
    # straight in. The nightly reset wipes these accounts anyway.
    if Config.DEMO_MODE:
        await run_in_threadpool(store.set_email_verified, account["user_id"], True)
        account = await run_in_threadpool(store.get_account, account["user_id"])
        payload = await run_in_threadpool(_session_payload, account, created=True)
        return {**payload, "requires_verification": False}

    token = await run_in_threadpool(
        store.create_email_token, account["user_id"], "verify", VERIFY_TOKEN_TTL
    )
    await run_in_threadpool(mailer.send_verification_email, email, token)

    return {
        "success": True,
        "requires_verification": True,
        "email": email,
        "message": "Account created. Check your email to verify your account.",
    }


@router.post("/auth/email/login")
@limiter.limit("10/minute")
async def email_login(request: Request, body: EmailLoginRequest):
    """Log in with email + password, returning a session. 401 on bad credentials,
    kept generic so it is not an account-existence oracle; 403 if unverified."""
    email = _normalize_email(body.email)
    account = await run_in_threadpool(store.get_account_by_email, email)

    # Always hash, against a throwaway if need be, so response time doesn't
    # reveal whether the email exists.
    stored_hash = account.get("password_hash") if account else None
    ok = await run_in_threadpool(
        passwords.verify_password,
        body.password,
        stored_hash or "pbkdf2_sha256$1$AAAA$AAAA",
    )
    if not account or not stored_hash or not ok:
        await run_in_threadpool(
            audit.log_event,
            "login_failed", outcome="failure", request=request, identity=email,
            user_id=account["user_id"] if account else None,
            detail={"method": "email", "reason": "bad_credentials"},
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not account.get("email_verified"):
        await run_in_threadpool(
            audit.log_event,
            "login_unverified", outcome="failure", request=request, identity=email,
            user_id=account["user_id"], detail={"method": "email"},
        )
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before signing in. Check your inbox or request a new link.",
        )

    # Upgrade an out-of-date hash now that we have the plaintext.
    if passwords.needs_rehash(stored_hash):
        new_hash = await run_in_threadpool(passwords.hash_password, body.password)
        await run_in_threadpool(store.set_password, account["user_id"], new_hash)

    await run_in_threadpool(
        audit.log_event,
        "login_success", outcome="success", request=request, identity=email,
        user_id=account["user_id"], detail={"method": "email"},
    )
    return await run_in_threadpool(_session_payload, account, created=False)


@router.post("/auth/email/verify")
@limiter.limit("20/minute")
def email_verify(request: Request, body: EmailTokenRequest):
    """Consume a verification token, mark the email verified and return a session,
    so verifying lands the user straight in the app."""
    user_id = store.consume_email_token(body.token, "verify")
    if user_id is None:
        audit.log_event("verify_failed", outcome="failure", request=request)
        raise HTTPException(status_code=400, detail="This verification link is invalid or has expired")
    store.set_email_verified(user_id, True)
    account = store.get_account(user_id)
    audit.log_event(
        "email_verified", outcome="success", request=request,
        user_id=user_id, identity=account.get("email"),
    )
    return _session_payload(account, created=True)


@router.post("/auth/email/resend")
@limiter.limit("5/minute")
async def email_resend(request: Request, body: EmailOnlyRequest):
    """Resend the verification email. Always reports success so it is not an
    account-existence oracle; only sends for an existing unverified account."""
    email = _normalize_email(body.email)
    account = await run_in_threadpool(store.get_account_by_email, email)
    sent = bool(account and account.get("email") and not account.get("email_verified"))
    if sent:
        token = await run_in_threadpool(
            store.create_email_token, account["user_id"], "verify", VERIFY_TOKEN_TTL
        )
        await run_in_threadpool(mailer.send_verification_email, email, token)
    # Logged even when nothing is sent: a burst of resends for emails that don't
    # exist is exactly the probing the ledger is for. Only admins can read it, so
    # recording `sent` re-opens no oracle.
    await run_in_threadpool(
        audit.log_event,
        "verify_resend_requested", request=request, identity=email, detail={"sent": sent},
    )
    return {"success": True, "message": "If that account exists and is unverified, a new link is on its way."}


@router.post("/auth/email/forgot")
@limiter.limit("5/minute")
async def email_forgot(request: Request, body: EmailOnlyRequest):
    """Start a password reset. Always reports success so it is not an
    account-existence oracle; only sends for an existing password account."""
    email = _normalize_email(body.email)
    account = await run_in_threadpool(store.get_account_by_email, email)
    sent = bool(account and account.get("password_hash"))
    if sent:
        token = await run_in_threadpool(
            store.create_email_token, account["user_id"], "reset", RESET_TOKEN_TTL
        )
        await run_in_threadpool(mailer.send_reset_email, email, token)
    await run_in_threadpool(
        audit.log_event,
        "password_reset_requested", request=request, identity=email, detail={"sent": sent},
    )
    return {"success": True, "message": "If that account exists, a reset link is on its way."}


@router.post("/auth/email/reset")
@limiter.limit("5/minute")
async def email_reset(request: Request, body: ResetPasswordRequest):
    """Complete a password reset: consume the token, set the password and revoke
    every session. Also marks the email verified, since holding the inbox proves
    ownership."""
    _validate_password(body.password)
    user_id = await run_in_threadpool(store.consume_email_token, body.token, "reset")
    if user_id is None:
        await run_in_threadpool(
            audit.log_event, "password_reset_failed", outcome="failure", request=request
        )
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired")

    pw_hash = await run_in_threadpool(passwords.hash_password, body.password)
    await run_in_threadpool(store.set_password, user_id, pw_hash)
    await run_in_threadpool(store.set_email_verified, user_id, True)
    await run_in_threadpool(store.revoke_user_sessions, user_id)
    await run_in_threadpool(
        audit.log_event,
        "password_reset_success", outcome="success", request=request, user_id=user_id,
    )
    return {"success": True, "message": "Password updated. You can now sign in."}


# --- account info ----------------------------------------------------------
@router.get("/account/me")
def account_me(user: dict = Depends(require_user)):
    favs = store.list_favorites(user["user_id"])
    prog = store.list_progress(user["user_id"])
    return {
        "success": True,
        "user_id": user.get("user_id"),
        "public_key": user.get("public_key"),
        "email": user.get("email"),
        "email_verified": user.get("email_verified"),
        "is_admin": bool(user.get("is_admin")),
        "username": user.get("username"),
        "label": user.get("label"),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
        "favorites_count": len(favs),
        "progress_count": len(prog),
        # Empty object when never set; older clients just ignore the field.
        "preferences": store.get_preferences(user["user_id"]),
    }


# --- client preferences ----------------------------------------------------
# An open key/value bag of per-account client settings, kept generic so a new
# preference is a frontend-only change. Capped, though the client stores little.
_MAX_PREFERENCES_BYTES = 4096


@router.get("/account/preferences")
def get_preferences(user: dict = Depends(require_user)):
    """The account's stored client preferences; ``{}`` when none set."""
    return {"success": True, "preferences": store.get_preferences(user["user_id"])}


@router.put("/account/preferences")
@limiter.limit("30/minute")
async def put_preferences(request: Request, user: dict = Depends(require_user)):
    """Replace the account's client preferences with the JSON object in the body.

    Sent raw rather than multipart, mirroring the import endpoint, to keep the
    slim image dependency-free. Never affects auth, favorites or progress.
    """
    raw = await request.body()
    if len(raw) > _MAX_PREFERENCES_BYTES:
        raise HTTPException(status_code=413, detail="Preferences payload too large")
    try:
        data = json.loads(raw or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Preferences must be a JSON object")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Preferences must be a JSON object")
    saved = await run_in_threadpool(store.set_preferences, user["user_id"], data)
    return {"success": True, "preferences": saved}


# --- display name ----------------------------------------------------------
# Cosmetic name for greetings, kept separate from auth and from the preferences
# blob so the frontend can read one first-class field off /account/me.
# Non-unique by design.
MAX_USERNAME_LENGTH = 20


class UsernameIn(BaseModel):
    # Empty or null clears it, falling back to a generic greeting.
    username: Optional[str] = Field(default=None, max_length=MAX_USERNAME_LENGTH)


@router.put("/account/username")
@limiter.limit("20/minute")
def set_username(request: Request, body: UsernameIn, user: dict = Depends(require_user)):
    """Set or clear the cosmetic display name. Trimmed; an empty value clears it."""
    name = (body.username or "").strip()
    if len(name) > MAX_USERNAME_LENGTH:
        raise HTTPException(status_code=400, detail=f"Name must be at most {MAX_USERNAME_LENGTH} characters")
    store.set_username(user["user_id"], name or None)
    return {"success": True, "username": name or None}


# --- favorites / watchlists ------------------------------------------------
# 'favorites' is the default list; any other list_name is a custom watchlist,
# and a show may live in several at once.
@router.get("/account/favorites")
def get_favorites(
    user: dict = Depends(require_user),
    list_name: Optional[str] = Query(None, description="Filter to one list; omit for all lists"),
):
    items = store.list_favorites(user["user_id"], list_name)
    return {"success": True, "count": len(items), "favorites": items}


@router.get("/account/watchlists")
def get_watchlists(user: dict = Depends(require_user)):
    """Distinct list names, each with its item count."""
    lists = store.list_watchlists(user["user_id"])
    return {"success": True, "count": len(lists), "watchlists": lists}


# Exported columns in order. Internal keys (user_id, item_key) are dropped, and
# list_name leads so a CSV groups naturally when sorted on it.
_EXPORT_FIELDS = (
    "list_name", "title", "media_type", "tmdb_id", "anilist_id",
    "season_number", "poster", "added_at",
)


@router.get("/account/favorites/export")
def export_favorites(
    user: dict = Depends(require_user),
    format: str = Query("csv", pattern="^(csv|json)$", description="csv (default) or json"),
):
    """Download every watchlist as one file.

    ``csv`` is the spreadsheet-friendly default; ``json`` round-trips types and
    nulls. Either way it is one row per show, newest first, carrying its
    ``list_name`` so all lists coexist in one file. Served as an attachment.
    """
    rows = store.list_favorites(user["user_id"])  # all lists, newest first
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d")

    if format == "json":
        payload = {
            "exported_at": now.isoformat(),
            "count": len(rows),
            "watchlists": [{k: r.get(k) for k in _EXPORT_FIELDS} for r in rows],
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="crimson-watchlists-{stamp}.json"'},
        )

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k) for k in _EXPORT_FIELDS})
    # Excel reads UTF-8 reliably only with a BOM, otherwise non-ASCII titles are
    # mangled in a spreadsheet.
    body = "﻿" + buf.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="crimson-watchlists-{stamp}.csv"'},
    )


# Bounded so a client can't stream a huge body into memory. Far more than even
# a maxed-out account's export.
_MAX_IMPORT_BYTES = 5 * 1024 * 1024


def _coerce_int(val) -> Optional[int]:
    """Best-effort int from a CSV string or JSON value. CSV gives everything as
    strings, so tolerate '5', '5.0', ints and blanks."""
    if val is None:
        return None
    if isinstance(val, bool):  # guard: bool is an int subclass
        return None
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _clean_str(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _parse_export(raw: bytes) -> List[dict]:
    """Parse an uploaded file into row dicts. Accepts anything /export produces:
    our JSON, a bare JSON array, or CSV with or without the BOM. The format is
    sniffed from the first character. Raises on anything unreadable."""
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    if text[:1] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            rows = data.get("watchlists") or data.get("favorites") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        return [r for r in rows if isinstance(r, dict)]
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]


@router.post("/account/favorites/import")
@limiter.limit("6/minute")
async def import_favorites(
    request: Request,
    user: dict = Depends(require_user),
    mode: str = Query(
        "merge",
        pattern="^(merge|replace)$",
        description="merge (default) adds to your existing lists; replace clears all your lists first",
    ),
):
    """Restore watchlists from a previously exported CSV or JSON file.

    Sent as the raw request body rather than multipart, to keep the slim image
    dependency-free. Each row is upserted into its ``list_name`` (default
    'favorites') keyed by AniList id when present else TMDB id, so re-importing
    is idempotent. ``mode=replace`` wipes every list first; the default ``merge``
    adds and updates. Rows with no id, or past the account cap, are counted in
    ``skipped``.
    """
    raw = await request.body()
    if len(raw) > _MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large to import (max 5 MB)")
    try:
        rows = _parse_export(raw)
    except (json.JSONDecodeError, csv.Error, UnicodeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Couldn't read that file. Upload a Crimson watchlist CSV or JSON export",
        )

    # Coerce up front, dropping anything without an id.
    favs: List[tuple] = []
    skipped_no_id = 0
    for r in rows:
        tmdb_id = _coerce_int(r.get("tmdb_id"))
        anilist_id = _coerce_int(r.get("anilist_id"))
        if tmdb_id is None and anilist_id is None:
            skipped_no_id += 1
            continue
        list_name = (_clean_str(r.get("list_name")) or "favorites")[:100]
        favs.append((
            list_name,
            {
                "item_key": _favorite_item_key(tmdb_id, anilist_id, _clean_str(r.get("media_type"))),
                "tmdb_id": tmdb_id,
                "anilist_id": anilist_id,
                "season_number": _coerce_int(r.get("season_number")),
                "media_type": _clean_str(r.get("media_type")),
                "title": _clean_str(r.get("title")),
                "poster": _clean_str(r.get("poster")),
            },
        ))

    def _apply() -> dict:
        if mode == "replace":
            store.clear_favorites(user["user_id"])
        return store.bulk_upsert_favorites(user["user_id"], favs)

    result = await run_in_threadpool(_apply)
    skipped = skipped_no_id + result["skipped_quota"]
    return {
        "success": True,
        "mode": mode,
        "total": len(rows),
        "imported": result["imported"],
        "skipped": skipped,
        "skipped_no_id": skipped_no_id,
        "skipped_quota": result["skipped_quota"],
    }


@router.post("/account/favorites")
@limiter.limit("60/minute")
def add_favorite(request: Request, body: FavoriteIn, user: dict = Depends(require_user)):
    item_key = _favorite_item_key(body.tmdb_id, body.anilist_id, body.media_type)
    fav = {"item_key": item_key, **body.model_dump(exclude={"list_name"})}
    try:
        saved = store.upsert_favorite(user["user_id"], fav, list_name=body.list_name)
    except QuotaExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"success": True, "favorite": saved}


@router.delete("/account/favorites")
def remove_favorite(
    user: dict = Depends(require_user),
    tmdb_id: Optional[int] = Query(None),
    anilist_id: Optional[int] = Query(None),
    item_key: Optional[str] = Query(None),
    media_type: Optional[str] = Query(None, description="'movie' to target the movie namespace"),
    list_name: Optional[str] = Query(None, description="Remove from one list; omit for all lists"),
):
    """Remove a favorite by item_key, or by tmdb_id / anilist_id. With
    ``list_name`` it leaves that one list, without it every list.
    """
    if not item_key:
        if tmdb_id is None and anilist_id is None:
            raise HTTPException(status_code=400, detail="Provide item_key, tmdb_id or anilist_id")
        item_key = _favorite_item_key(tmdb_id, anilist_id, media_type)
    removed = store.remove_favorite(user["user_id"], item_key, list_name)
    if not removed:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return {"success": True, "removed": item_key}


# --- watch progress --------------------------------------------------------
def _dedup_by_show(rows: List[dict], limit: Optional[int] = None) -> List[dict]:
    """Collapse progress rows to one entry per show, preserving order.

    Rows must arrive newest-first, so the first row seen for a show is its latest
    episode. Keyed like _progress_item_key: AniList id when present, else TMDB."""
    seen: set[str] = set()
    out: List[dict] = []
    for row in rows:
        if row.get("anilist_id") is not None:
            show_key = f"anilist:{row['anilist_id']}"
        elif row.get("media_type") == "local":
            # Every episode of a local title shares its path token, so one card.
            show_key = f"local:{row.get('local_id')}"
        elif row.get("media_type") == "movie":
            show_key = f"movie:{row['tmdb_id']}"
        else:
            show_key = f"tmdb:{row['tmdb_id']}"
        if show_key in seen:
            continue
        seen.add(show_key)
        out.append(row)
        if limit is not None and len(out) >= limit:
            break
    return out


def _resolve_status(body: ProgressIn) -> str:
    """Explicit status wins; otherwise infer 'completed' near the end."""
    if body.status in ("in_progress", "completed"):
        return body.status
    if body.position_seconds and body.duration_seconds and body.duration_seconds > 0:
        if body.position_seconds / body.duration_seconds >= 0.9:
            return "completed"
    return "in_progress"


@router.get("/account/progress")
def get_progress(
    user: dict = Depends(require_user),
    status: Optional[str] = Query(None, description="Filter: in_progress | completed"),
):
    items = store.list_progress(user["user_id"], status=status)
    return {"success": True, "count": len(items), "progress": items}


@router.post("/account/progress")
@limiter.limit("60/minute")
async def upsert_progress(request: Request, body: ProgressIn, user: dict = Depends(require_user)):
    item_key = _progress_item_key(
        body.tmdb_id, body.anilist_id, body.season_number, body.episode_number,
        body.media_type, body.local_id,
    )
    payload = body.model_dump()
    payload["status"] = _resolve_status(body)
    try:
        prog = await run_in_threadpool(
            store.upsert_progress, user["user_id"], {"item_key": item_key, **payload}
        )
    except QuotaExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Pre-cache the next episode so it plays instantly when the viewer advances.
    # TV and anime only, since a movie has no next; the warmup itself skips
    # end-of-season and unaired. Never awaited, never affects this response.
    if (
        _warmup_handler
        and body.media_type not in ("movie", "manga", "local")
        and body.tmdb_id is not None
        and body.season_number is not None
        and body.episode_number is not None
    ):
        try:
            prefs = await run_in_threadpool(store.get_preferences, user["user_id"])
            # _warmup_handler calls asyncio.create_task, so it must run on the
            # event loop, not the threadpool. That is why this handler alone
            # stays `async def`.
            _warmup_handler(
                request,
                tmdb_id=body.tmdb_id,
                season_number=body.season_number,
                episode_number=body.episode_number,
                preferences=prefs,
            )
        except Exception as e:
            logger.warning(f"warmup scheduling failed: {e}")

    return {"success": True, "progress": prog}


@router.get("/account/continue-watching")
async def continue_watching(user: dict = Depends(require_user)):
    """In-progress shows, most recent first, for the Continue Watching row.

    Collapsed to one entry per show at its latest in-progress episode."""
    rows = await run_in_threadpool(store.list_progress, user["user_id"], status="in_progress")
    items = await _enrich(_dedup_by_show(rows))
    return {"success": True, "count": len(items), "items": items}


@router.get("/account/recent")
async def recent(
    user: dict = Depends(require_user),
    limit: int = Query(20, ge=1, le=100, description="Max items to return"),
):
    """Recently watched shows of any status, most recent first, for the History row.

    Collapsed to one entry per show at its latest episode. Unlike
    /account/continue-watching this keeps completed rows, so history stays
    populated after a series is finished."""
    rows = await run_in_threadpool(store.list_progress, user["user_id"])
    items = await _enrich(_dedup_by_show(rows, limit=limit))
    return {"success": True, "count": len(items), "items": items}


@router.delete("/account/progress")
def remove_progress(
    user: dict = Depends(require_user),
    item_key: Optional[str] = Query(None),
    tmdb_id: Optional[int] = Query(None),
    anilist_id: Optional[int] = Query(None),
    season_number: Optional[int] = Query(None),
    episode_number: Optional[int] = Query(None),
    media_type: Optional[str] = Query(None, description="'movie'/'local' to target that namespace"),
    local_id: Optional[str] = Query(None, description="on-disk title token (media_type='local')"),
):
    if not item_key:
        if tmdb_id is None and anilist_id is None and local_id is None:
            raise HTTPException(status_code=400, detail="Provide item_key, or tmdb_id/anilist_id/local_id (+season/episode)")
        item_key = _progress_item_key(tmdb_id, anilist_id, season_number, episode_number, media_type, local_id)
    removed = store.remove_progress(user["user_id"], item_key)
    if not removed:
        raise HTTPException(status_code=404, detail="Progress entry not found")
    return {"success": True, "removed": item_key}

"""
Account API — mnemonic (Ed25519) sign-in + favorites + watch progress.

Auth model (P-Stream style, no username/password):

  * The client generates a 12-word BIP39 mnemonic, derives an Ed25519 keypair
    from it (seed -> seed[:32] -> keypair, see account_engine.ed25519), and that
    **public key is the account**. The mnemonic / private key never leave the
    client.
  * To prove identity the client signs a one-time server challenge; the server
    only ever *verifies* the signature against the public key. A DB leak exposes
    no credential.

Flow:

    POST /auth/challenge {public_key}                 -> {challenge}
    # client signs the challenge string with its Ed25519 private key
    POST /auth/register  {public_key, challenge, signature, label?}  -> session
    POST /auth/login     {public_key, challenge, signature}          -> session
    # authenticated requests:  Authorization: Bearer <session_token>
    GET/POST/DELETE /account/favorites
    GET/POST/DELETE /account/progress
    GET /account/continue-watching, GET /account/recent

Favorites are show-level; watch progress is per-episode. Both are stored as
plain structured rows (so the backend can serve "continue watching" etc.).
"""

import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from . import ed25519, mailer, passwords
from .db import AccountStore, QuotaExceeded, VERIFY_TOKEN_TTL, RESET_TOKEN_TTL
from rate_limit import limiter

router = APIRouter(tags=["account"])
store = AccountStore()

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")   # 32-byte public key
_HEX128 = re.compile(r"^[0-9a-fA-F]{128}$")  # 64-byte signature
# Pragmatic email shape check (we deliberately avoid the email-validator dep on
# the slim image). Good enough to reject obvious garbage; deliverability is
# proven by the verification link, not by this regex.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHALLENGE_PURPOSE = "auth"


def _allowed_invite_codes() -> set:
    """Invite codes that may register an email account, from SIGNUP_INVITE_CODE
    (comma-separated). Empty/unset => registration is closed (no code matches),
    which fails safe for a 'login required' site."""
    raw = os.getenv("SIGNUP_INVITE_CODE", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# --- helpers ---------------------------------------------------------------
def _verify_signed_challenge(public_key: str, challenge: str, signature: str) -> None:
    """Consume the one-time challenge and verify the Ed25519 signature over it.
    Raises HTTPException(401) on any failure."""
    if not _HEX64.match(public_key or ""):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars (32-byte Ed25519 key)")
    if not _HEX128.match(signature or ""):
        raise HTTPException(status_code=400, detail="signature must be 128 hex chars (64-byte Ed25519 signature)")

    public_key = public_key.lower()
    # Single-use: consume first so a leaked/failed attempt can't be replayed.
    if not store.consume_challenge(challenge, public_key, CHALLENGE_PURPOSE):
        raise HTTPException(status_code=401, detail="Invalid or expired challenge")

    ok = ed25519.verify(
        bytes.fromhex(public_key),
        challenge.encode("utf-8"),
        bytes.fromhex(signature),
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Signature verification failed")


def require_user(authorization: Optional[str] = Header(None)) -> dict:
    """FastAPI dependency: resolve the Bearer session token to an account."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    user = store.get_user_by_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


def _favorite_item_key(tmdb_id: Optional[int], anilist_id: Optional[int]) -> str:
    """Stable dedup key for a show-level favorite (AniList id preferred)."""
    if anilist_id is not None:
        return f"anilist:{anilist_id}"
    return f"tmdb:{tmdb_id}"


def _progress_item_key(
    tmdb_id: Optional[int], anilist_id: Optional[int],
    season_number: Optional[int], episode_number: Optional[int],
) -> str:
    """Stable dedup key for a single episode's progress."""
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

    @model_validator(mode="after")
    def _need_an_id(self):
        if self.tmdb_id is None and self.anilist_id is None:
            raise ValueError("Provide at least one of tmdb_id or anilist_id")
        return self


# --- auth endpoints --------------------------------------------------------
@router.post("/auth/challenge", response_model=ChallengeResponse)
@limiter.limit("20/minute")
async def auth_challenge(request: Request, body: ChallengeRequest):
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
async def auth_register(request: Request, body: RegisterRequest):
    """Create the account for a public key (proving key possession via the
    signed challenge) and return a session. 409 if the key is already
    registered — use /auth/login instead.

    The account-exists check runs BEFORE the (one-time) challenge is consumed,
    so a 409 leaves the challenge intact for a /auth/login retry with the same
    challenge — supporting a frontend that tries register then falls back to
    login (and vice-versa) on a single challenge."""
    pk = (body.public_key or "").lower()
    if not _HEX64.match(pk):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars")

    if store.get_account_by_public_key(pk):
        raise HTTPException(status_code=409, detail="Account already exists; use /auth/login")

    _verify_signed_challenge(pk, body.challenge, body.signature)
    account = store.create_account(pk, body.label)
    token, expires_at = store.create_session(account["user_id"])
    store.touch_login(account["user_id"])
    return AuthResponse(
        public_key=pk, label=account.get("label"),
        session_token=token, expires_at=expires_at, created=True,
    )


@router.post("/auth/login", response_model=AuthResponse)
@limiter.limit("10/minute")
async def auth_login(request: Request, body: LoginRequest):
    """Log in to an existing account by signing the challenge. 404 if the public
    key isn't registered yet — use /auth/register.

    The account-exists check runs BEFORE the (one-time) challenge is consumed,
    so a 404 leaves the challenge intact for a /auth/register fallback with the
    same challenge (the common 'link identity' frontend flow: try login, then
    register the new key)."""
    pk = (body.public_key or "").lower()
    if not _HEX64.match(pk):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars")

    account = store.get_account_by_public_key(pk)
    if not account:
        raise HTTPException(status_code=404, detail="No account for this key; use /auth/register")

    _verify_signed_challenge(pk, body.challenge, body.signature)
    token, expires_at = store.create_session(account["user_id"])
    store.touch_login(account["user_id"])
    return AuthResponse(
        public_key=pk, label=account.get("label"),
        session_token=token, expires_at=expires_at, created=False,
    )


@router.post("/auth/logout")
async def auth_logout(authorization: Optional[str] = Header(None)):
    """Revoke the current session token."""
    if authorization and authorization.lower().startswith("bearer "):
        store.delete_session(authorization.split(" ", 1)[1].strip())
    return {"success": True}


# --- email + password auth -------------------------------------------------
# Added alongside the mnemonic/Ed25519 flow above. Registration is gated by an
# invite code (SIGNUP_INVITE_CODE) and requires email verification before login,
# so the site stays closed to strangers. Password hashing (PBKDF2, ~0.3s) and
# SMTP sending both run in a threadpool so the event loop is never blocked.
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
            detail=f"Password must be {passwords.MIN_PASSWORD_LENGTH}–{passwords.MAX_PASSWORD_LENGTH} characters",
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
    """Create an email+password account (invite-gated, unverified) and email a
    verification link. Returns 403 on a bad invite code, 409 if the email is
    taken. No session is issued until the email is verified."""
    email = _validate_email(body.email)
    _validate_password(body.password)

    allowed = _allowed_invite_codes()
    if not allowed or body.invite_code.strip() not in allowed:
        raise HTTPException(status_code=403, detail="Invalid invite code")

    if store.get_account_by_email(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    pw_hash = await run_in_threadpool(passwords.hash_password, body.password)
    account = store.create_email_account(email, pw_hash, body.label)

    token = store.create_email_token(account["user_id"], "verify", VERIFY_TOKEN_TTL)
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
    """Log in with email + password. Returns a session on success. 401 on bad
    credentials (deliberately generic, no account-existence oracle); 403 if the
    email isn't verified yet."""
    email = _normalize_email(body.email)
    account = store.get_account_by_email(email)

    # Always run a hash comparison (against the stored hash, or a throwaway) so
    # the response time doesn't reveal whether the email exists.
    stored_hash = account.get("password_hash") if account else None
    ok = await run_in_threadpool(
        passwords.verify_password,
        body.password,
        stored_hash or "pbkdf2_sha256$1$AAAA$AAAA",
    )
    if not account or not stored_hash or not ok:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not account.get("email_verified"):
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before signing in. Check your inbox or request a new link.",
        )

    # Transparently upgrade an out-of-date hash now that we have the plaintext.
    if passwords.needs_rehash(stored_hash):
        new_hash = await run_in_threadpool(passwords.hash_password, body.password)
        store.set_password(account["user_id"], new_hash)

    return _session_payload(account, created=False)


@router.post("/auth/email/verify")
@limiter.limit("20/minute")
async def email_verify(request: Request, body: EmailTokenRequest):
    """Consume a verification token, mark the email verified, and sign the user
    in (returns a session) so verifying lands them straight in the app."""
    user_id = store.consume_email_token(body.token, "verify")
    if user_id is None:
        raise HTTPException(status_code=400, detail="This verification link is invalid or has expired")
    store.set_email_verified(user_id, True)
    account = store.get_account(user_id)
    return _session_payload(account, created=True)


@router.post("/auth/email/resend")
@limiter.limit("5/minute")
async def email_resend(request: Request, body: EmailOnlyRequest):
    """Resend the verification email. Always returns success (no account-exists
    oracle); only actually sends for an existing, still-unverified account."""
    email = _normalize_email(body.email)
    account = store.get_account_by_email(email)
    if account and account.get("email") and not account.get("email_verified"):
        token = store.create_email_token(account["user_id"], "verify", VERIFY_TOKEN_TTL)
        await run_in_threadpool(mailer.send_verification_email, email, token)
    return {"success": True, "message": "If that account exists and is unverified, a new link is on its way."}


@router.post("/auth/email/forgot")
@limiter.limit("5/minute")
async def email_forgot(request: Request, body: EmailOnlyRequest):
    """Start a password reset. Always returns success (no account-exists oracle);
    only sends for an existing email+password account."""
    email = _normalize_email(body.email)
    account = store.get_account_by_email(email)
    if account and account.get("password_hash"):
        token = store.create_email_token(account["user_id"], "reset", RESET_TOKEN_TTL)
        await run_in_threadpool(mailer.send_reset_email, email, token)
    return {"success": True, "message": "If that account exists, a reset link is on its way."}


@router.post("/auth/email/reset")
@limiter.limit("5/minute")
async def email_reset(request: Request, body: ResetPasswordRequest):
    """Complete a password reset: consume the token, set the new password, revoke
    all existing sessions, and (since controlling the inbox proves ownership)
    mark the email verified."""
    _validate_password(body.password)
    user_id = store.consume_email_token(body.token, "reset")
    if user_id is None:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired")

    pw_hash = await run_in_threadpool(passwords.hash_password, body.password)
    store.set_password(user_id, pw_hash)
    store.set_email_verified(user_id, True)
    store.revoke_user_sessions(user_id)
    return {"success": True, "message": "Password updated. You can now sign in."}


# --- account info ----------------------------------------------------------
@router.get("/account/me")
async def account_me(user: dict = Depends(require_user)):
    favs = store.list_favorites(user["user_id"])
    prog = store.list_progress(user["user_id"])
    return {
        "success": True,
        "public_key": user.get("public_key"),
        "email": user.get("email"),
        "email_verified": user.get("email_verified"),
        "label": user.get("label"),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
        "favorites_count": len(favs),
        "progress_count": len(prog),
    }


# --- favorites -------------------------------------------------------------
@router.get("/account/favorites")
async def get_favorites(user: dict = Depends(require_user)):
    items = store.list_favorites(user["user_id"])
    return {"success": True, "count": len(items), "favorites": items}


@router.post("/account/favorites")
@limiter.limit("60/minute")
async def add_favorite(request: Request, body: FavoriteIn, user: dict = Depends(require_user)):
    item_key = _favorite_item_key(body.tmdb_id, body.anilist_id)
    try:
        fav = store.upsert_favorite(user["user_id"], {"item_key": item_key, **body.model_dump()})
    except QuotaExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"success": True, "favorite": fav}


@router.delete("/account/favorites")
async def remove_favorite(
    user: dict = Depends(require_user),
    tmdb_id: Optional[int] = Query(None),
    anilist_id: Optional[int] = Query(None),
    item_key: Optional[str] = Query(None),
):
    """Remove a favorite by item_key, or by tmdb_id / anilist_id."""
    if not item_key:
        if tmdb_id is None and anilist_id is None:
            raise HTTPException(status_code=400, detail="Provide item_key, tmdb_id or anilist_id")
        item_key = _favorite_item_key(tmdb_id, anilist_id)
    removed = store.remove_favorite(user["user_id"], item_key)
    if not removed:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return {"success": True, "removed": item_key}


# --- watch progress --------------------------------------------------------
def _dedup_by_show(rows: List[dict], limit: Optional[int] = None) -> List[dict]:
    """Collapse progress rows to one entry per show, preserving order.

    Rows are expected newest-first, so the first row seen for a show is its most
    recent episode (carrying that episode's season/episode + progress). Keyed by
    AniList id when present, else TMDB id — matching _progress_item_key."""
    seen: set[str] = set()
    out: List[dict] = []
    for row in rows:
        show_key = (
            f"anilist:{row['anilist_id']}" if row.get("anilist_id") is not None
            else f"tmdb:{row['tmdb_id']}"
        )
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
async def get_progress(
    user: dict = Depends(require_user),
    status: Optional[str] = Query(None, description="Filter: in_progress | completed"),
):
    items = store.list_progress(user["user_id"], status=status)
    return {"success": True, "count": len(items), "progress": items}


@router.post("/account/progress")
@limiter.limit("60/minute")
async def upsert_progress(request: Request, body: ProgressIn, user: dict = Depends(require_user)):
    item_key = _progress_item_key(
        body.tmdb_id, body.anilist_id, body.season_number, body.episode_number
    )
    payload = body.model_dump()
    payload["status"] = _resolve_status(body)
    try:
        prog = store.upsert_progress(user["user_id"], {"item_key": item_key, **payload})
    except QuotaExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"success": True, "progress": prog}


@router.get("/account/continue-watching")
async def continue_watching(user: dict = Depends(require_user)):
    """In-progress shows, most-recently-watched first — for a 'Continue Watching'
    row on the frontend.

    Collapsed to one entry per show (latest in-progress episode), so a series
    you're partway through several episodes of appears once."""
    items = _dedup_by_show(store.list_progress(user["user_id"], status="in_progress"))
    return {"success": True, "count": len(items), "items": items}


@router.get("/account/recent")
async def recent(
    user: dict = Depends(require_user),
    limit: int = Query(20, ge=1, le=100, description="Max items to return"),
):
    """Recently-watched shows of *any* status (in_progress + completed),
    most-recently-watched first — for a 'Recent' / 'History' row on the frontend.

    Collapsed to one entry per show: a viewer who watched several episodes of the
    same series shows up once, carrying that show's most recent episode (and its
    progress). Rows are newest-first, so the first time we see a show is its
    latest episode. Unlike /account/continue-watching (which is in_progress only),
    this keeps finished episodes so the history stays populated after completion."""
    items = _dedup_by_show(store.list_progress(user["user_id"]), limit=limit)
    return {"success": True, "count": len(items), "items": items}


@router.delete("/account/progress")
async def remove_progress(
    user: dict = Depends(require_user),
    item_key: Optional[str] = Query(None),
    tmdb_id: Optional[int] = Query(None),
    anilist_id: Optional[int] = Query(None),
    season_number: Optional[int] = Query(None),
    episode_number: Optional[int] = Query(None),
):
    if not item_key:
        if tmdb_id is None and anilist_id is None:
            raise HTTPException(status_code=400, detail="Provide item_key, or tmdb_id/anilist_id (+season/episode)")
        item_key = _progress_item_key(tmdb_id, anilist_id, season_number, episode_number)
    removed = store.remove_progress(user["user_id"], item_key)
    if not removed:
        raise HTTPException(status_code=404, detail="Progress entry not found")
    return {"success": True, "removed": item_key}

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
    POST /auth/register  {public_key, challenge, signature, invite_code, label?}  -> session
    POST /auth/login     {public_key, challenge, signature}          -> session
    # authenticated requests:  Authorization: Bearer <session_token>
    GET/POST/DELETE /account/favorites   (?list_name=... selects a watchlist)
    GET /account/watchlists
    GET/POST/DELETE /account/progress
    GET /account/continue-watching, GET /account/recent

Favorites are show-level; watch progress is per-episode. Both are stored as
plain structured rows (so the backend can serve "continue watching" etc.).
A favorite belongs to a named list (``list_name``, default 'favorites'); custom
lists are watchlists like 'Todo'/'Done'/'Paused' and a show may be in several.
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

from . import ed25519, mailer, passwords
from .db import AccountStore, QuotaExceeded, VERIFY_TOKEN_TTL, RESET_TOKEN_TTL
from rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])
store = AccountStore()

# Optional hook injected by api.py at startup (see set_episode_enricher). Given the
# deduped per-show progress rows, it annotates each with "next episode" hints
# (season_episode_count / next_episode_exists / next_episode_air_date) from TMDB,
# so the frontend never points at a non-existent or not-yet-aired next episode.
# Kept as injection to avoid importing the heavy api module here (circular import).
_episode_enricher = None  # async callable(rows: List[dict]) -> None  (mutates in place)


def set_episode_enricher(handler) -> None:
    """Register the progress-row enricher (called by api.py once it's defined)."""
    global _episode_enricher
    _episode_enricher = handler


async def _enrich(rows: List[dict]) -> List[dict]:
    """Run the injected enricher over rows (best-effort; rows returned unchanged if
    it isn't set or raises). Enrichment is purely additive metadata, never load-bearing."""
    if _episode_enricher and rows:
        try:
            await _episode_enricher(rows)
        except Exception as e:
            logger.warning(f"progress enrichment failed: {e}")
    return rows

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


def _check_invite_code(code: str) -> bool:
    """Validate an invite code for NEW-account creation, shared by both the email
    and mnemonic signup flows. Two kinds of invite are accepted in the same field:

      * a shared, reusable code from SIGNUP_INVITE_CODE, or
      * a single-use token minted by the Discord bot (see discord_bot/), which can
        register exactly one account.

    Returns True if the code is a shared static code, False if it's an available
    single-use token; raises HTTPException(403) if it's neither. This does NOT
    consume a single-use token — burn that with _consume_invite_code only once
    you're committed to creating the account, so a later 409/validation failure
    doesn't waste it."""
    code = (code or "").strip()
    static_codes = _allowed_invite_codes()
    is_static = bool(static_codes) and code in static_codes
    if not is_static and not store.invite_token_is_available(code):
        raise HTTPException(status_code=403, detail="Invalid invite code")
    return is_static


def _consume_invite_code(code: str, is_static: bool, used_by: str) -> None:
    """Burn a single-use invite token now that we're committed to creating the
    account. No-op for a shared static code. Race-safe via consume_invite_token:
    if a concurrent signup consumed the token in the gap since _check_invite_code,
    this fails closed with 403."""
    if is_static:
        return
    if not store.consume_invite_token((code or "").strip(), used_by=used_by):
        raise HTTPException(status_code=403, detail="This invite code has already been used")


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


def _favorite_item_key(
    tmdb_id: Optional[int], anilist_id: Optional[int], media_type: Optional[str] = None
) -> str:
    """Stable dedup key for a show-level favorite (AniList id preferred).

    A general MOVIE gets its own ``movie:{tmdb_id}`` namespace: TMDB *movie* and
    *tv* ids share the same numeric space, so a movie and a TV show could collide
    on ``tmdb:{id}`` otherwise. Anime (anilist) and TV/show keys are unchanged, so
    existing favorites keep their exact keys (no migration)."""
    if anilist_id is not None:
        return f"anilist:{anilist_id}"
    if media_type == "movie":
        return f"movie:{tmdb_id}"
    return f"tmdb:{tmdb_id}"


def _progress_item_key(
    tmdb_id: Optional[int], anilist_id: Optional[int],
    season_number: Optional[int], episode_number: Optional[int],
    media_type: Optional[str] = None,
) -> str:
    """Stable dedup key for a single episode's progress (or a whole movie).

    Movies are namespaced ``movie:{tmdb_id}`` (no season/episode — a movie is one
    item) for the same id-collision reason as _favorite_item_key. TV/anime keys are
    byte-identical to before."""
    if anilist_id is None and media_type == "movie":
        return f"movie:{tmdb_id}"
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
    # Required: creating a NEW mnemonic account is invite-gated exactly like email
    # signup, so a freshly minted keypair can't bypass the invite system. Existing
    # mnemonic accounts log in via /auth/login and need no code.
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
    # Which list this belongs to. Omitted -> the default 'favorites' list, so
    # legacy clients keep their single-tab behaviour. Any other name is a custom
    # watchlist (e.g. 'Todo', 'Done', 'Paused').
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
    # 'movie' namespaces the progress key (and lets the frontend route history rows
    # back to /watch-movie). Optional, so existing TV/anime clients are unaffected.
    media_type: Optional[str] = None

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

    Creating a NEW mnemonic account is invite-gated (``invite_code``) exactly like
    email signup, so a freshly minted client-side keypair can't bypass the
    invite-only site. Existing mnemonic accounts are unaffected — they sign in via
    /auth/login and need no code.

    Ordering is deliberate so nothing one-time is wasted on a doomed attempt:
    the account-exists check (409) and invite-code validity check (403) both run
    BEFORE the (one-time) challenge is consumed — so a 409 leaves the challenge
    intact for a /auth/login retry with the same challenge (the frontend tries
    register then falls back to login on a single challenge) — and the single-use
    invite token is only burned once the signed challenge has verified."""
    pk = (body.public_key or "").lower()
    if not _HEX64.match(pk):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars")

    # Existence check first (before invite validation) so a register→login
    # fallback for an already-registered key still 409s cleanly regardless of code.
    if store.get_account_by_public_key(pk):
        raise HTTPException(status_code=409, detail="Account already exists; use /auth/login")

    # Validate the invite (does not consume single-use tokens yet) before the
    # one-time challenge, so a bad code doesn't burn the challenge.
    is_static = _check_invite_code(body.invite_code)

    _verify_signed_challenge(pk, body.challenge, body.signature)
    # Committed now: burn the single-use token (race-safe; no-op for static codes).
    _consume_invite_code(body.invite_code, is_static, used_by=f"mnemonic:{pk}")
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

    # Invite-gated (shared static code OR single-use Discord-bot token); validated
    # here but only *consumed* once we're committed to creating the account, so a
    # 409 (email taken) doesn't burn a single-use token. See _check_invite_code.
    is_static = _check_invite_code(body.invite_code)

    if store.get_account_by_email(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    _consume_invite_code(body.invite_code, is_static, used_by=email)

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
        "user_id": user.get("user_id"),
        "public_key": user.get("public_key"),
        "email": user.get("email"),
        "email_verified": user.get("email_verified"),
        "is_admin": bool(user.get("is_admin")),
        "label": user.get("label"),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
        "favorites_count": len(favs),
        "progress_count": len(prog),
    }


# --- favorites / watchlists ------------------------------------------------
# The default list is 'favorites' (original single-tab behaviour); any other
# list_name is a custom watchlist. A show may live in several lists at once.
@router.get("/account/favorites")
async def get_favorites(
    user: dict = Depends(require_user),
    list_name: Optional[str] = Query(None, description="Filter to one list; omit for all lists"),
):
    items = store.list_favorites(user["user_id"], list_name)
    return {"success": True, "count": len(items), "favorites": items}


@router.get("/account/watchlists")
async def get_watchlists(user: dict = Depends(require_user)):
    """The user's distinct list names, each with its item count."""
    lists = store.list_watchlists(user["user_id"])
    return {"success": True, "count": len(lists), "watchlists": lists}


# Columns exported per show, in order. These are the human-meaningful fields of a
# favorite row — internal keys (id, user_id, item_key) are intentionally dropped.
# Order puts the list first so a CSV groups naturally when sorted on that column.
_EXPORT_FIELDS = (
    "list_name", "title", "media_type", "tmdb_id", "anilist_id",
    "season_number", "poster", "added_at",
)


@router.get("/account/favorites/export")
async def export_favorites(
    user: dict = Depends(require_user),
    format: str = Query("csv", pattern="^(csv|json)$", description="csv (default) or json"),
):
    """Download every watchlist (all lists at once) as a single file.

    ``csv`` is the spreadsheet-friendly default; ``json`` is a round-trippable
    backup that preserves types/nulls. Either way it's one row/object per show,
    newest-first, carrying the list it belongs to in ``list_name`` so all lists
    coexist in one file. Served as an attachment so the browser saves it.
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
    # Excel reads UTF-8 reliably only with a BOM; prepend one so non-ASCII titles
    # (e.g. Japanese) aren't mangled when the file is opened in a spreadsheet.
    body = "﻿" + buf.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="crimson-watchlists-{stamp}.csv"'},
    )


# Upper bound on an uploaded export so a malicious client can't stream a huge
# body into memory. 5 MiB is far more than even a maxed-out account's export.
_MAX_IMPORT_BYTES = 5 * 1024 * 1024


def _coerce_int(val) -> Optional[int]:
    """Best-effort int from a CSV string / JSON value. CSV gives everything as
    strings (and empty cells as ''); tolerate '5', '5.0', ints, and blanks."""
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
    """Parse an uploaded file back into a list of row dicts. Accepts what /export
    produces: our JSON ({"watchlists": [...]}), a bare JSON array, or our CSV
    (with or without the UTF-8 BOM). Format is sniffed from the content (JSON
    starts with '{' or '['; anything else is CSV). Raises on anything unreadable."""
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
    """Restore watchlists from a previously-exported CSV or JSON file.

    The file is sent as the raw request body (no multipart — keeps the slim image
    dependency-free); its format is sniffed from the content. Round-trips the
    /export output: each row is upserted into the list named in its ``list_name``
    column (defaulting to 'favorites'), keyed by AniList id when present else TMDB
    id, so re-importing is idempotent. ``mode=replace`` wipes every existing list
    first (a clean restore); the default ``merge`` keeps what's there and
    adds/updates. Rows without any id are skipped; rows past the account cap are
    reported in ``skipped``.
    """
    raw = await request.body()
    if len(raw) > _MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large to import (max 5 MB)")
    try:
        rows = _parse_export(raw)
    except (json.JSONDecodeError, csv.Error, UnicodeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Couldn't read that file — upload a Crimson watchlist CSV or JSON export",
        )

    # Coerce rows into favorite dicts up front, dropping anything without an id.
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
async def add_favorite(request: Request, body: FavoriteIn, user: dict = Depends(require_user)):
    item_key = _favorite_item_key(body.tmdb_id, body.anilist_id, body.media_type)
    fav = {"item_key": item_key, **body.model_dump(exclude={"list_name"})}
    try:
        saved = store.upsert_favorite(user["user_id"], fav, list_name=body.list_name)
    except QuotaExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"success": True, "favorite": saved}


@router.delete("/account/favorites")
async def remove_favorite(
    user: dict = Depends(require_user),
    tmdb_id: Optional[int] = Query(None),
    anilist_id: Optional[int] = Query(None),
    item_key: Optional[str] = Query(None),
    media_type: Optional[str] = Query(None, description="'movie' to target the movie namespace"),
    list_name: Optional[str] = Query(None, description="Remove from one list; omit for all lists"),
):
    """Remove a favorite by item_key, or by tmdb_id / anilist_id.

    With ``list_name`` the show is removed from that list only; without it the
    show is removed from every list it belongs to.
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

    Rows are expected newest-first, so the first row seen for a show is its most
    recent episode (carrying that episode's season/episode + progress). Keyed by
    AniList id when present, else TMDB id — matching _progress_item_key."""
    seen: set[str] = set()
    out: List[dict] = []
    for row in rows:
        if row.get("anilist_id") is not None:
            show_key = f"anilist:{row['anilist_id']}"
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
        body.tmdb_id, body.anilist_id, body.season_number, body.episode_number,
        body.media_type,
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
    items = await _enrich(_dedup_by_show(store.list_progress(user["user_id"], status="in_progress")))
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
    items = await _enrich(_dedup_by_show(store.list_progress(user["user_id"]), limit=limit))
    return {"success": True, "count": len(items), "items": items}


@router.delete("/account/progress")
async def remove_progress(
    user: dict = Depends(require_user),
    item_key: Optional[str] = Query(None),
    tmdb_id: Optional[int] = Query(None),
    anilist_id: Optional[int] = Query(None),
    season_number: Optional[int] = Query(None),
    episode_number: Optional[int] = Query(None),
    media_type: Optional[str] = Query(None, description="'movie' to target the movie namespace"),
):
    if not item_key:
        if tmdb_id is None and anilist_id is None:
            raise HTTPException(status_code=400, detail="Provide item_key, or tmdb_id/anilist_id (+season/episode)")
        item_key = _progress_item_key(tmdb_id, anilist_id, season_number, episode_number, media_type)
    removed = store.remove_progress(user["user_id"], item_key)
    if not removed:
        raise HTTPException(status_code=404, detail="Progress entry not found")
    return {"success": True, "removed": item_key}

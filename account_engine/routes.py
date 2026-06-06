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
    GET/POST/DELETE /account/progress, GET /account/continue-watching

Favorites are show-level; watch progress is per-episode. Both are stored as
plain structured rows (so the backend can serve "continue watching" etc.).
"""

import re
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from . import ed25519
from .db import AccountStore

router = APIRouter(tags=["account"])
store = AccountStore()

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")   # 32-byte public key
_HEX128 = re.compile(r"^[0-9a-fA-F]{128}$")  # 64-byte signature
CHALLENGE_PURPOSE = "auth"


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
async def auth_challenge(body: ChallengeRequest):
    """Issue a one-time challenge for a public key. The client signs the
    returned ``challenge`` string with its Ed25519 private key, then calls
    /auth/register or /auth/login."""
    pk = (body.public_key or "").lower()
    if not _HEX64.match(pk):
        raise HTTPException(status_code=400, detail="public_key must be 64 hex chars")
    challenge, expires_at = store.create_challenge(pk, CHALLENGE_PURPOSE)
    return ChallengeResponse(public_key=pk, challenge=challenge, expires_at=expires_at)


@router.post("/auth/register", response_model=AuthResponse)
async def auth_register(body: RegisterRequest):
    """Create the account for a public key (proving key possession via the
    signed challenge) and return a session. 409 if the key is already
    registered — use /auth/login instead."""
    _verify_signed_challenge(body.public_key, body.challenge, body.signature)
    pk = body.public_key.lower()

    if store.get_account_by_public_key(pk):
        raise HTTPException(status_code=409, detail="Account already exists; use /auth/login")

    account = store.create_account(pk, body.label)
    token, expires_at = store.create_session(account["user_id"])
    store.touch_login(account["user_id"])
    return AuthResponse(
        public_key=pk, label=account.get("label"),
        session_token=token, expires_at=expires_at, created=True,
    )


@router.post("/auth/login", response_model=AuthResponse)
async def auth_login(body: LoginRequest):
    """Log in to an existing account by signing the challenge. 404 if the public
    key isn't registered yet — use /auth/register."""
    _verify_signed_challenge(body.public_key, body.challenge, body.signature)
    pk = body.public_key.lower()

    account = store.get_account_by_public_key(pk)
    if not account:
        raise HTTPException(status_code=404, detail="No account for this key; use /auth/register")

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


# --- account info ----------------------------------------------------------
@router.get("/account/me")
async def account_me(user: dict = Depends(require_user)):
    favs = store.list_favorites(user["user_id"])
    prog = store.list_progress(user["user_id"])
    return {
        "success": True,
        "public_key": user["public_key"],
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
async def add_favorite(body: FavoriteIn, user: dict = Depends(require_user)):
    item_key = _favorite_item_key(body.tmdb_id, body.anilist_id)
    fav = store.upsert_favorite(user["user_id"], {"item_key": item_key, **body.model_dump()})
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
async def upsert_progress(body: ProgressIn, user: dict = Depends(require_user)):
    item_key = _progress_item_key(
        body.tmdb_id, body.anilist_id, body.season_number, body.episode_number
    )
    payload = body.model_dump()
    payload["status"] = _resolve_status(body)
    prog = store.upsert_progress(user["user_id"], {"item_key": item_key, **payload})
    return {"success": True, "progress": prog}


@router.get("/account/continue-watching")
async def continue_watching(user: dict = Depends(require_user)):
    """In-progress episodes, most-recently-watched first — for a
    'Continue Watching' row on the frontend."""
    items = store.list_progress(user["user_id"], status="in_progress")
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

"""Keys for the movie-web fork, injected server-side by its proxy so a key never
reaches a browser. The login wall accepts them on /mw paths only."""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from account_engine.audit import admin_identity, log_admin_action
from account_engine.deps import require_admin
from core.rate_limit import limiter

from .db import store

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class ApiKeyCreate(BaseModel):
    label: Optional[str] = Field(
        None, max_length=100, description="A note to identify this key, e.g. 'movie-web prod'"
    )


@router.get("/api-keys")
async def list_api_keys(include_revoked: bool = Query(True)):
    """The raw secret is shown once at creation and never here; ``id`` is the
    handle for revocation."""
    items = await asyncio.to_thread(store.list_keys, include_revoked)
    return {"success": True, "count": len(items), "keys": items}


@router.post("/api-keys")
@limiter.limit("30/minute")
async def create_api_key(request: Request, body: ApiKeyCreate, user: dict = Depends(require_admin)):
    """Only the hash is stored, so the raw key is returned exactly once: it can
    be revoked and replaced later, never retrieved."""
    raw, info = await asyncio.to_thread(store.create_key, body.label or None, admin_identity(user))
    log_admin_action(request, user, "api_key_created", label=body.label)
    return {"success": True, "key": raw, "info": info}


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(request: Request, key_id: str, user: dict = Depends(require_admin)):
    """Takes effect within the login wall's cache window, about a minute."""
    if not await asyncio.to_thread(store.revoke_key, key_id):
        raise HTTPException(status_code=404, detail="Unknown or already-revoked API key")
    log_admin_action(request, user, "api_key_revoked", key_id=key_id)
    return {"success": True, "revoked": key_id}

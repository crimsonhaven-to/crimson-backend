"""The operator's controls for Lumi: the switch, provider and model, budgets,
and what it has cost. Per-account grants ride on PATCH /admin/users/{id}. API
keys are never read or written here; the dashboard only learns whether each is
present."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from account_engine.audit import log_admin_action
from account_engine.deps import require_admin

from .access import provider_key
from .db import store
from .models import catalogue, get_model
from .providers import ANTHROPIC_SDK_AVAILABLE
from .schemas import ChatSettingsUpdate

router = APIRouter(prefix="/admin/chat", tags=["admin"], dependencies=[Depends(require_admin)])


def _settings_payload() -> dict:
    return {
        "settings": store.get_settings(),
        "models": catalogue(),
        "keys": {
            "anthropic": bool(provider_key("anthropic")),
            "gemini": bool(provider_key("gemini")),
        },
        "sdk": {"anthropic": ANTHROPIC_SDK_AVAILABLE},
    }


@router.get("/settings")
async def chat_settings():
    return await asyncio.to_thread(_settings_payload)


@router.patch("/settings")
async def update_chat_settings(
    request: Request, body: ChatSettingsUpdate, user: dict = Depends(require_admin)
):
    """Switching on without a key for the provider is refused, or the first
    viewer to open the drawer would meet the failure as a 503."""
    patch = body.model_dump(exclude_none=True)
    current = await asyncio.to_thread(store.get_settings)
    provider = patch.get("provider", current["provider"])
    if patch.get("enabled") and not provider_key(provider):
        raise HTTPException(
            status_code=400,
            detail=f"No API key configured for {provider}. Set it in the environment first.",
        )
    if "model" in patch:
        model = get_model(patch["model"])
        if model is None:
            raise HTTPException(status_code=400, detail=f"Unknown model '{patch['model'][:60]}'")
        # Named, rather than silently falling back to the provider's default later.
        if model.provider != provider:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{model.model_id}' belongs to {model.provider}, not {provider}",
            )
    await asyncio.to_thread(store.update_settings, patch, updated_by=user["user_id"])
    log_admin_action(request, user, "chat_settings_updated", **patch)
    return await asyncio.to_thread(_settings_payload)


@router.get("/usage")
async def chat_usage(days: int = Query(30, ge=1, le=365, description="Aggregation window")):
    """Estimated from published per-million rates, so it tracks spend closely but
    will not match an invoice to the cent."""
    return await asyncio.to_thread(store.usage_overview, days)

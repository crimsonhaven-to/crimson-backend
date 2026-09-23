"""Who may chat, and what it costs them.

Gated three ways, in order: a valid session, the operator switching the feature
on, and this account being granted access. Deny by default: a new account can
browse everything and never spend a token.
"""

import asyncio
from typing import Dict, Optional

from fastapi import Depends, HTTPException

from account_engine.deps import require_user
from core.config import get_settings

from .db import store
from .models import ANTHROPIC, GEMINI


def provider_key(provider: str) -> Optional[str]:
    """Keys live in the environment, not the settings table, so a database dump
    never carries billable credentials."""
    settings = get_settings()
    return {ANTHROPIC: settings.anthropic_api_key, GEMINI: settings.gemini_api_key}.get(provider)


def feature_state(user: Dict) -> Dict:
    settings = store.get_settings()
    key_present = bool(provider_key(settings["provider"]))
    granted = bool(user.get("chat_enabled"))
    return {
        "settings": settings,
        "key_present": key_present,
        "granted": granted,
        "available": settings["enabled"] and key_present and granted,
    }


async def require_chat_user(user: dict = Depends(require_user)) -> dict:
    """A viewer without the grant gets 403, not 404, so the drawer can tell them
    to ask the operator. Feature-off and key-missing are reported apart so an
    admin can tell misconfiguration from policy."""
    state = await asyncio.to_thread(feature_state, user)
    if not state["settings"]["enabled"]:
        raise HTTPException(status_code=403, detail="Lumi is not currently awake.")
    if not state["key_present"]:
        raise HTTPException(status_code=503, detail="Lumi has no oracle configured. Tell the operator.")
    if not state["granted"]:
        raise HTTPException(status_code=403, detail="You have not been granted an audience with Lumi.")
    return user


def monthly_budget(user: Dict, settings: Dict) -> int:
    """The account's own ceiling if set, else the global one. 0 means unlimited."""
    own = user.get("chat_monthly_token_budget")
    return int(own) if own is not None else int(settings.get("monthly_token_budget") or 0)

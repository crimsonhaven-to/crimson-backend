"""Turning the @handles a member typed into accounts."""

from typing import Dict, List

from fastapi import HTTPException

from .db import store


def normalize(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def resolve(handles: List[str]) -> List[Dict]:
    """Every handle must name a member, in the order given, without repeats."""
    wanted = list(dict.fromkeys(normalize(h) for h in handles))
    found = {row["handle"]: row for row in store.members_by_handles(wanted)}
    for handle in wanted:
        if handle not in found:
            raise HTTPException(status_code=404, detail=f"No member is called @{handle}")
    return [found[h] for h in wanted]


def resolve_one(handle: str) -> Dict:
    return resolve([handle])[0]

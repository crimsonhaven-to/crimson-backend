"""``GET /skiptimes``: AniSkip intro and outro intervals for an anime episode,
behind the login wall. Always 200; ``found: false`` means the player shows no
skip buttons."""

from fastapi import APIRouter, Query

from core.http_client import http_client
from metadata_engine.anilist import fetch_anilist_metadata

from .service import fetch_skip_times, resolve_mal_id

router = APIRouter(tags=["skiptimes"])


@router.get("/skiptimes")
async def get_skip_times(
    anilist_id: int = Query(..., description="AniList id of the anime (anime-only)"),
    episode: int = Query(..., ge=1, description="Absolute episode number for the season"),
    episode_length: float = Query(
        0, ge=0, description="Player-known episode length in seconds (improves accuracy; 0 = unknown)"
    ),
):
    """``op`` and ``ed`` are ``{start, end}`` in seconds, or null."""
    empty = {"success": True, "found": False, "mal_id": None, "op": None, "ed": None}

    async with http_client() as client:
        meta = await fetch_anilist_metadata(client, anilist_id) or {}
        # The metadata cache holds entries written before it carried idMal, so a
        # missing one here is not authoritative.
        mal_id = meta.get("mal_id") or await resolve_mal_id(client, anilist_id)
        if not mal_id:
            return empty
        result = await fetch_skip_times(client, mal_id, episode, episode_length)
    if result is None:
        return {**empty, "mal_id": mal_id}

    return {
        "success": True,
        "found": bool(result.get("op") or result.get("ed")),
        "mal_id": mal_id,
        "op": result.get("op"),
        "ed": result.get("ed"),
        "episode_length": result.get("episode_length"),
    }

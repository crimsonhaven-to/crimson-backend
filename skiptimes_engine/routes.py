"""
Skip-times API — AniSkip-backed intro/outro timestamps for the anime player.

One authed endpoint (behind the global login wall, like ``/subtitles``):

  * ``GET /skiptimes`` — given an ``anilist_id`` + ``episode``, returns the OP
    (intro) and ED (outro) intervals so CrimsonPlayer can show a "Skip Intro"
    button and reuse the Auto-Next card as a "Skip Outro / Up Next" prompt.

AniList-keyed (so anime-only by construction); the MAL id AniSkip needs is
resolved server-side from ``fetch_anilist_metadata``. Always 200 with a best-effort
body — ``found: false`` just means the player shows no buttons.
"""

from fastapi import APIRouter, Query

from core.http_client import http_client
from metadata_engine.anilist import fetch_anilist_metadata

from .service import resolve_mal_id, service

router = APIRouter(tags=["skiptimes"])


@router.get("/skiptimes")
async def get_skip_times(
    anilist_id: int = Query(..., description="AniList id of the anime (anime-only)"),
    episode: int = Query(..., ge=1, description="Absolute episode number for the season"),
    episode_length: float = Query(
        0, ge=0, description="Player-known episode length in seconds (improves accuracy; 0 = unknown)"
    ),
):
    """Intro/outro skip intervals for an anime episode.

    Returns ``{success, found, mal_id, op, ed, episode_length}`` where ``op``/``ed``
    are ``{start, end}`` (seconds) or ``null``. Never errors on missing data — an
    anime with no AniList ``idMal`` or no AniSkip submissions just yields
    ``found: false``."""
    empty = {"success": True, "found": False, "mal_id": None, "op": None, "ed": None}

    async with http_client() as client:
        meta = await fetch_anilist_metadata(client, anilist_id) or {}
        mal_id = meta.get("mal_id")
        # The shared metadata cache can hold entries written before idMal existed,
        # so a missing mal_id here isn't authoritative — resolve it directly (fresh,
        # small-cached) before giving up. Skipped when the cache already carries it.
        if not mal_id:
            mal_id = await resolve_mal_id(client, anilist_id)
    if not mal_id:
        return empty

    result = await service.fetch(mal_id, episode, episode_length)
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

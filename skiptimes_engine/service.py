"""AniSkip intro and outro (OP/ED) timestamps for the anime player.

AniSkip is keyed by MyAnimeList id and episode, so the route first maps the
AniList id to ``idMal``, which also makes the feature anime-only. An AniSkip 404
just means nobody submitted timings; it is cached like any other answer and the
player shows no buttons.
"""

import logging
from typing import Optional

import httpx

from core.bounded_cache import BoundedCache

logger = logging.getLogger(__name__)

ANISKIP_BASE = "https://api.aniskip.com/v2"
ANILIST_GQL = "https://graphql.anilist.co"
_MAL_QUERY = "query($id:Int){Media(id:$id,type:ANIME){idMal}}"

_TTL = 6 * 3600.0
# anilist_id -> (mal_id or None,). Wrapped in a tuple so a cached "no MAL id" is
# told apart from a miss.
_mal_cache = BoundedCache(4096)
_skip_cache = BoundedCache(4096)


async def resolve_mal_id(client: httpx.AsyncClient, anilist_id: int) -> Optional[int]:
    """A direct query rather than ``fetch_anilist_metadata``, whose 24h
    ``api_cache`` still holds entries written before it carried ``mal_id``."""
    cached = _mal_cache.get(anilist_id)
    if cached is not None:
        return cached[0]

    try:
        resp = await client.post(
            ANILIST_GQL,
            json={"query": _MAL_QUERY, "variables": {"id": anilist_id}},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.info("[aniskip] idMal lookup %s for anilist=%s", resp.status_code, anilist_id)
            return None
        media = ((resp.json() or {}).get("data") or {}).get("Media") or {}
    except (httpx.RequestError, ValueError) as e:
        logger.warning("[aniskip] idMal lookup failed for anilist=%s: %s - %s",
                       anilist_id, type(e).__name__, e)
        return None

    raw = media.get("idMal")
    mal_id = int(raw) if raw else None
    _mal_cache.set(anilist_id, (mal_id,), ttl=_TTL)
    return mal_id


async def fetch_skip_times(
    client: httpx.AsyncClient, mal_id: int, episode: int, episode_length: float = 0
) -> Optional[dict]:
    """``{"op", "ed", "episode_length"}`` with ``{start, end}`` intervals, or None
    only on a transport or upstream error."""
    key = f"{mal_id}:{episode}:{int(episode_length or 0)}"
    cached = _skip_cache.get(key)
    if cached is not None:
        return cached

    params = {"types": ["op", "ed"], "episodeLength": str(int(episode_length or 0))}
    try:
        resp = await client.get(
            f"{ANISKIP_BASE}/skip-times/{mal_id}/{episode}",
            params=params,
            timeout=10.0,
            follow_redirects=True,
        )
    except httpx.RequestError as e:
        logger.warning("[aniskip] request failed: %s - %s", type(e).__name__, e)
        return None

    if resp.status_code == 404:
        result = {"op": None, "ed": None, "episode_length": None}
    elif resp.status_code != 200:
        logger.info("[aniskip] %s for mal=%s ep=%s", resp.status_code, mal_id, episode)
        return None
    else:
        try:
            result = _normalize(resp.json())
        except ValueError:
            return None
    _skip_cache.set(key, result, ttl=_TTL)
    return result


def _normalize(data: dict) -> dict:
    """The first OP and first ED interval from AniSkip's ``results``."""
    op = ed = None
    episode_length = None
    for res in data.get("results") or []:
        interval = res.get("interval") or {}
        start, end = interval.get("startTime"), interval.get("endTime")
        if start is None or end is None:
            continue
        seg = {"start": float(start), "end": float(end)}
        stype = res.get("skipType")
        if stype in ("op", "mixed-op") and op is None:
            op = seg
        elif stype in ("ed", "mixed-ed") and ed is None:
            ed = seg
        if episode_length is None and res.get("episodeLength"):
            episode_length = res.get("episodeLength")
    return {"op": op, "ed": ed, "episode_length": episode_length}

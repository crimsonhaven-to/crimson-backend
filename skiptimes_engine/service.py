"""
AniSkip service — community-sourced anime intro/outro (OP/ED) skip timestamps.

AniSkip (https://api.aniskip.com) is the de-facto source every Zoro/aniwatch-style
player uses for "Skip Intro / Skip Outro". It's free and keyless, keyed on the
**MyAnimeList id + episode number** (plus an optional episode length used to scale
the timestamps). Crimson is AniList-keyed, so the route resolves ``anilist_id ->
mal_id`` via the already-cached ``fetch_anilist_metadata`` (AniList's ``idMal``);
this also makes the feature inherently **anime-only**.

Unlike the subtitle/stream proxies there's no quota or key and the response is
plain JSON the frontend reads directly (not a browser-loaded resource), so there's
no signed proxy — just an authed JSON endpoint behind the login wall with a small
in-memory cache. "Not found" (AniSkip 404) is a normal, cacheable answer: lots of
obscure/new episodes simply have no submitted timings, and the player just doesn't
show the buttons.
"""

import logging
import time
from typing import Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

ANISKIP_BASE = "https://api.aniskip.com/v2"


class AniSkipService:
    """Thin AniSkip client with an in-memory TTL cache. One process-wide instance
    (see ``skiptimes_engine.service.service``)."""

    def __init__(self) -> None:
        # key "{mal}:{ep}:{len}" -> (expires_at_monotonic, normalized result dict)
        self._cache: Dict[str, Tuple[float, dict]] = {}
        self._ttl = 6 * 3600.0
        self._max = 4096

    async def fetch(self, mal_id: int, episode: int,
                    episode_length: float = 0) -> Optional[dict]:
        """Return ``{"op": {start,end}|None, "ed": {start,end}|None,
        "episode_length": float|None}`` for an episode, or ``None`` only on a hard
        upstream/transport error (the route turns that into "no skip times").

        An AniSkip 404 ("no submitted timings") is a *successful* empty result and
        is cached so unknown episodes aren't re-queried."""
        key = f"{mal_id}:{episode}:{int(episode_length or 0)}"
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

        url = f"{ANISKIP_BASE}/skip-times/{mal_id}/{episode}"
        params = [
            ("types", "op"),
            ("types", "ed"),
            ("episodeLength", str(int(episode_length or 0))),
        ]
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, params=params)
        except httpx.RequestError as e:
            logger.warning("[aniskip] request failed: %s - %s", type(e).__name__, e)
            return None

        if resp.status_code == 404:
            result = {"op": None, "ed": None, "episode_length": None}
            self._store(key, result)
            return result
        if resp.status_code != 200:
            logger.info("[aniskip] %s for mal=%s ep=%s", resp.status_code, mal_id, episode)
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        result = self._normalize(data)
        self._store(key, result)
        return result

    @staticmethod
    def _normalize(data: dict) -> dict:
        """Collapse AniSkip's ``results`` to one OP + one ED interval."""
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

    def _store(self, key: str, result: dict) -> None:
        if len(self._cache) >= self._max:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[: self._max // 10]:
                self._cache.pop(k, None)
        self._cache[key] = (time.monotonic() + self._ttl, result)


# Shared, process-wide instance (cache lives here).
service = AniSkipService()

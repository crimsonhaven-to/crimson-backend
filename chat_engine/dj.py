"""Lumi as a DJ: she names a theme, picks a handful of songs from the viewer's own
library for it and introduces the set in a line. The client plays the set and asks
for the next one before it runs out, passing what already played.

One request and no tools: a random slice of the library rides in the prompt, so a
set costs a single round trip and the model can only pick songs that exist. When
the oracle fails or answers nonsense the music keeps going on a random set,
because silence is the one thing a DJ must never produce.

The intro is also spoken (see voice.py), after the set is chosen, since the words
must exist before the voice can. It rides along with the set, so the prefetch two
songs ahead hides that second round trip too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Dict, List, Optional

from music_engine.db import store as music_store

from . import persona, providers, voice
from .access import provider_key, record_usage
from .models import resolve

logger = logging.getLogger("crimson.chat.dj")

# Enough variety for a theme to find four songs, small enough to keep a set cheap.
POOL_SIZE = 120
SET_MIN = 3
SET_MAX = 6
# The client sends every song the session played; only the recent past matters
# for not repeating, and an unbounded list would grow the query forever.
MAX_EXCLUDED = 500
FALLBACK_THEME = "Lumi's wildcards"
FALLBACK_INTRO = "My oracle is sulking, mortal, so I chose these myself. You are welcome."


class EmptyLibrary(Exception):
    pass


def _prompt(pool: List[Dict], previous_themes: List[str]) -> str:
    songs = [
        {"id": t["id"], "title": t["title"], "artists": t["artists"], "album": t["album"]}
        for t in pool
    ]
    return json.dumps({"songs": songs, "previous_themes": previous_themes[-5:]})


def _undashed(text: str) -> str:
    # The prompt bans dashes, but a model drifts, and the intro is shown verbatim.
    return text.replace("\u2014", ", ").replace("\u2013", ", ").strip()


def parse_set(text: str, pool: List[Dict]) -> Optional[Dict]:
    """The set the model described, or None when it is unusable."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        answer = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("track_ids"), list):
        return None

    by_id = {t["id"]: t for t in pool}
    tracks: List[Dict] = []
    for raw in answer["track_ids"]:
        track = by_id.get(raw) if isinstance(raw, int) else None
        if track and track not in tracks:
            tracks.append(track)
    if len(tracks) < SET_MIN:
        return None
    return {
        "theme": _undashed(str(answer.get("theme") or FALLBACK_THEME))[:80],
        "intro": _undashed(str(answer.get("intro") or ""))[:300],
        "tracks": tracks[:SET_MAX],
    }


def random_set(pool: List[Dict]) -> Dict:
    return {
        "theme": FALLBACK_THEME,
        "intro": FALLBACK_INTRO,
        "tracks": random.sample(pool, min(len(pool), SET_MAX)),
    }


async def _pool(user_id: int, played_ids: List[int]) -> List[Dict]:
    excluded = played_ids[-MAX_EXCLUDED:]
    pool = await asyncio.to_thread(
        music_store.random_ready_tracks, user_id, excluded, POOL_SIZE
    )
    if not pool and excluded:
        # Everything has played once: start the library over rather than stop.
        pool = await asyncio.to_thread(music_store.random_ready_tracks, user_id, [], POOL_SIZE)
    if not pool:
        raise EmptyLibrary()
    return pool


async def _ask(user_id: int, settings: Dict, pool: List[Dict], themes: List[str]) -> str:
    provider = settings["provider"]
    model = resolve(provider, settings["model"])
    turn = None
    async for event in providers.stream_turn(
        provider=provider,
        api_key=provider_key(provider),
        model=model,
        system=persona.DJ_PROMPT,
        messages=[providers.user_msg(_prompt(pool, themes))],
        tools=[],
    ):
        if event["type"] == "turn":
            turn = event["turn"]
    if turn is None:
        return ""
    await asyncio.to_thread(record_usage, user_id, provider, model, turn.usage)
    return turn.text


async def next_set(user: Dict, settings: Dict, played_ids: List[int], themes: List[str]) -> Dict:
    """``theme``, ``intro``, the ``tracks`` rows to play in order, and ``voice``:
    the intro spoken as a base64 WAV, or None."""
    pool = await _pool(user["user_id"], played_ids)
    try:
        text = await _ask(user["user_id"], settings, pool, themes)
    except providers.ProviderError as exc:
        logger.warning("dj set for user %s fell back: %s", user["user_id"], exc)
        text = ""
    chosen = parse_set(text, pool) or random_set(pool)
    chosen["voice"] = await voice.speak(user["user_id"], chosen["intro"])
    return chosen

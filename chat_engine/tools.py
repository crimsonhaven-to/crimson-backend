"""
The tools Lumi can call, and their dispatch into the existing engines.

Design rule: nothing here implements catalogue logic. Every handler is a thin
adapter over code that already serves the REST API, so a recommendation Lumi
gives is by construction the same recommendation ``/recommendations`` would give.
When the ranking changes, she changes with it and there is no second
implementation to keep in step.

Schemas are declared once, provider-neutral, in the JSON Schema subset both
Anthropic and Gemini accept. providers.py reshapes them into each vendor's
envelope. Keeping one declaration matters because tool descriptions are the
highest-leverage text in the whole feature: they are what the model reads to
decide whether to call at all.

On ``open_title``
-----------------
It deliberately does NOT navigate anything. It resolves a title to a client
route and returns it, and the drawer renders that as a button the viewer presses.
The backend stays stateless and unaware of the client's router, which is the same
division of labour the rest of the app uses (the client resolves sources; the
backend hands it what it cannot derive).
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from starlette.concurrency import run_in_threadpool

from core.http_client import http_client
from metadata_engine.tmdb import (
    fetch_tmdb_movie_search_results,
    fetch_tmdb_search_results,
    fetch_tmdb_show_search_results,
)

logger = logging.getLogger("crimson.chat.tools")

# Bound on how much any single tool result can add to the context. A viewer with
# a thousand saved titles must not be able to turn one question into a very
# expensive request, and a long tool result crowds out the conversation anyway.
MAX_ITEMS = 8


# --- schemas ---------------------------------------------------------------
# `description` on each tool and each property is what the model actually reads.
# They are written prescriptively (when to call, not just what it does) because
# recent models are conservative about reaching for tools by default.

TOOL_SCHEMAS: List[Dict] = [
    {
        "name": "recommend_titles",
        "description": (
            "Suggest something to watch, personalised from this viewer's own saved "
            "titles and watch history. Call this whenever the viewer asks what to "
            "watch, asks for a suggestion, or says they are bored or undecided. "
            "Never answer those from your own knowledge: only titles this library "
            "actually holds can be recommended."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many suggestions to return, 1 to 8. Default 5.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "search_catalogue",
        "description": (
            "Find a title by name and get its real ids. Call this before saying "
            "anything factual about a specific title, and always before open_title "
            "or manage_watchlist, because those need an id you can only get here. "
            "Returns an empty list when the library does not have it, which is a "
            "real answer and should be reported honestly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The title to search for, as the viewer said it.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["anime", "show", "movie"],
                    "description": (
                        "Which surface to search. Use 'anime' for anime series and "
                        "films, 'show' for live action or western television, "
                        "'movie' for non-anime films. Default 'anime'."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "open_title",
        "description": (
            "Turn a title into a button that takes the viewer straight to playback. "
            "Call this when they ask to watch, play or open something. You must "
            "have the id from search_catalogue, recommend_titles or watch_progress "
            "first. Mention it in one short line; the button carries the rest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["anime", "show", "movie"],
                    "description": "Which kind of title this id belongs to.",
                },
                "anilist_id": {
                    "type": "integer",
                    "description": "Required when kind is 'anime'.",
                },
                "tmdb_id": {
                    "type": "integer",
                    "description": "Required when kind is 'show' or 'movie'.",
                },
                "season": {
                    "type": "integer",
                    "description": "Season number for anime and shows. Defaults to 1.",
                },
                "episode": {
                    "type": "integer",
                    "description": "Episode number for anime and shows. Defaults to 1.",
                },
                "title": {
                    "type": "string",
                    "description": "Display title, used as the button label.",
                },
            },
            "required": ["kind"],
        },
    },
    {
        "name": "watch_progress",
        "description": (
            "Look up what this viewer has been watching and where they stopped. "
            "Call this for questions like 'where was I', 'what was I watching', "
            "'what have I finished', or when they want to resume something without "
            "naming it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "completed", "any"],
                    "description": "Filter by state. Default 'in_progress'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many entries to return, 1 to 8. Default 5.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "manage_watchlist",
        "description": (
            "Save a title to the viewer's watchlist or remove it. Call this only "
            "when they clearly ask to save, add, bookmark, remove or unsave "
            "something. Requires an id from search_catalogue first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove"],
                    "description": "Whether to save or remove the title.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["anime", "show", "movie"],
                    "description": "Which kind of title this id belongs to.",
                },
                "anilist_id": {"type": "integer", "description": "Required when kind is 'anime'."},
                "tmdb_id": {
                    "type": "integer",
                    "description": "Required when kind is 'show' or 'movie'.",
                },
                "title": {"type": "string", "description": "Display title to store with the entry."},
                "poster": {"type": "string", "description": "Poster URL, when known."},
            },
            "required": ["action", "kind"],
        },
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOL_SCHEMAS)


# --- route building --------------------------------------------------------
def build_route(
    kind: str,
    *,
    anilist_id: Optional[int] = None,
    tmdb_id: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> Optional[str]:
    """Client route for a playable thing, or None when the ids do not add up.

    These paths mirror the routes declared in the client's App.jsx. They are the
    one piece of frontend knowledge this engine holds, which is why they are
    isolated in a single function with a test rather than formatted inline.
    """
    s = max(1, int(season or 1))
    e = max(1, int(episode or 1))
    if kind == "anime" and anilist_id:
        return f"/watch/{int(anilist_id)}/{s}/{e}"
    if kind == "show" and tmdb_id:
        return f"/watch-show/{int(tmdb_id)}/{s}/{e}"
    if kind == "movie" and tmdb_id:
        return f"/watch-movie/{int(tmdb_id)}"
    return None


def _item_key(kind: str, anilist_id: Optional[int], tmdb_id: Optional[int]) -> Optional[str]:
    """Dedup key for a watchlist row.

    Mirrors ``account_engine.routes._favorite_item_key``. It is reproduced rather
    than imported because that helper is private to the routes module and
    importing it here would couple two engines through a private name; the shapes
    are asserted equal in the tests instead.
    """
    if kind == "anime" and anilist_id is not None:
        return f"anilist:{anilist_id}"
    if kind == "movie" and tmdb_id is not None:
        return f"movie:{tmdb_id}"
    if tmdb_id is not None:
        return f"tmdb:{tmdb_id}"
    return None


def _clamp(value, default: int, high: int = MAX_ITEMS) -> int:
    try:
        return max(1, min(high, int(value)))
    except (TypeError, ValueError):
        return default


# --- handlers --------------------------------------------------------------
# Each returns (result_for_model, actions_for_client). The first is the JSON the
# model reads; the second is any UI affordance the drawer should render, such as
# a play button. Splitting them keeps presentation data out of the token budget.


async def _recommend_titles(user_id: int, args: Dict):
    # Imported lazily: recommend_engine.routes imports account_engine.routes at
    # module scope, and a top-level import here would drag that whole chain into
    # chat_engine's import time for a tool that may never be called.
    from recommend_engine.routes import _recommend

    limit = _clamp(args.get("limit"), 5)
    payload = await run_in_threadpool(_recommend, user_id, limit)
    items = payload.get("recommendations", [])[:limit]

    slim = [
        {
            "title": it.get("title"),
            "kind": it.get("media_type") or "anime",
            "anilist_id": it.get("anilist_id"),
            "tmdb_id": it.get("tmdb_id"),
            "year": it.get("year"),
            "genres": (it.get("matched_genres") or [])[:3],
        }
        for it in items
    ]
    if not slim:
        return (
            {
                "recommendations": [],
                "note": (
                    "No recommendations yet. This viewer has not saved or watched "
                    "enough for a genre profile. Suggest they watch or save a few "
                    "things first."
                ),
            },
            None,
        )
    return {"recommendations": slim}, {"type": "titles", "items": items}


async def _search_catalogue(user_id: int, args: Dict):
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "A query is required."}, None

    kind = args.get("kind") or "anime"
    fetchers = {
        "anime": fetch_tmdb_search_results,
        "show": fetch_tmdb_show_search_results,
        "movie": fetch_tmdb_movie_search_results,
    }
    fetch = fetchers.get(kind, fetch_tmdb_search_results)

    async with http_client() as client:
        results = await fetch(client, query)

    slim = [
        {
            "title": r.get("title"),
            "kind": kind,
            "anilist_id": r.get("anilist_id"),
            "tmdb_id": r.get("tmdb_id"),
            "year": r.get("year"),
        }
        for r in (results or [])[:MAX_ITEMS]
    ]
    if not slim:
        return (
            {
                "results": [],
                "note": f"The library has nothing matching '{query}' under {kind}.",
            },
            None,
        )
    return {"results": slim}, None


async def _open_title(user_id: int, args: Dict):
    kind = args.get("kind") or "anime"
    route = build_route(
        kind,
        anilist_id=args.get("anilist_id"),
        tmdb_id=args.get("tmdb_id"),
        season=args.get("season"),
        episode=args.get("episode"),
    )
    if not route:
        return (
            {
                "error": (
                    "Missing the id for that kind. Anime needs anilist_id; shows "
                    "and movies need tmdb_id. Use search_catalogue to get one."
                )
            },
            None,
        )

    title = args.get("title") or "this title"
    label = title
    if kind in ("anime", "show"):
        season = max(1, int(args.get("season") or 1))
        episode = max(1, int(args.get("episode") or 1))
        label = f"{title} S{season}E{episode}"

    return (
        {"opened": True, "label": label, "note": "A button was shown to the viewer."},
        {"type": "open", "route": route, "label": label},
    )


async def _watch_progress(user_id: int, args: Dict):
    from account_engine.routes import store

    status = args.get("status") or "in_progress"
    limit = _clamp(args.get("limit"), 5)
    rows = await run_in_threadpool(
        store.list_progress, user_id, None if status == "any" else status
    )

    slim = [
        {
            "title": r.get("title"),
            "kind": r.get("media_type") or ("anime" if r.get("anilist_id") else "show"),
            "anilist_id": r.get("anilist_id"),
            "tmdb_id": r.get("tmdb_id"),
            "season": r.get("season_number"),
            "episode": r.get("episode_number"),
            "status": r.get("status"),
        }
        for r in (rows or [])[:limit]
    ]
    if not slim:
        return {"progress": [], "note": "This viewer has no watch history yet."}, None
    return {"progress": slim}, None


async def _manage_watchlist(user_id: int, args: Dict):
    from account_engine.db import QuotaExceeded
    from account_engine.routes import store

    action = args.get("action")
    kind = args.get("kind") or "anime"
    anilist_id = args.get("anilist_id")
    tmdb_id = args.get("tmdb_id")
    key = _item_key(kind, anilist_id, tmdb_id)
    if not key:
        return (
            {"error": "Missing the id for that kind. Use search_catalogue first."},
            None,
        )

    if action == "remove":
        removed = await run_in_threadpool(store.remove_favorite, user_id, key, None)
        return {"removed": bool(removed)}, None

    fav = {
        "item_key": key,
        "tmdb_id": tmdb_id,
        "anilist_id": anilist_id,
        "season_number": None,
        "media_type": kind,
        "title": args.get("title"),
        "poster": args.get("poster"),
    }
    try:
        await run_in_threadpool(store.upsert_favorite, user_id, fav, "favorites")
    except QuotaExceeded as exc:
        return {"error": f"Their watchlist is full: {exc}"}, None
    return {"saved": True, "title": args.get("title")}, None


_HANDLERS: Dict[str, Callable] = {
    "recommend_titles": _recommend_titles,
    "search_catalogue": _search_catalogue,
    "open_title": _open_title,
    "watch_progress": _watch_progress,
    "manage_watchlist": _manage_watchlist,
}


async def dispatch(name: str, args: Dict, *, user_id: int):
    """Run one tool call.

    Returns ``(result, action)``. Never raises: a tool that blows up returns an
    error object the model can read and recover from, because an exception here
    would abort a half-streamed reply and leave the drawer stuck. The model is
    explicitly told in the persona to report failures honestly rather than
    invent a result.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"No such tool: {name}"}, None
    try:
        return await handler(user_id, args or {})
    except Exception as exc:  # noqa: BLE001 - deliberately total, see docstring
        logger.warning("chat tool %s failed for user %s: %s", name, user_id, exc)
        return {"error": f"The {name} tool failed. Tell the viewer plainly."}, None

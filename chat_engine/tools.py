"""
The tools Lumi can call, each a thin adapter over code that already serves the
REST API. A recommendation from Lumi is therefore the one ``/recommendations``
gives, and there is no second implementation to keep in step.

Schemas are declared once, in the JSON Schema subset both providers accept, and
providers.py reshapes them. The descriptions are the highest-leverage text in the
feature: they are what the model reads to decide whether to call at all.

``open_title`` navigates nothing. It returns a client route that the drawer
renders as a button, so the backend stays unaware of the client's router.
``play_music`` works the same way: the drawer turns it into a play button,
because a browser only starts audio from a tap.

The music tools are offered only to a viewer who has music, so a model never
sees a tool its viewer cannot use.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from account_engine.db import QuotaExceeded, store
from account_engine.library import as_show_favorite, favorite_item_key, show_favorite_key
from core.http_client import http_client
from metadata_engine.tmdb import (
    fetch_tmdb_movie_search_results,
    fetch_tmdb_search_results,
    fetch_tmdb_show_search_results,
)
from music_engine import library as music_library
from music_engine.access import has_music
from music_engine.db import store as music_store
from recommend_engine.service import recommend

logger = logging.getLogger("crimson.chat.tools")

# Bound on how much any single tool result can add to the context. A viewer with
# a thousand saved titles must not be able to turn one question into a very
# expensive request, and a long tool result crowds out the conversation anyway.
MAX_ITEMS = 8


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

MUSIC_TOOL_SCHEMAS: List[Dict] = [
    {
        "name": "find_music",
        "description": (
            "Search this viewer's own music library: their playlists and the songs "
            "in them. Call this whenever they mention music, a song, an artist, an "
            "album or a playlist, and always before play_music, add_to_playlist or "
            "create_playlist, because those need ids you can only get here. With no "
            "query it lists every playlist they have. It only knows songs already in "
            "their playlists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A song title, artist, album or playlist name, as the viewer "
                        "said it. Leave it out to list their playlists."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "play_music",
        "description": (
            "Turn a playlist, or one song in it, into a button that starts playback. "
            "Call this when the viewer asks to play, put on or listen to something. "
            "Needs a playlist_id from find_music; pass a track_id too to start at "
            "that song. Mention it in one short line; the button carries the rest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playlist_id": {"type": "integer", "description": "The playlist to play."},
                "track_id": {
                    "type": "integer",
                    "description": "A song in that playlist to start at. Optional.",
                },
                "shuffle": {
                    "type": "boolean",
                    "description": "Shuffle the playlist. Default false.",
                },
            },
            "required": ["playlist_id"],
        },
    },
    {
        "name": "add_to_playlist",
        "description": (
            "Add songs from the viewer's library to one of their own playlists. Call "
            "this only when they clearly ask to add songs. Only playlists find_music "
            "marks editable can change; imported ones follow Spotify."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playlist_id": {"type": "integer", "description": "An editable playlist."},
                "track_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Song ids from find_music, in the order to add them.",
                },
            },
            "required": ["playlist_id", "track_ids"],
        },
    },
    {
        "name": "create_playlist",
        "description": (
            "Make a new playlist of the viewer's own, optionally filled with songs "
            "from their library. Call this only when they ask for a new playlist. "
            "Returns a button that opens it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The playlist's name."},
                "track_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Song ids from find_music to start it with. Optional.",
                },
            },
            "required": ["name"],
        },
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOL_SCHEMAS + MUSIC_TOOL_SCHEMAS)


def schemas_for(user: Dict) -> List[Dict]:
    return TOOL_SCHEMAS + MUSIC_TOOL_SCHEMAS if has_music(user) else TOOL_SCHEMAS


def _at_least_one(value) -> int:
    return max(1, int(value or 1))


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
    s, e = _at_least_one(season), _at_least_one(episode)
    if kind == "anime" and anilist_id:
        return f"/watch/{int(anilist_id)}/{s}/{e}"
    if kind == "show" and tmdb_id:
        return f"/watch-show/{int(tmdb_id)}/{s}/{e}"
    if kind == "movie" and tmdb_id:
        return f"/watch-movie/{int(tmdb_id)}"
    return None


def _item_key(kind: str, anilist_id: Optional[int], tmdb_id: Optional[int]) -> Optional[str]:
    """The watchlist key, matching the account engine's. Only anime key by AniList id."""
    anilist_id = anilist_id if kind == "anime" else None
    if anilist_id is None and tmdb_id is None:
        return None
    return favorite_item_key(tmdb_id, anilist_id, kind)


def _clamp(value, default: int, high: int = MAX_ITEMS) -> int:
    try:
        return max(1, min(high, int(value)))
    except (TypeError, ValueError):
        return default


# Each handler returns (result_for_model, action_for_client): the JSON the model
# reads, and any affordance the drawer renders, such as a play button. Splitting
# them keeps presentation data out of the token budget.


async def _recommend_titles(user_id: int, args: Dict):
    limit = _clamp(args.get("limit"), 5)
    payload = await asyncio.to_thread(recommend, user_id, limit)
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
        label = f"{title} S{_at_least_one(args.get('season'))}E{_at_least_one(args.get('episode'))}"

    return (
        {"opened": True, "label": label, "note": "A button was shown to the viewer."},
        {"type": "open", "route": route, "label": label},
    )


async def _watch_progress(user_id: int, args: Dict):
    status = args.get("status") or "in_progress"
    limit = _clamp(args.get("limit"), 5)
    rows = await asyncio.to_thread(
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
        if kind == "anime":
            key = await asyncio.to_thread(show_favorite_key, tmdb_id, anilist_id, kind)
        removed = await asyncio.to_thread(store.remove_favorite, user_id, key, None)
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
        fav = await asyncio.to_thread(as_show_favorite, fav)
        await asyncio.to_thread(store.upsert_favorite, user_id, fav, "favorites")
    except QuotaExceeded as exc:
        return {"error": f"Their watchlist is full: {exc}"}, None
    return {"saved": True, "title": args.get("title")}, None


def _playlist_slim(row: Dict) -> Dict:
    return {
        "playlist_id": row["id"],
        "name": row["name"],
        "editable": row["source"] == music_library.LOCAL,
        "songs": row.get("track_count"),
    }


async def _find_music(user_id: int, args: Dict):
    query = (args.get("query") or "").strip()
    playlists = await asyncio.to_thread(music_store.list_playlists, user_id)
    if not query:
        if not playlists:
            return {"playlists": [], "note": "This viewer has no playlists yet."}, None
        return {"playlists": [_playlist_slim(p) for p in playlists]}, None

    named = [p for p in playlists if query.lower() in p["name"].lower()][:MAX_ITEMS]
    songs = await asyncio.to_thread(music_store.search_library, user_id, query, MAX_ITEMS)
    result: Dict = {
        "playlists": [_playlist_slim(p) for p in named],
        "songs": [
            {
                "track_id": t["id"],
                "title": t["title"],
                "artists": t["artists"],
                "album": t["album"],
                "playable": t["status"] == "ready",
                "playlist_id": t["playlist_id"],
                "playlist": t["playlist_name"],
            }
            for t in songs
        ],
    }
    if not named and not songs:
        result["note"] = f"Nothing in their library matches '{query}'."
    return result, None


async def _owned_playlist(user_id: int, playlist_id) -> Optional[Dict]:
    try:
        return await asyncio.to_thread(music_store.get_playlist, user_id, int(playlist_id))
    except (TypeError, ValueError):
        return None


def _track_ids(value) -> List[int]:
    try:
        return [int(v) for v in (value or [])][:100]
    except (TypeError, ValueError):
        return []


async def _play_music(user_id: int, args: Dict):
    playlist = await _owned_playlist(user_id, args.get("playlist_id"))
    if not playlist:
        return {"error": "No such playlist. Use find_music to get a playlist_id."}, None

    tracks = await asyncio.to_thread(music_store.playlist_tracks, playlist["id"])
    ready = [t for t in tracks if t["status"] == "ready"]
    if not ready:
        return {"error": "Nothing in that playlist has finished downloading yet."}, None

    try:
        track_id = int(args["track_id"]) if args.get("track_id") else None
    except (TypeError, ValueError):
        track_id = None
    start = next((t for t in ready if t["id"] == track_id), None) if track_id else None
    if track_id and start is None:
        return {"error": "That song is not playable from this playlist."}, None

    label = f"{start['title']} · {playlist['name']}" if start else playlist["name"]
    return (
        {"ready": True, "label": label, "note": "A play button was shown to the viewer."},
        {
            "type": "play_music",
            "playlist_id": playlist["id"],
            "track_id": start["id"] if start else None,
            "shuffle": bool(args.get("shuffle")),
            "label": label,
        },
    )


async def _add_to_playlist(user_id: int, args: Dict):
    playlist = await _owned_playlist(user_id, args.get("playlist_id"))
    if not playlist:
        return {"error": "No such playlist. Use find_music to get a playlist_id."}, None
    if playlist["source"] != music_library.LOCAL:
        return {"error": "That playlist is imported and follows its source. Pick an editable one."}, None
    ids = _track_ids(args.get("track_ids"))
    added = await asyncio.to_thread(music_store.append_tracks, user_id, playlist["id"], ids)
    if added:
        await asyncio.to_thread(music_library.write_playlist_file, playlist)
    return {"added": len(added), "skipped": len(ids) - len(added)}, None


async def _create_playlist(user_id: int, args: Dict):
    name = (args.get("name") or "").strip()[:200]
    if not name:
        return {"error": "A playlist needs a name."}, None
    playlist = (await music_library.create_local(user_id, name))["playlist"]
    ids = _track_ids(args.get("track_ids"))
    added = await asyncio.to_thread(music_store.append_tracks, user_id, playlist["id"], ids)
    if added:
        await asyncio.to_thread(music_library.write_playlist_file, playlist)
    return (
        {"playlist_id": playlist["id"], "added": len(added)},
        {"type": "open", "route": f"/music/playlist/{playlist['id']}", "label": name},
    )


_HANDLERS: Dict[str, Callable] = {
    "recommend_titles": _recommend_titles,
    "search_catalogue": _search_catalogue,
    "open_title": _open_title,
    "watch_progress": _watch_progress,
    "manage_watchlist": _manage_watchlist,
    "find_music": _find_music,
    "play_music": _play_music,
    "add_to_playlist": _add_to_playlist,
    "create_playlist": _create_playlist,
}


async def dispatch(name: str, args: Dict, *, user_id: int, offered: frozenset):
    """Run one tool call.

    Returns ``(result, action)``. Never raises: a tool that blows up returns an
    error object the model can read and recover from, because an exception here
    would abort a half-streamed reply and leave the drawer stuck. The model is
    explicitly told in the persona to report failures honestly rather than
    invent a result. ``offered`` is what this viewer was shown, so a model
    cannot reach a tool its viewer was never given by naming it.
    """
    handler = _HANDLERS.get(name) if name in offered else None
    if handler is None:
        return {"error": f"No such tool: {name}"}, None
    try:
        return await handler(user_id, args or {})
    except Exception as exc:  # noqa: BLE001 - deliberately total, see docstring
        logger.warning("chat tool %s failed for user %s: %s", name, user_id, exc)
        return {"error": f"The {name} tool failed. Tell the viewer plainly."}, None

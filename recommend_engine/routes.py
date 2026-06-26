"""
Recommendation API — "what to watch next".

Read-only, purely-additive endpoints built entirely on data already in the
database (genres on ``anime_entries`` / ``tmdb_shows`` / ``tmdb_movies`` + the
account engine's favorites / watch progress). No schema changes, no external
API calls.

    GET /recommendations                  (auth)   personalized, mixed surfaces
    GET /recommendations/similar/{anilist_id}      "more like this" for one anime

The personalized feed seeds three genre profiles — anime, non-anime shows and
movies — from every watchlist plus watch progress (a saved or finished title
weighs more than one merely in progress), scores each surface within its own
genre vocabulary, then merges the three by score. Items come back in the same
shape the frontend already consumes for /trending and /search (title / tmdb_id /
anilist_id / kind / poster / year / vote_average), plus additive fields
(genres, matched_genres, score).
"""

import logging
from collections import defaultdict
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from account_engine.routes import require_user, store
from rate_limit import limiter

from .db import get_catalogue_index
from .recommender import build_genre_weights, score_candidates, top_genres

logger = logging.getLogger(__name__)

router = APIRouter(tags=["recommendations"])

# How much each signal counts toward the genre profile. A saved title is the
# strongest explicit signal; a finished one a strong implicit one; something still
# in progress a weak hint. When a title appears via several signals we keep the
# strongest (max), so it isn't double-counted.
FAVORITE_WEIGHT = 3.0
COMPLETED_WEIGHT = 2.0
IN_PROGRESS_WEIGHT = 1.0


def _tmdb_img(path: Optional[str], size: str = "w500") -> Optional[str]:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


def _shape(item: Dict) -> Dict:
    """Shape a scored candidate as a frontend feed item (mirrors the /trending +
    /search item shapes, with additive extras). ``kind`` tells the client which
    overview page to route to (anime -> /anime, show -> /show, movie -> /movie)."""
    return {
        "title": item.get("title"),
        "tmdb_id": item.get("tmdb_id"),
        "anilist_id": item.get("anilist_id"),
        "kind": item.get("kind"),
        "poster": _tmdb_img(item.get("poster_path")),
        "year": item.get("year"),
        "vote_average": None,  # not stored locally; kept for shape parity
        "genres": sorted(item.get("genres") or []),
        "matched_genres": item.get("matched_genres") or [],
        "score": item.get("score"),
    }


def _classify(row: Dict) -> Optional[str]:
    """Which surface a favorite/progress row belongs to: 'anime' | 'show' | 'movie'."""
    if row.get("anilist_id") is not None:
        return "anime"
    if row.get("media_type") == "movie":
        return "movie"
    if row.get("tmdb_id") is not None:
        return "show"
    return None


def _collect_seeds(user_id: int) -> Dict:
    """Build per-surface weighted seed maps from favorites + watch progress.

    Deduped per title, keeping the highest weight any signal assigned. Returns the
    three seed maps plus counts for the response's ``based_on`` summary.
    Synchronous DB access — call from a threadpool.
    """
    # surface -> key -> {anilist_id?, tmdb_id, weight}
    seeds: Dict[str, Dict] = {"anime": {}, "show": {}, "movie": {}}
    fav_count = 0
    hist_count = 0

    def _add(row: Dict, weight: float) -> bool:
        surface = _classify(row)
        if surface is None:
            return False
        if surface == "anime":
            key = row.get("anilist_id")
        else:
            key = row.get("tmdb_id")
        if key is None:
            return False
        bucket = seeds[surface]
        cur = bucket.get(key)
        if cur is None:
            bucket[key] = {
                "anilist_id": row.get("anilist_id"),
                "tmdb_id": row.get("tmdb_id"),
                "weight": weight,
            }
            return True
        if weight > cur["weight"]:
            cur["weight"] = weight
        if cur.get("tmdb_id") is None and row.get("tmdb_id") is not None:
            cur["tmdb_id"] = row.get("tmdb_id")
        return False

    for row in store.list_favorites(user_id):
        if _add(row, FAVORITE_WEIGHT):
            fav_count += 1

    for row in store.list_progress(user_id):
        weight = COMPLETED_WEIGHT if row.get("status") == "completed" else IN_PROGRESS_WEIGHT
        if _add(row, weight):
            hist_count += 1

    return {"seeds": seeds, "favorites_used": fav_count, "history_used": hist_count}


def _recommend(user_id: int, limit: int) -> Dict:
    """Whole personalized pipeline (runs in a threadpool): collect seeds, build a
    genre profile per surface, score each surface's candidates, merge by score."""
    collected = _collect_seeds(user_id)
    index = get_catalogue_index()
    surfaces = collected["seeds"]

    # Per-surface genre lookup + candidate list.
    config = {
        "anime": (index.genres_by_anilist, index.anime_candidates, "anilist_id"),
        "show": (index.genres_by_show, index.show_candidates, "tmdb_id"),
        "movie": (index.genres_by_movie, index.movie_candidates, "tmdb_id"),
    }

    merged_weights: Dict[str, float] = defaultdict(float)
    scored_all: List[Dict] = []
    total_used = 0

    for surface, (genre_lookup, candidates, key_field) in config.items():
        bucket = surfaces[surface]
        if not bucket:
            continue

        # Resolve each seed to its genres and the show tmdb_id to exclude.
        seed_list: List[Dict] = []
        excluded_tmdb = set()
        for seed in bucket.values():
            tmdb_id = seed.get("tmdb_id")
            if surface == "anime":
                genres = genre_lookup.get(seed.get("anilist_id"))
                if tmdb_id is None and seed.get("anilist_id") is not None:
                    tmdb_id = index.tmdb_by_anilist.get(seed["anilist_id"])
            else:
                genres = genre_lookup.get(tmdb_id)
            if tmdb_id is not None:
                excluded_tmdb.add(tmdb_id)
            if genres:
                seed_list.append({"genres": genres, "weight": seed["weight"]})

        genre_weights, used = build_genre_weights(seed_list)
        total_used += used
        for g, w in genre_weights.items():
            merged_weights[g] += w
        scored_all.extend(score_candidates(candidates, genre_weights, excluded_tmdb))

    # Merge surfaces by score (tie-break newer, then id), then take the top slice.
    scored_all.sort(
        key=lambda c: (c["score"], c.get("year") or 0, c.get("tmdb_id") or 0),
        reverse=True,
    )
    recommendations = [_shape(it) for it in scored_all[:limit]]

    return {
        "recommendations": recommendations,
        "based_on": {
            "seed_count": total_used,
            "favorites_used": collected["favorites_used"],
            "history_used": collected["history_used"],
            "top_genres": top_genres(dict(merged_weights)),
        },
    }


@router.get("/recommendations")
@limiter.limit("30/minute")
async def get_recommendations(
    request: Request,
    user: dict = Depends(require_user),
    limit: int = Query(24, ge=1, le=50, description="Max recommendations to return"),
):
    """Personalized "watch next" feed across anime, shows and movies, ranked by the
    genres of the titles you've saved and watched.

    Returns an empty list (with ``based_on.seed_count == 0``) when there's nothing
    to learn from yet — a brand-new account, or one whose history has no genres on
    record. The frontend can fall back to /trending in that case.
    """
    try:
        result = await run_in_threadpool(_recommend, user["user_id"], limit)
    except Exception as e:
        logger.error(f"recommendations failed: {e}")
        raise HTTPException(status_code=500, detail="Could not build recommendations")

    return {
        "success": True,
        "count": len(result["recommendations"]),
        "based_on": result["based_on"],
        "recommendations": result["recommendations"],
    }


@router.get("/recommendations/similar/{anilist_id}")
@limiter.limit("60/minute")
async def get_similar(
    request: Request,
    anilist_id: int,
    limit: int = Query(20, ge=1, le=50, description="Max recommendations to return"),
):
    """"More like this": anime that share genres with one given title. Public (no
    auth) so it can power a 'Recommended' row on the overview page. 404 if the
    anilist id has no genres on record."""

    def _work() -> Optional[List[Dict]]:
        index = get_catalogue_index()
        genres = index.genres_by_anilist.get(anilist_id)
        if not genres:
            return None
        genre_weights, _ = build_genre_weights([{"genres": genres, "weight": 1.0}])
        excluded = {index.tmdb_by_anilist.get(anilist_id)}
        ranked = score_candidates(index.anime_candidates, genre_weights, excluded)
        return [_shape(it) for it in ranked[:limit]]

    try:
        items = await run_in_threadpool(_work)
    except Exception as e:
        logger.error(f"similar recommendations failed: {e}")
        raise HTTPException(status_code=500, detail="Could not build recommendations")

    if items is None:
        raise HTTPException(status_code=404, detail="No genre data for that title")

    return {"success": True, "anilist_id": anilist_id, "count": len(items), "recommendations": items}

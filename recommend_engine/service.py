"""The personalized feed and "more like this", both built from the genres
already in the local tables. Synchronous; run from a thread.

The feed seeds three genre profiles (anime, TV shows, movies) from the member's
watchlists and progress, scores each surface within its own genre vocabulary,
then merges them by score.
"""

from collections import defaultdict
from typing import Dict, List, Optional

from account_engine.db import store as account_store
from metadata_engine.tmdb import tmdb_img

from .db import get_catalogue_index
from .recommender import build_genre_weights, score_candidates, top_genres

# A saved title is the strongest explicit signal, a finished one a strong
# implicit one, something in progress a weak hint. A title seen through several
# signals keeps its strongest, so it is not counted twice.
FAVORITE_WEIGHT = 3.0
COMPLETED_WEIGHT = 2.0
IN_PROGRESS_WEIGHT = 1.0


def _shape(item: Dict) -> Dict:
    """The /trending and /search item shape, plus genres, matched genres and
    score. ``kind`` tells the client which overview page to open."""
    return {
        "title": item.get("title"),
        "tmdb_id": item.get("tmdb_id"),
        "anilist_id": item.get("anilist_id"),
        "kind": item.get("kind"),
        "poster": tmdb_img(item.get("poster_path")),
        "year": item.get("year"),
        # Not stored locally; present for shape parity.
        "vote_average": None,
        "genres": sorted(item.get("genres") or []),
        "matched_genres": item.get("matched_genres") or [],
        "score": item.get("score"),
    }


def _surface(row: Dict) -> Optional[str]:
    if row.get("anilist_id") is not None:
        return "anime"
    if row.get("media_type") == "movie":
        return "movie"
    if row.get("tmdb_id") is not None:
        return "show"
    return None


def _collect_seeds(user_id: int) -> Dict:
    """Per surface, each title once at the highest weight any signal gave it."""
    seeds: Dict[str, Dict] = {"anime": {}, "show": {}, "movie": {}}
    counts = {"favorites_used": 0, "history_used": 0}

    def _add(row: Dict, weight: float, counter: str) -> None:
        surface = _surface(row)
        key = row.get("anilist_id") if surface == "anime" else row.get("tmdb_id")
        if surface is None or key is None:
            return
        seed = seeds[surface].get(key)
        if seed is None:
            seeds[surface][key] = {"anilist_id": row.get("anilist_id"), "tmdb_id": row.get("tmdb_id"), "weight": weight}
            counts[counter] += 1
            return
        seed["weight"] = max(seed["weight"], weight)
        if seed["tmdb_id"] is None:
            seed["tmdb_id"] = row.get("tmdb_id")

    for row in account_store.list_favorites(user_id):
        _add(row, FAVORITE_WEIGHT, "favorites_used")
    for row in account_store.list_progress(user_id):
        _add(row, COMPLETED_WEIGHT if row.get("status") == "completed" else IN_PROGRESS_WEIGHT, "history_used")
    return {"seeds": seeds, **counts}


def recommend(user_id: int, limit: int) -> Dict:
    """Empty, with ``seed_count`` 0, when there is nothing to learn from yet; the
    client falls back to /trending."""
    collected = _collect_seeds(user_id)
    index = get_catalogue_index()
    surfaces = {
        "anime": (index.genres_by_anilist, index.anime_candidates),
        "show": (index.genres_by_show, index.show_candidates),
        "movie": (index.genres_by_movie, index.movie_candidates),
    }

    merged_weights: Dict[str, float] = defaultdict(float)
    scored: List[Dict] = []
    seed_count = 0
    for surface, (genre_lookup, candidates) in surfaces.items():
        if not collected["seeds"][surface]:
            continue
        seed_list: List[Dict] = []
        # A seed's own show never comes back as its recommendation.
        excluded_tmdb = set()
        for seed in collected["seeds"][surface].values():
            tmdb_id = seed["tmdb_id"]
            if surface == "anime":
                genres = genre_lookup.get(seed["anilist_id"])
                tmdb_id = tmdb_id or index.tmdb_by_anilist.get(seed["anilist_id"])
            else:
                genres = genre_lookup.get(tmdb_id)
            if tmdb_id is not None:
                excluded_tmdb.add(tmdb_id)
            if genres:
                seed_list.append({"genres": genres, "weight": seed["weight"]})
        genre_weights, used = build_genre_weights(seed_list)
        seed_count += used
        for genre, weight in genre_weights.items():
            merged_weights[genre] += weight
        scored.extend(score_candidates(candidates, genre_weights, excluded_tmdb))

    # Ties go to the newer title, then the higher id.
    scored.sort(key=lambda c: (c["score"], c.get("year") or 0, c.get("tmdb_id") or 0), reverse=True)
    return {
        "recommendations": [_shape(it) for it in scored[:limit]],
        "based_on": {
            "seed_count": seed_count,
            "favorites_used": collected["favorites_used"],
            "history_used": collected["history_used"],
            "top_genres": top_genres(dict(merged_weights)),
        },
    }


def similar(anilist_id: int, limit: int) -> Optional[List[Dict]]:
    """Anime sharing genres with one title, or None when it has no genres."""
    index = get_catalogue_index()
    genres = index.genres_by_anilist.get(anilist_id)
    if not genres:
        return None
    genre_weights, _ = build_genre_weights([{"genres": genres, "weight": 1.0}])
    ranked = score_candidates(index.anime_candidates, genre_weights, {index.tmdb_by_anilist.get(anilist_id)})
    return [_shape(it) for it in ranked[:limit]]

"""
Pure recommendation scoring.

Deterministic functions over in-memory data (no I/O), so the weights and the
ranking maths live in one easily-reasoned-about place. The routes layer feeds
these per-surface (anime / shows / movies) seeds and candidate lists — each
scored within its own genre vocabulary — then merges the results by score.
"""

import math
from collections import defaultdict
from typing import Dict, List, Set, Tuple


def build_genre_weights(seeds: List[Dict]) -> Tuple[Dict[str, float], int]:
    """Aggregate seeds into a weighted genre profile.

    ``seeds`` is a list of ``{genres: frozenset, weight: float}``. Returns the
    genre -> summed-weight map and how many seeds actually contributed genres.
    """
    weights: Dict[str, float] = defaultdict(float)
    used = 0
    for seed in seeds:
        genres = seed.get("genres")
        if not genres:
            continue
        used += 1
        w = float(seed.get("weight", 1.0))
        for g in genres:
            weights[g] += w
    return dict(weights), used


def score_candidates(
    candidates: List[Dict],
    genre_weights: Dict[str, float],
    excluded_tmdb: Set[int],
) -> List[Dict]:
    """Score one surface's candidates against a genre profile.

    A candidate's score is the summed weight of its genres the viewer likes,
    dampened by ``sqrt(genre_count)`` so a kitchen-sink entry tagged with many
    genres can't dominate purely by surface area — focused overlap is rewarded.
    Returns every matching candidate (a copy, annotated with ``score`` +
    ``matched_genres``), sorted best-first; the caller merges/slices across
    surfaces. ``excluded_tmdb`` drops titles the viewer already has.
    """
    if not genre_weights:
        return []

    scored: List[Dict] = []
    for cand in candidates:
        if cand.get("tmdb_id") in excluded_tmdb:
            continue
        genres = cand.get("genres") or frozenset()
        matched = [g for g in genres if g in genre_weights]
        if not matched:
            continue
        raw = sum(genre_weights[g] for g in matched)
        score = raw / math.sqrt(len(genres))
        item = dict(cand)
        item["score"] = round(score, 4)
        item["matched_genres"] = matched
        scored.append(item)

    scored.sort(
        key=lambda c: (c["score"], c.get("year") or 0, c.get("tmdb_id") or 0),
        reverse=True,
    )
    return scored


def top_genres(genre_weights: Dict[str, float], limit: int = 8) -> List[Dict]:
    """The heaviest genres in a profile, for a 'because you like ...' summary."""
    ranked = sorted(genre_weights.items(), key=lambda kv: kv[1], reverse=True)
    return [{"genre": g, "weight": round(w, 4)} for g, w in ranked[:limit]]

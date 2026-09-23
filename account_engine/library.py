"""Keys and rules for favorites and watch progress.

TMDB numbers movies and shows independently, so a movie gets its own ``movie:``
namespace. Manga gets ``manga:`` so the client can route it and it reads
unambiguously next to an anime row. The anime and TV keys predate both and are
unchanged, so existing rows need no migration.
"""

from typing import List, Optional

from .schemas import ProgressIn

# Past this share of the runtime an episode counts as watched.
COMPLETED_RATIO = 0.9


def favorite_item_key(
    tmdb_id: Optional[int], anilist_id: Optional[int], media_type: Optional[str] = None
) -> str:
    if media_type == "manga" and anilist_id is not None:
        return f"manga:{anilist_id}"
    if anilist_id is not None:
        return f"anilist:{anilist_id}"
    if media_type == "movie":
        return f"movie:{tmdb_id}"
    return f"tmdb:{tmdb_id}"


def progress_item_key(
    tmdb_id: Optional[int], anilist_id: Optional[int],
    season_number: Optional[int], episode_number: Optional[int],
    media_type: Optional[str] = None, local_id: Optional[str] = None,
) -> str:
    """One row per episode, or one per movie. Manga keeps one row per title: the
    chapter rides in episode_number and the page in position_seconds, so each
    save updates the single "where you're reading" row."""
    if media_type == "local" and local_id:
        base = f"local:{local_id}"
    elif anilist_id is None and media_type == "movie":
        return f"movie:{tmdb_id}"
    elif media_type == "manga" and anilist_id is not None:
        return f"manga:{anilist_id}"
    else:
        base = f"anilist:{anilist_id}" if anilist_id is not None else f"tmdb:{tmdb_id}"
    if season_number is not None:
        base += f":s{season_number}"
    if episode_number is not None:
        base += f":e{episode_number}"
    return base


def show_key(row: dict) -> str:
    """The title a progress row belongs to. Every episode of a local title shares
    its path token, so it collapses the same way."""
    if row.get("anilist_id") is not None:
        return f"anilist:{row['anilist_id']}"
    if row.get("media_type") == "local":
        return f"local:{row.get('local_id')}"
    if row.get("media_type") == "movie":
        return f"movie:{row['tmdb_id']}"
    return f"tmdb:{row['tmdb_id']}"


def dedup_by_show(rows: List[dict], limit: Optional[int] = None) -> List[dict]:
    """One row per title. Rows arrive newest first, so the first one seen for a
    title is its latest episode."""
    seen: set[str] = set()
    out: List[dict] = []
    for row in rows:
        key = show_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if limit is not None and len(out) >= limit:
            break
    return out


def resolve_status(body: ProgressIn) -> str:
    if body.status in ("in_progress", "completed"):
        return body.status
    if body.position_seconds and body.duration_seconds and body.duration_seconds > 0:
        if body.position_seconds / body.duration_seconds >= COMPLETED_RATIO:
            return "completed"
    return "in_progress"

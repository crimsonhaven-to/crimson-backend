"""TMDB date strings, which carry no time or zone."""

from datetime import datetime, timezone
from typing import Optional


def is_future_air_date(air_date: Optional[str]) -> bool:
    """Strictly after today (UTC), so an episode airing today counts as aired. An
    unknown or malformed date also counts as aired, so missing metadata never
    blocks playback."""
    if not air_date:
        return False
    try:
        day = datetime.strptime(air_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return day > datetime.now(timezone.utc).date()


def year_from_date(date_str: Optional[str]) -> Optional[int]:
    if not date_str or len(date_str) < 4 or not date_str[:4].isdigit():
        return None
    return int(date_str[:4])

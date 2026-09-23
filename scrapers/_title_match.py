"""Title normalisation shared by the library sources (local folders, Jellyfin)."""

import re
from typing import List, Optional

# "Show Season 2", "Show 2nd Season", "Show Part 2", "Show II". A bare trailing
# number is left alone because too many titles end in one ("86").
_SEASON_SUFFIX_PATTERNS = [
    r"\s*[:\-]?\s*season\s*\d{1,2}\s*$",
    r"\s*\d{1,2}(?:st|nd|rd|th)\s+season\s*$",
    r"\s*[:\-]?\s*(?:part|cour)\s*\d{1,2}\s*$",
    r"\s+(?:ii|iii|iv|v|vi|vii|viii)\s*$",
]


def norm(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def strip_season_suffix(title: str) -> Optional[str]:
    """``title`` without its trailing season indicator, or None when it has none."""
    if not title:
        return None
    t = title.strip()
    for pat in _SEASON_SUFFIX_PATTERNS:
        stripped = re.sub(pat, "", t, flags=re.I).strip()
        if stripped and stripped != t:
            return stripped
    return None


def search_titles(media_ctx: dict, *, synonyms: bool) -> List[str]:
    """The request's titles, each followed by its season-stripped form, so a
    per-season title ("Show Season 2") still finds a base-named library entry.
    Deduplicated on the normalised form, order kept."""
    titles: List[str] = []
    for key in ("title", "title_english", "title_romaji"):
        v = media_ctx.get(key)
        if v:
            titles.append(v)
            base = strip_season_suffix(v)
            if base:
                titles.append(base)
    if synonyms:
        titles += [s for s in media_ctx.get("synonyms") or [] if s]
    seen: set = set()
    ordered = []
    for t in titles:
        k = norm(t)
        if k and k not in seen:
            seen.add(k)
            ordered.append(t)
    return ordered

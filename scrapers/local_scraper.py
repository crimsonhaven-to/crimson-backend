"""
Local source scraper — finds an episode's file in an admin-registered directory
tree (NAS share / Docker bind-mount) and emits a ``crimson-local:{token}`` embed
the LocalResolver turns into a direct-play ``/local_proxy`` stream.

Disabled (returns nothing) unless at least one local source is enabled in the
Admin Dashboard.

Matching, MVP-pragmatic and layout-tolerant:
  * ``search_anime`` finds candidate *show* directories under each enabled root
    by fuzzy-matching the folder name against the title (+ english/romaji +
    synonyms, and their season-suffix-stripped forms).
  * ``get_episode_embeds`` walks those directories for direct-playable video
    files, parses season/episode out of each filename (and, when the filename
    carries only an episode, the season out of the parent "Season N" folder),
    and returns the file matching the requested S/E. A single-file folder is
    treated as S1E1 so a movie still resolves.

Browser-playable containers (mp4/m4v/mov/webm) are always considered; non-web
containers (mkv/avi/ts/…) are only surfaced for sources that have **encoding**
enabled, in which case they play through the on-the-fly HLS transcode route. The
``is_playable_path`` choke point owns that policy (see local_engine.fs).
"""

from __future__ import annotations

import asyncio
import difflib
import os
import re
from typing import List, Optional

from local_engine.db import LocalSourceStore
from local_engine.fs import EMBED_MARKER, encode_token, is_configured, is_playable_path

from .base_scraper import BaseAnimeScraper

_store = LocalSourceStore()

# How close a folder name must be to a title to count as the show (0..1).
_DIR_MATCH_THRESHOLD = 0.78


def _norm(s: str) -> str:
    """Lowercase, alphanumeric-only — for title/folder comparison."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


_SEASON_SUFFIX_PATTERNS = [
    r"\s*[:\-]?\s*season\s*\d{1,2}\s*$",
    r"\s*\d{1,2}(?:st|nd|rd|th)\s+season\s*$",
    r"\s*[:\-]?\s*(?:part|cour)\s*\d{1,2}\s*$",
    r"\s+(?:ii|iii|iv|v|vi|vii|viii)\s*$",
]


def _strip_season_suffix(title: str) -> Optional[str]:
    """``title`` with a trailing season indicator removed, or None if it has none.
    Mirrors the Jellyfin scraper so a per-season title ("Show Season 2") still
    finds a base-named folder ("Show")."""
    if not title:
        return None
    t = title.strip()
    for pat in _SEASON_SUFFIX_PATTERNS:
        stripped = re.sub(pat, "", t, flags=re.I).strip()
        if stripped and stripped != t:
            return stripped
    return None


def _search_titles(media_ctx: dict) -> List[str]:
    """Ordered, deduped titles to match a folder against."""
    titles: List[str] = []
    for key in ("title", "title_english", "title_romaji"):
        v = media_ctx.get(key)
        if v:
            titles.append(v)
            base = _strip_season_suffix(v)
            if base:
                titles.append(base)
    for syn in media_ctx.get("synonyms") or []:
        if syn:
            titles.append(syn)
    seen, ordered = set(), []
    for t in titles:
        k = _norm(t)
        if k and k not in seen:
            seen.add(k)
            ordered.append(t)
    return ordered


# Season/episode parsed from a filename. Ordered most- to least- specific.
_SE_PATTERNS = [
    re.compile(r"s(\d{1,2})[\s._-]*e(\d{1,3})", re.I),          # S01E02
    re.compile(r"(\d{1,2})x(\d{1,3})", re.I),                    # 1x02
    re.compile(r"season[\s._-]*(\d{1,2}).*?episode[\s._-]*(\d{1,3})", re.I),
]
_EP_ONLY_PATTERNS = [
    re.compile(r"\bepisode[\s._-]*(\d{1,3})\b", re.I),
    re.compile(r"\bep[\s._-]*(\d{1,3})\b", re.I),
    re.compile(r"\be(\d{1,3})\b", re.I),
    re.compile(r"[\s._-]-[\s._-]*(\d{1,3})\b"),                  # "Show - 05"
]
_SEASON_DIR_PATTERNS = [
    re.compile(r"season[\s._-]*(\d{1,2})", re.I),
    re.compile(r"staffel[\s._-]*(\d{1,2})", re.I),
    re.compile(r"\bs(\d{1,2})\b", re.I),
]


def _parse_se(filename: str):
    """Return ``(season|None, episode|None)`` parsed from a filename stem."""
    stem = os.path.splitext(filename)[0]
    for pat in _SE_PATTERNS:
        m = pat.search(stem)
        if m:
            return int(m.group(1)), int(m.group(2))
    for pat in _EP_ONLY_PATTERNS:
        m = pat.search(stem)
        if m:
            return None, int(m.group(1))
    return None, None


def _season_from_dir(name: str) -> Optional[int]:
    for pat in _SEASON_DIR_PATTERNS:
        m = pat.search(name or "")
        if m:
            return int(m.group(1))
    return None


def _dir_matches_title(dir_name: str, norm_titles: List[str]) -> float:
    """Best similarity of a folder name to any target title (0..1).

    A title fully contained in the folder name (or vice-versa) scores 1.0 — this
    handles "Show (2021) [1080p]" style folders cheaply; otherwise fall back to a
    fuzzy ratio.
    """
    nd = _norm(dir_name)
    if not nd:
        return 0.0
    best = 0.0
    for nt in norm_titles:
        if not nt:
            continue
        if nt in nd or nd in nt:
            return 1.0
        best = max(best, difflib.SequenceMatcher(None, nt, nd).ratio())
    return best


class LocalScraper(BaseAnimeScraper):
    """Locates an episode file across the operator's enabled local sources."""

    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        """Collect candidate show directories for this title."""
        self._candidate_dirs: List[str] = []
        if not is_configured():
            return None

        norm_titles = [_norm(t) for t in _search_titles(media_ctx)]
        norm_titles = [t for t in norm_titles if t]
        if not norm_titles:
            return None

        roots = _store.enabled_roots()
        # Directory listing is blocking I/O — keep it off the event loop.
        candidates = await asyncio.to_thread(self._find_show_dirs, roots, norm_titles)
        if not candidates:
            print(f"[LocalScraper] No matching directory under {len(roots)} root(s) for {media_ctx.get('title')!r}")
            return None

        self._candidate_dirs = candidates
        print(f"[LocalScraper] {len(candidates)} candidate dir(s): {candidates[:4]}")
        return candidates[0]

    @staticmethod
    def _find_show_dirs(roots: List[str], norm_titles: List[str]) -> List[str]:
        """Scan each root (and one level down) for folders matching the title."""
        scored: list = []
        for root in roots:
            try:
                if not os.path.isdir(root):
                    continue
                for name in os.listdir(root):
                    full = os.path.join(root, name)
                    if not os.path.isdir(full):
                        continue
                    score = _dir_matches_title(name, norm_titles)
                    if score >= _DIR_MATCH_THRESHOLD:
                        scored.append((score, full))
            except Exception as e:
                print(f"[LocalScraper] Could not scan {root!r}: {type(e).__name__} - {e}")
        scored.sort(key=lambda x: x[0], reverse=True)
        return [path for _score, path in scored]

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> List[str]:
        """Find the file for the requested season/episode and emit its marker."""
        if not anime_slug or not is_configured():
            return []
        candidate_dirs = getattr(self, "_candidate_dirs", None) or [anime_slug]

        path = await asyncio.to_thread(self._locate_file, candidate_dirs, season_num, episode_num)
        if not path:
            print(f"[LocalScraper] No file for S{season_num}E{episode_num} in {candidate_dirs[:3]}")
            return []
        print(f"[LocalScraper] Matched S{season_num}E{episode_num} -> {path}")
        return [f"{EMBED_MARKER}:{encode_token(path)}"]

    @staticmethod
    def _locate_file(candidate_dirs: List[str], season_num: int, episode_num: int) -> Optional[str]:
        # Collect every direct-playable file with its parsed (season, episode).
        entries: list = []  # (season, episode, path)
        all_videos: list = []
        for base in candidate_dirs:
            try:
                for root, _dirs, files in os.walk(base):
                    parent = os.path.basename(root)
                    dir_season = _season_from_dir(parent)
                    for f in files:
                        full = os.path.join(root, f)
                        # Web-native always; non-web only when its source has encoding
                        # on (then it plays via the /local_hls transcode route).
                        if not is_playable_path(full):
                            continue
                        all_videos.append(full)
                        s, e = _parse_se(f)
                        if e is None:
                            continue
                        if s is None:
                            s = dir_season if dir_season is not None else 1
                        entries.append((s, e, full))
            except Exception as e:
                print(f"[LocalScraper] Walk failed for {base!r}: {type(e).__name__} - {e}")

        # 1) exact season + episode.
        for s, e, full in entries:
            if s == season_num and e == episode_num:
                return full
        # 2) season 1 is often implicit (flat folders): match on episode alone.
        if season_num == 1:
            for s, e, full in entries:
                if e == episode_num:
                    return full
        # 3) movie fallback: a single-file folder requested as S1E1.
        if season_num == 1 and episode_num == 1 and len(all_videos) == 1:
            return all_videos[0]
        return None

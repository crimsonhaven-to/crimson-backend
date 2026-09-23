"""
Finds an episode in an admin-registered directory tree (NAS share or bind mount)
and emits a ``crimson-local:{token}`` marker for the LocalResolver.

Show folders are fuzzy-matched against the title variants under each enabled
root. Season and episode come from the file name, with the
season falling back to a "Season N" parent folder. A folder holding a single
video counts as S1E1, so a movie still resolves. Which files count at all
(browser-native always, others only with encoding on) is
``local_engine.fs.is_playable_path``'s call.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
from typing import List, Optional

from local_engine.db import store
from local_engine.fs import EMBED_MARKER, encode_token, is_configured, is_playable_path

from ._title_match import norm, search_titles
from .base_scraper import BaseAnimeScraper

logger = logging.getLogger(__name__)

_DIR_MATCH_THRESHOLD = 0.78

# Most specific first.
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


def _parse_se(filename: str) -> tuple[Optional[int], Optional[int]]:
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
    """Best similarity (0..1) of a folder name to any title. Containment either
    way scores 1.0, which covers "Show (2021) [1080p]" folders without fuzzing."""
    nd = norm(dir_name)
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
    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        self._candidate_dirs: List[str] = []
        if not is_configured():
            return None

        norm_titles = [norm(t) for t in search_titles(media_ctx, synonyms=True)]
        if not norm_titles:
            return None

        # A cache miss on the roots is a database round trip and the listing is
        # disk I/O, and this runs inside the /watch fan-out, so both leave the loop.
        roots = await asyncio.to_thread(store.enabled_roots)
        candidates = await asyncio.to_thread(self._find_show_dirs, roots, norm_titles)
        if not candidates:
            logger.info("Local: no directory under %d root(s) matches %r", len(roots), media_ctx.get("title"))
            return None

        self._candidate_dirs = candidates
        logger.info("Local: %d candidate dir(s): %s", len(candidates), candidates[:4])
        return candidates[0]

    @staticmethod
    def _find_show_dirs(roots: List[str], norm_titles: List[str]) -> List[str]:
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
                logger.warning("Local: could not scan %r: %s - %s", root, type(e).__name__, e)
        scored.sort(key=lambda x: x[0], reverse=True)
        return [path for _score, path in scored]

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> List[str]:
        if not anime_slug or not is_configured():
            return []
        candidate_dirs = getattr(self, "_candidate_dirs", None) or [anime_slug]

        path = await asyncio.to_thread(self._locate_file, candidate_dirs, season_num, episode_num)
        if not path:
            logger.info("Local: no file for S%sE%s in %s", season_num, episode_num, candidate_dirs[:3])
            return []
        logger.info("Local: matched S%sE%s -> %s", season_num, episode_num, path)
        return [f"{EMBED_MARKER}:{encode_token(path)}"]

    @staticmethod
    def _locate_file(candidate_dirs: List[str], season_num: int, episode_num: int) -> Optional[str]:
        entries: list = []  # (season, episode, path)
        all_videos: list = []
        for base in candidate_dirs:
            try:
                for root, _dirs, files in os.walk(base):
                    dir_season = _season_from_dir(os.path.basename(root))
                    for f in files:
                        full = os.path.join(root, f)
                        if not is_playable_path(full):
                            continue
                        all_videos.append(full)
                        season, episode = _parse_se(f)
                        if episode is None:
                            continue
                        if season is None:
                            season = dir_season if dir_season is not None else 1
                        entries.append((season, episode, full))
            except Exception as e:
                logger.warning("Local: walk failed for %r: %s - %s", base, type(e).__name__, e)

        for season, episode, full in entries:
            if season == season_num and episode == episode_num:
                return full
        # Flat folders leave season 1 implicit, so match on the episode alone.
        if season_num == 1:
            for _season, episode, full in entries:
                if episode == episode_num:
                    return full
        # A movie: a single-file folder requested as S1E1.
        if season_num == 1 and episode_num == 1 and len(all_videos) == 1:
            return all_videos[0]
        return None

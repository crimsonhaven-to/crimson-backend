"""Browsable catalogue of the Local roots: whatever is on disk becomes titles,
even files that match no TMDB or AniList entry.

A folder is one title (show or movie) and a loose file is a single-file movie.
Container folders (categories, ``crimson-downloads``) are descended into instead.
Display metadata comes from the first source that yields a title:

| order | source   | from                                                   |
|-------|----------|--------------------------------------------------------|
| 1     | nfo      | ``tvshow.nfo``, ``movie.nfo``, ``<stem>.nfo``          |
| 2     | json     | ``<stem>.json``, ``metadata.json``, ``crimson.json``   |
| 3     | embedded | ffprobe container tags, probe count capped per scan    |
| 4     | tmdb-dir | the cache's ``tmdb-<id>`` and ``movie-tmdb-<id>`` names |
| 5     | filename | the cleaned folder or file name                        |

Titles and episodes are identified by the same path tokens playback uses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from core.bounded_cache import BoundedCache
from core.ffmpeg import ffprobe_available

from .db import store
from .fs import (
    ART_EXTENSIONS,
    art_proxy_url,
    encode_token,
    is_playable_path,
    safe_resolve,
    safe_resolve_dir,
    source_label_for,
)

logger = logging.getLogger("local_engine.library")


# Bounds so registering a huge NAS can never hang a scan or one title's walk.
_MAX_TITLES = 4000
_MAX_FILES_PER_TITLE = 1500
_EMBED_PROBE_BUDGET = 200
# How deep the scan descends through container folders: a guard against a
# pathological tree, not a limit on a title's own season nesting.
_MAX_SCAN_DEPTH = 6

_SHOW_NFO_NAMES = ("tvshow.nfo",)
_MOVIE_NFO_NAMES = ("movie.nfo",)
_JSON_NAMES = ("metadata.json", "crimson.json")
_ART_BASENAMES = ("poster", "folder", "cover", "default", "movie", "show", "banner", "fanart")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


_JUNK_TOKENS = re.compile(
    r"\b(1080p|2160p|720p|480p|4k|uhd|hdr|x264|x265|h264|h265|hevc|aac|ac3|dts|"
    r"bluray|blu-ray|bdrip|brrip|webrip|web-dl|webdl|hdtv|dvdrip|remux|proper|"
    r"repack|multi|dual|dubbed|subbed|complete|season|staffel)\b",
    re.I,
)
_YEAR_RE = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)")


def _clean_title(raw: str) -> str:
    """A display title from a release name: no extension, bracketed groups,
    release junk or trailing ``- GROUP``."""
    name = raw or ""
    name = re.sub(r"\.[a-z0-9]{2,4}$", "", name, flags=re.I)
    name = re.sub(r"[\[(\{].*?[\])\}]", " ", name)
    name = name.replace("_", " ").replace(".", " ")
    name = _JUNK_TOKENS.sub(" ", name)
    # Release names separate the group with a hyphen, en dash or em dash.
    name = re.sub(r"[-\u2013\u2014]\s*[A-Za-z0-9]+\s*$", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -\u2013\u2014\u00b7.")
    return name or (raw or "").strip()


def _parse_year(raw: str) -> Optional[int]:
    m = _YEAR_RE.search(raw or "")
    if not m:
        return None
    y = int(m.group(1))
    return y if 1900 <= y <= 2100 else None


# The cache names folders by TMDB id (cache_engine.fs.plan_rel_path). Recognising
# them gives a cached library real titles and the right kind instead of "tmdb".
_TMDB_DIR_RE = re.compile(r"^(movie-)?tmdb[-_](\d+)$", re.I)


def _tmdb_from_dirname(name: str):
    m = _TMDB_DIR_RE.match((name or "").strip())
    if not m:
        return None, None
    return int(m.group(2)), ("movie" if m.group(1) else "show")


# Mirrors the Local scraper's patterns, kept here so the library does not import
# a scraper.
_SE_PATTERNS = [
    re.compile(r"s(\d{1,2})[\s._-]*e(\d{1,3})", re.I),
    re.compile(r"(\d{1,2})x(\d{1,3})", re.I),
    re.compile(r"season[\s._-]*(\d{1,2}).*?episode[\s._-]*(\d{1,3})", re.I),
]
_EP_ONLY_PATTERNS = [
    re.compile(r"\bepisode[\s._-]*(\d{1,3})\b", re.I),
    re.compile(r"\bep[\s._-]*(\d{1,3})\b", re.I),
    re.compile(r"\be(\d{1,3})\b", re.I),
    re.compile(r"[\s._-]-[\s._-]*(\d{1,3})\b"),
]
_SEASON_DIR_PATTERNS = [
    re.compile(r"season[\s._-]*(\d{1,2})", re.I),
    re.compile(r"staffel[\s._-]*(\d{1,2})", re.I),
    re.compile(r"\bs(\d{1,2})\b", re.I),
]


def _parse_se(filename: str) -> Tuple[Optional[int], Optional[int]]:
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


def _first_text(root: ET.Element, tags: Tuple[str, ...]) -> Optional[str]:
    for tag in tags:
        el = root.find(tag)
        text = (el.text or "").strip() if el is not None else ""
        if text:
            return text
    return None


def _parse_nfo(path: str) -> Optional[Dict]:
    """Metadata from a ``movie``, ``tvshow`` or ``episodedetails`` nfo, or None
    when it does not parse."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as e:
        logger.debug(f"nfo parse failed for {path!r}: {e}")
        return None

    meta: Dict = {"source": "nfo"}
    tag = (root.tag or "").lower()
    if tag == "movie":
        meta["media_kind"] = "movie"
    elif tag in ("tvshow", "season"):
        meta["media_kind"] = "show"

    title = _first_text(root, ("title", "originaltitle", "showtitle"))
    if title:
        meta["title"] = title.strip()

    year_txt = _first_text(root, ("year", "premiered", "aired", "releasedate"))
    if year_txt:
        meta["year"] = _parse_year(year_txt)

    plot = _first_text(root, ("plot", "outline", "summary"))
    if plot:
        meta["description"] = plot

    genres = [(g.text or "").strip() for g in root.findall("genre") if (g.text or "").strip()]
    if genres:
        meta["genres"] = genres

    # Older scrapers write <tmdbid> instead of <uniqueid type="tmdb">.
    for uid in root.findall("uniqueid"):
        utype = (uid.get("type") or "").lower()
        val = (uid.text or "").strip()
        if not val:
            continue
        if utype == "tmdb" and val.isdigit():
            meta["tmdb_id"] = int(val)
        elif utype == "anilist" and val.isdigit():
            meta["anilist_id"] = int(val)
        elif utype == "imdb":
            meta["imdb_id"] = val
    if "tmdb_id" not in meta:
        legacy = _first_text(root, ("tmdbid",))
        if legacy and legacy.isdigit():
            meta["tmdb_id"] = int(legacy)

    if tag == "episodedetails":
        se = _first_text(root, ("season",))
        ep = _first_text(root, ("episode",))
        meta["season"] = int(se) if se and se.isdigit() else None
        meta["episode"] = int(ep) if ep and ep.isdigit() else None

    return meta


def _parse_sidecar_json(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        logger.debug(f"sidecar json failed for {path!r}: {e}")
        return None
    if not isinstance(data, dict):
        return None

    def pick(*keys):
        for k in keys:
            if data.get(k):
                return data[k]
        return None

    meta: Dict = {"source": "json"}
    title = pick("title", "name")
    if title:
        meta["title"] = str(title).strip()
    year = pick("year")
    if isinstance(year, int):
        meta["year"] = year
    elif isinstance(year, str):
        meta["year"] = _parse_year(year)
    desc = pick("overview", "plot", "description", "summary")
    if desc:
        meta["description"] = str(desc)
    genres = pick("genres", "genre")
    if isinstance(genres, list):
        meta["genres"] = [str(g) for g in genres if g]
    elif isinstance(genres, str):
        meta["genres"] = [g.strip() for g in genres.split(",") if g.strip()]
    poster = pick("poster", "poster_url", "image")
    if isinstance(poster, str) and poster.lower().startswith(("http://", "https://")):
        meta["poster"] = poster
    tmdb = pick("tmdb_id", "tmdbId", "tmdbid")
    if isinstance(tmdb, int):
        meta["tmdb_id"] = tmdb
    elif isinstance(tmdb, str) and tmdb.isdigit():
        meta["tmdb_id"] = int(tmdb)
    kind = pick("kind", "type", "media_kind")
    if isinstance(kind, str) and kind.lower() in ("movie", "show", "tv"):
        meta["media_kind"] = "show" if kind.lower() == "tv" else kind.lower()
    return meta if meta.get("title") else None


# Keyed by (path, mtime, size) so re-scans skip unchanged files. An empty dict
# records "probed, no tags".
_embedded_tags = BoundedCache(4096)


class _ProbeBudget:
    """Caps ffprobe calls across one scan, shared down the recursion."""

    def __init__(self) -> None:
        self.left = _EMBED_PROBE_BUDGET

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _embedded_meta(path: str) -> Optional[Dict]:
    """Title and year from the container tags, or None. Blocking."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, int(st.st_mtime), st.st_size)
    cached = _embedded_tags.get(key)
    if cached is not None:
        return cached or None
    tags: Dict = {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags",
             "-of", "json", path],
            capture_output=True, text=True, timeout=15,
        )
        raw = json.loads(out.stdout or "{}").get("format", {}).get("tags", {}) or {}
        raw = {str(k).lower(): v for k, v in raw.items()}
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        logger.debug(f"ffprobe tags failed for {path!r}: {e}")
        raw = {}
    title = raw.get("title") or raw.get("show")
    if title:
        tags["title"] = str(title).strip()
        tags["source"] = "embedded"
    date = raw.get("date") or raw.get("year")
    if date:
        y = _parse_year(str(date))
        if y:
            tags["year"] = y
    _embedded_tags.set(key, tags)
    return tags or None


def _find_artwork(dir_path: str, stem: Optional[str] = None) -> Optional[str]:
    """A poster-ish image in ``dir_path``. For a loose file only ``<stem>.<img>``
    counts, so a ``poster.jpg`` in a root does not attach to every loose file there."""
    try:
        entries = {e.lower(): e for e in os.listdir(dir_path)}
    except OSError:
        return None
    bases = [stem.lower()] if stem else list(_ART_BASENAMES)
    for base in bases:
        for ext in ART_EXTENSIONS:
            cand = f"{base}{ext}"
            if cand in entries:
                return os.path.join(dir_path, entries[cand])
    return None


def _walk_playable(dir_path: str, cap: int = _MAX_FILES_PER_TITLE) -> List[str]:
    found: List[str] = []
    for root, _dirs, files in os.walk(dir_path):
        for f in files:
            full = os.path.join(root, f)
            try:
                if is_playable_path(full):
                    found.append(full)
            except Exception:
                continue
            if len(found) >= cap:
                return found
    return found


def _size_or_zero(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _resolve_metadata(dir_path: str, media_files: List[str], stem: Optional[str],
                      is_movie_hint: bool, budget: _ProbeBudget) -> Dict:
    """The precedence in the module docstring, for one title."""
    base_name = stem if stem is not None else os.path.basename(dir_path.rstrip(os.sep))
    meta: Dict = {}

    nfo_candidates: List[str] = []
    if stem is not None:
        nfo_candidates.append(os.path.join(dir_path, f"{stem}.nfo"))
    nfo_candidates += [os.path.join(dir_path, n) for n in (_MOVIE_NFO_NAMES + _SHOW_NFO_NAMES)]
    for nfo in nfo_candidates:
        if os.path.isfile(nfo):
            parsed = _parse_nfo(nfo)
            if parsed and parsed.get("title"):
                meta = parsed
                break

    if not meta.get("title"):
        json_candidates = []
        if stem is not None:
            json_candidates.append(os.path.join(dir_path, f"{stem}.json"))
        json_candidates += [os.path.join(dir_path, n) for n in _JSON_NAMES]
        for jf in json_candidates:
            if os.path.isfile(jf):
                parsed = _parse_sidecar_json(jf)
                if parsed and parsed.get("title"):
                    meta = parsed
                    break

    if not meta.get("title") and media_files and ffprobe_available() and budget.take():
        emb = _embedded_meta(max(media_files, key=_size_or_zero))
        if emb and emb.get("title"):
            meta = dict(emb)

    # Only a folder can be a cache id folder, never a loose file.
    tmdb_dir_id, tmdb_dir_kind = _tmdb_from_dirname(base_name) if stem is None else (None, None)

    # An id folder's name is not a title, so its title is left to TMDB enrichment.
    if not meta.get("title"):
        if tmdb_dir_id:
            meta = {"source": "tmdb-dir"}
        else:
            meta = {"source": "filename", "title": _clean_title(base_name)}
    if tmdb_dir_id:
        meta["tmdb_id"] = meta.get("tmdb_id") or tmdb_dir_id
        meta["media_kind"] = tmdb_dir_kind
    if not meta.get("year"):
        meta["year"] = _parse_year(base_name)
    if "media_kind" not in meta:
        meta["media_kind"] = "movie" if is_movie_hint else "show"
    meta["has_metadata"] = meta.get("source") in ("nfo", "json", "embedded", "tmdb-dir")
    return meta


def _build_title(entry_path: str, is_dir: bool, budget: _ProbeBudget) -> Optional[Dict]:
    """One list item for a folder or a loose file, or None when nothing in it plays."""
    if is_dir:
        dir_path = entry_path
        stem = None
        media_files = _walk_playable(dir_path)
        if not media_files:
            return None
        parsed_eps = [se for se in (_parse_se(os.path.basename(f)) for f in media_files) if se[1] is not None]
        has_movie_nfo = any(os.path.isfile(os.path.join(dir_path, n)) for n in _MOVIE_NFO_NAMES)
        is_movie = has_movie_nfo or (len(media_files) == 1 and not parsed_eps)
        rep_for_token = dir_path
    else:
        dir_path = os.path.dirname(entry_path)
        stem = os.path.splitext(os.path.basename(entry_path))[0]
        media_files = [entry_path]
        is_movie = True
        rep_for_token = entry_path

    meta = _resolve_metadata(dir_path, media_files, stem, is_movie, budget)

    art_path = _find_artwork(dir_path, stem)
    poster = meta.get("poster") or (art_proxy_url(art_path) if art_path else None)

    # Metadata beats the file-count guess: a cache tmdb-<id> folder holding one
    # episode is still a show.
    final_kind = meta.get("media_kind") or ("movie" if is_movie else "show")
    final_is_movie = final_kind == "movie"
    # "TMDB 280042" is a placeholder the route replaces through enrichment.
    title = meta.get("title")
    if not title:
        title = f"TMDB {meta['tmdb_id']}" if meta.get("tmdb_id") else _clean_title(os.path.basename(rep_for_token))

    return {
        "id": encode_token(rep_for_token),
        "title": title,
        "year": meta.get("year"),
        "poster": poster,
        "genres": meta.get("genres") or [],
        "media_kind": final_kind,
        "episode_count": 0 if final_is_movie else len(media_files),
        "source_label": source_label_for(os.path.realpath(rep_for_token)) or "Local",
        "has_metadata": bool(meta.get("has_metadata")),
        "tmdb_id": meta.get("tmdb_id"),
        "description": meta.get("description"),
    }


_TITLE = "title"
_CONTAINER = "container"
_EMPTY = "empty"


def _dir_has_media(dir_path: str) -> bool:
    for root, _dirs, files in os.walk(dir_path):
        for f in files:
            try:
                if is_playable_path(os.path.join(root, f)):
                    return True
            except Exception:
                continue
    return False


def _has_direct_media(dir_path: str) -> bool:
    try:
        with os.scandir(dir_path) as it:
            for e in it:
                if e.is_file():
                    try:
                        if is_playable_path(e.path):
                            return True
                    except Exception:
                        continue
    except OSError:
        pass
    return False


def _child_dirs(dir_path: str) -> List[str]:
    out: List[str] = []
    try:
        with os.scandir(dir_path) as it:
            for e in it:
                if e.is_dir() and not e.name.startswith("."):
                    out.append(e.path)
    except OSError:
        pass
    return out


def _classify_dir(dir_path: str) -> str:
    """A title holds media directly or in season folders, so a multi-season show
    stays one tile. A container (``Movies/``, ``crimson-downloads/``) only has
    other folders with media, and the scan descends into it."""
    if _has_direct_media(dir_path):
        return _TITLE
    subdirs = _child_dirs(dir_path)
    season_dirs = [sub for sub in subdirs if _season_from_dir(os.path.basename(sub)) is not None]
    if any(_dir_has_media(sub) for sub in season_dirs):
        return _TITLE
    if any(_dir_has_media(sub) for sub in subdirs if sub not in season_dirs):
        return _CONTAINER
    return _EMPTY


def _scan_dir(dir_path: str, items: List[Dict], seen: set, budget: _ProbeBudget,
              depth: int) -> bool:
    """Append the titles under ``dir_path``. False once _MAX_TITLES is hit, so the
    caller stops walking."""
    if depth > _MAX_SCAN_DEPTH:
        return True
    try:
        names = sorted(os.listdir(dir_path))
    except OSError as e:
        logger.warning(f"[library] could not scan {dir_path!r}: {e}")
        return True

    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(dir_path, name)
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            continue

        if not is_dir:
            try:
                if not is_playable_path(full):
                    continue
                item = _build_title(full, False, budget)
            except Exception as e:
                logger.warning(f"[library] failed to build title for {full!r}: {e}")
                item = None
        else:
            cls = _classify_dir(full)
            if cls == _CONTAINER:
                if not _scan_dir(full, items, seen, budget, depth + 1):
                    return False
                continue
            if cls == _EMPTY:
                continue
            try:
                item = _build_title(full, True, budget)
            except Exception as e:
                logger.warning(f"[library] failed to build title for {full!r}: {e}")
                item = None

        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)
        if len(items) >= _MAX_TITLES:
            logger.warning(f"[library] hit _MAX_TITLES={_MAX_TITLES}; truncating scan")
            return False
    return True


def scan_library() -> List[Dict]:
    """Every title under the enabled roots, sorted by title. Offline and blocking."""
    items: List[Dict] = []
    budget = _ProbeBudget()
    seen_ids: set = set()
    for root in store.enabled_roots():
        if not os.path.isdir(root):
            continue
        if not _scan_dir(root, items, seen_ids, budget, depth=0):
            break
    items.sort(key=lambda x: (x["title"] or "").lower())
    return items


def _episodes_for(dir_path: str) -> List[Dict]:
    media_files = _walk_playable(dir_path)
    eps: List[Dict] = []
    any_parsed = False
    for full in media_files:
        fname = os.path.basename(full)
        parent = os.path.basename(os.path.dirname(full))
        s, e = _parse_se(fname)
        if e is not None:
            any_parsed = True
            if s is None:
                s = _season_from_dir(parent)
                if s is None:
                    s = 1
        ep_title = None
        nfo = os.path.splitext(full)[0] + ".nfo"
        if os.path.isfile(nfo):
            parsed = _parse_nfo(nfo)
            if parsed:
                ep_title = parsed.get("title")
        eps.append({
            "season_number": s,
            "episode_number": e,
            "title": ep_title or _clean_title(fname),
            "id": encode_token(full),
            "_name": fname,
        })
    if not any_parsed:
        # No S/E markers anywhere: season 1, numbered by file name.
        eps.sort(key=lambda x: x["_name"].lower())
        for i, ep in enumerate(eps, start=1):
            ep["season_number"] = 1
            ep["episode_number"] = i
    else:
        for ep in eps:
            if ep["season_number"] is None:
                ep["season_number"] = 1
            if ep["episode_number"] is None:
                ep["episode_number"] = 999
    eps.sort(key=lambda x: (x["season_number"], x["episode_number"], x["_name"].lower()))
    for ep in eps:
        ep.pop("_name", None)
    return eps


def get_library_item(token: str) -> Optional[Dict]:
    """A title with its seasons (show) or play token (movie), or None when the
    token is not inside an enabled root. Offline and blocking."""
    budget = _ProbeBudget()
    real_dir = safe_resolve_dir(token)
    if real_dir:
        item = _build_title(real_dir, True, budget)
        if not item:
            return None
        if item["media_kind"] == "movie":
            files = _walk_playable(real_dir)
            play_file = max(files, key=_size_or_zero) if files else None
            item["play"] = {"id": encode_token(play_file)} if play_file else None
            item["seasons"] = []
        else:
            eps = _episodes_for(real_dir)
            by_season: Dict[int, List[Dict]] = {}
            for ep in eps:
                by_season.setdefault(ep["season_number"], []).append(ep)
            item["seasons"] = [
                {"season_number": s, "episodes": by_season[s]}
                for s in sorted(by_season)
            ]
            item["play"] = None
        return item

    real_file = safe_resolve(token)
    if real_file:
        item = _build_title(real_file, False, budget)
        if not item:
            return None
        item["play"] = {"id": encode_token(real_file)}
        item["seasons"] = []
        return item

    return None


def browse_dir(token: Optional[str] = None) -> Optional[Dict]:
    """The children of one directory, or the enabled roots when ``token`` is empty,
    so media that never resolves to a title is still reachable. None when the token
    is not inside an enabled root. Blocking.

    | type     | is                  | carries                                  |
    |----------|---------------------|------------------------------------------|
    | title    | a show/movie folder | the list-item shape, for /local-overview |
    | folder   | a container         | an ``id`` to browse again                |
    | file     | a loose file        | an ``id`` for /watch-local               |
    """
    budget = _ProbeBudget()

    if not token:
        entries: List[Dict] = []
        for r in store.enabled_roots_config():
            path = r["path"]
            if not os.path.isdir(path):
                continue
            entries.append({
                "type": "folder",
                "id": encode_token(os.path.realpath(path)),
                "name": r.get("label") or os.path.basename(path.rstrip(os.sep)) or path,
            })
        return {"token": None, "name": "Local", "parent": None, "entries": entries}

    real_dir = safe_resolve_dir(token)
    if not real_dir:
        return None

    entries = []
    try:
        names = sorted(os.listdir(real_dir))
    except OSError:
        names = []
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(real_dir, name)
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            continue
        if is_dir:
            cls = _classify_dir(full)
            if cls == _TITLE:
                try:
                    item = _build_title(full, True, budget)
                except Exception:
                    item = None
                if item:
                    entries.append({"type": "title", **item})
            elif cls == _CONTAINER:
                entries.append({
                    "type": "folder",
                    "id": encode_token(os.path.realpath(full)),
                    "name": name,
                })
        elif is_playable_path(full):
            entries.append({
                "type": "file",
                "id": encode_token(os.path.realpath(full)),
                "name": name,
                "media_kind": "movie",
            })

    # From a root, "up" goes to the synthetic list of roots.
    roots_real = {os.path.realpath(r) for r in store.enabled_roots()}
    parent = None if os.path.realpath(real_dir) in roots_real else encode_token(
        os.path.realpath(os.path.dirname(real_dir))
    )
    return {
        "token": token,
        "name": os.path.basename(real_dir.rstrip(os.sep)) or real_dir,
        "source_label": source_label_for(real_dir),
        "parent": parent,
        "entries": entries,
    }


def search_library(query: str, items: Optional[List[Dict]] = None, limit: int = 20) -> List[Dict]:
    """Title substring match. The route passes its cached scan as ``items``."""
    q = _norm(query)
    if not q:
        return []
    pool = items if items is not None else scan_library()
    out: List[Dict] = []
    for it in pool:
        if q in _norm(it.get("title") or ""):
            out.append(it)
            if len(out) >= limit:
                break
    return out

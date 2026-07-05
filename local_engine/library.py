"""
Browsable index of the operator's local media library.

The `local_scraper`/`local` resolver only answer a *targeted* request ("give me
S2E4 of this TMDB title"). This module is the other half: it walks the enabled
source roots and turns whatever is actually on disk into a browsable, searchable
catalogue of **titles** — so local media surfaces in the Index (its own "Local"
view) and in search, even for files that map to no TMDB/AniList entry at all.

Each top-level folder under a root is one title (a show or a movie); a loose media
file sitting directly in a root is a single-file movie title. For each title we
resolve display metadata in a strict precedence, richest first:

  1. **.nfo**            — Kodi/Jellyfin sidecar XML (tvshow.nfo / movie.nfo /
                           ``<stem>.nfo``): title, year, plot, genres, and any
                           ``uniqueid`` (tmdb/imdb/anilist).
  2. **sidecar .json**   — a ``<stem>.json`` / ``metadata.json`` / ``crimson.json``
                           with title/year/overview/genres/poster/tmdb_id.
  3. **embedded tags**   — ffprobe's container ``title``/``show`` format tags
                           (bounded + cached; only when 1+2 are absent).
  4. **filename parse**  — the fallback: a cleaned folder/file name + a year pulled
                           from it. This is what "index the non-metadata parts by
                           filename" means.

Artwork (poster/folder/cover/fanart images, or a ``<stem>.jpg``) is surfaced as a
signed ``/local_art`` URL. Titles that fell all the way to (4) — and carry no
tmdb_id — are additionally enriched *live* against TMDB at overview time (best
effort, confident hits only) to borrow a poster/overview/genres. That enrichment
is cached, so the list view can reuse it without fanning out one TMDB call per
title.

The identity of a title/episode is the same opaque base64url path token the rest
of local_engine uses (see fs.encode_token): a directory token for a title, a file
token for an episode/movie. Playback reuses the existing ``crimson-local:``
resolver, so nothing here touches the byte path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from . import fs
from .fs import (
    ART_EXTENSIONS,
    art_proxy_url,
    encode_token,
    is_playable_path,
    is_web_playable_path,
    safe_resolve,
    safe_resolve_dir,
    source_label_for,
)

logger = logging.getLogger("local_engine.library")

# Reuse the fs-level store instance so a single monkeypatch of its enabled-roots
# config (tests) — and the shared process-wide cache — covers scan + playability +
# labels alike.
_store = fs._store

# Bounds so registering a huge NAS can never hang a scan / a single title's walk.
_MAX_TITLES = 4000            # total titles across all roots
_MAX_FILES_PER_TITLE = 1500   # files walked inside one title before we stop
_EMBED_PROBE_BUDGET = 200     # max ffprobe calls for embedded tags per full scan

# Sidecar metadata file names (besides ``<stem>.nfo``/``<stem>.json``) looked for
# at a title's folder root.
_SHOW_NFO_NAMES = ("tvshow.nfo",)
_MOVIE_NFO_NAMES = ("movie.nfo",)
_JSON_NAMES = ("metadata.json", "crimson.json")
# Artwork base names (any ART_EXTENSIONS extension) looked for at a folder root.
_ART_BASENAMES = ("poster", "folder", "cover", "default", "movie", "show", "banner", "fanart")


# --- filename / folder-name parsing -----------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# Release-junk we strip out of a raw folder/file name to recover a display title.
_JUNK_TOKENS = re.compile(
    r"\b(1080p|2160p|720p|480p|4k|uhd|hdr|x264|x265|h264|h265|hevc|aac|ac3|dts|"
    r"bluray|blu-ray|bdrip|brrip|webrip|web-dl|webdl|hdtv|dvdrip|remux|proper|"
    r"repack|multi|dual|dubbed|subbed|complete|season|staffel)\b",
    re.I,
)
_YEAR_RE = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)")


def _clean_title(raw: str) -> str:
    """Turn a raw folder/file base name into a human title: drop bracketed groups,
    release junk and separators, collapse dots/underscores to spaces, Title-Case-ish
    preserved from the source."""
    name = raw or ""
    name = re.sub(r"\.[a-z0-9]{2,4}$", "", name, flags=re.I)  # trailing extension
    name = re.sub(r"[\[(\{].*?[\])\}]", " ", name)            # [1080p], (2021), {grp}
    name = name.replace("_", " ").replace(".", " ")
    name = _JUNK_TOKENS.sub(" ", name)
    # Drop a trailing release group after a dash ("Show - GROUP") and dangling seps.
    name = re.sub(r"[-–—]\s*[A-Za-z0-9]+\s*$", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -–—·.")
    return name or (raw or "").strip()


def _parse_year(raw: str) -> Optional[int]:
    m = _YEAR_RE.search(raw or "")
    if not m:
        return None
    y = int(m.group(1))
    return y if 1900 <= y <= 2100 else None


# The server-side Cache (cache_engine.fs.plan_rel_path) writes titles into folders
# whose NAME is a TMDB id, not a human title:
#   TV    -> ``tmdb-<id>/S<ss>E<ee>[ - <language>].<ext>``
#   movie -> ``movie-tmdb-<id>/movie[ - <language>].<ext>``
# Recognise those so a cached library shows real titles (resolved from that id) and a
# correct kind — instead of every folder collapsing to "tmdb".
_TMDB_DIR_RE = re.compile(r"^(movie-)?tmdb[-_](\d+)$", re.I)


def _tmdb_from_dirname(name: str):
    """``(tmdb_id, media_kind)`` if ``name`` is a TMDB-id-encoding folder (the Cache's
    naming), else ``(None, None)``."""
    m = _TMDB_DIR_RE.match((name or "").strip())
    if not m:
        return None, None
    return int(m.group(2)), ("movie" if m.group(1) else "show")


# Season/episode parsed from a filename (mirrors the scraper, kept local so the
# library has no scraper import).
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


# --- metadata sources (nfo / json / embedded) -------------------------------
def _first_text(root: ET.Element, tags: Tuple[str, ...]) -> Optional[str]:
    for tag in tags:
        el = root.find(tag)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
    return None


def _parse_nfo(path: str) -> Optional[Dict]:
    """Parse a Kodi/Jellyfin .nfo into our metadata dict, or None on any trouble.
    Handles ``movie``/``tvshow``/``episodedetails`` roots."""
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

    genres = [g.text.strip() for g in root.findall("genre") if (g.text or "").strip()]
    if genres:
        meta["genres"] = genres

    # uniqueid: <uniqueid type="tmdb">123</uniqueid> (+ legacy <tmdbid>/<id>).
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

    # episode nfo (for per-episode titles).
    if tag == "episodedetails":
        se = _first_text(root, ("season",))
        ep = _first_text(root, ("episode",))
        meta["season"] = int(se) if se and se.isdigit() else None
        meta["episode"] = int(ep) if ep and ep.isdigit() else None

    return meta if meta.get("title") or tag == "episodedetails" else meta or None


def _parse_sidecar_json(path: str) -> Optional[Dict]:
    """Parse a flexible ``<stem>.json`` / metadata.json sidecar, or None."""
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
        meta["poster"] = poster  # a remote poster URL is used as-is
    tmdb = pick("tmdb_id", "tmdbId", "tmdbid")
    if isinstance(tmdb, int):
        meta["tmdb_id"] = tmdb
    elif isinstance(tmdb, str) and tmdb.isdigit():
        meta["tmdb_id"] = int(tmdb)
    kind = pick("kind", "type", "media_kind")
    if isinstance(kind, str) and kind.lower() in ("movie", "show", "tv"):
        meta["media_kind"] = "show" if kind.lower() == "tv" else kind.lower()
    return meta if meta.get("title") else None


def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


# Bounded (path, mtime, size) -> tags cache so re-scans don't re-probe files.
_EMBED_CACHE: Dict[tuple, Dict] = {}
_EMBED_CACHE_MAX = 4096


def _embedded_meta(path: str) -> Optional[Dict]:
    """Container format tags (title/show/date) via ffprobe, or None. Memoised by
    (path, mtime, size); blocking, so call off the event loop."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, int(st.st_mtime), st.st_size)
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key] or None
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
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        _EMBED_CACHE.clear()
    _EMBED_CACHE[key] = tags
    return tags or None


# --- artwork ----------------------------------------------------------------
def _find_artwork(dir_path: str, stem: Optional[str] = None) -> Optional[str]:
    """Absolute path of a poster-ish image in ``dir_path``.

    For a loose file (``stem`` given) only ``<stem>.<img>`` counts — a shared
    ``poster.jpg`` sitting in a root must NOT attach to every loose file there. For
    a folder title (``stem`` None) the conventional poster/folder/cover names apply.
    """
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


# --- media file discovery ---------------------------------------------------
def _walk_playable(dir_path: str, cap: int = _MAX_FILES_PER_TITLE) -> List[str]:
    """Every playable media file under ``dir_path`` (bounded), respecting the
    encoding gate (mkv/avi only when the source has encoding on) via is_playable_path."""
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


def _largest(paths: List[str]) -> Optional[str]:
    best, best_size = None, -1
    for p in paths:
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = 0
        if sz > best_size:
            best, best_size = p, sz
    return best


# --- title assembly ---------------------------------------------------------
def _resolve_metadata(dir_path: str, media_files: List[str], stem: Optional[str],
                      is_movie_hint: bool, embed_budget: List[int]) -> Dict:
    """Apply the nfo -> json -> embedded -> filename precedence for one title.
    ``embed_budget`` is a 1-element list used as a mutable counter so a full scan
    can cap total ffprobe calls."""
    base_name = stem if stem is not None else os.path.basename(dir_path.rstrip(os.sep))
    meta: Dict = {}

    # 1) .nfo
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

    # 2) sidecar json
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

    # 3) embedded container tags (budgeted; only when 1+2 gave nothing)
    if not meta.get("title") and embed_budget[0] > 0 and _ffprobe_available():
        rep = _largest(media_files) if media_files else None
        if rep:
            embed_budget[0] -= 1
            emb = _embedded_meta(rep)
            if emb and emb.get("title"):
                meta = dict(emb)

    # 4) TMDB-id-encoding folder (the server-side Cache's tmdb-<id>/ dirs). Only a
    #    folder title's name can be an id — a loose file (stem set) is never one.
    tmdb_dir_id, tmdb_dir_kind = _tmdb_from_dirname(base_name) if stem is None else (None, None)

    # 5) filename / folder-name parse — the floor, but NOT for an id folder (whose
    #    "name" is an id, not a title): leave the title to TMDB resolution.
    if not meta.get("title"):
        if tmdb_dir_id:
            meta = {"source": "tmdb-dir"}
        else:
            meta = {"source": "filename", "title": _clean_title(base_name)}
    if tmdb_dir_id:
        meta["tmdb_id"] = meta.get("tmdb_id") or tmdb_dir_id
        # The tmdb-/movie-tmdb- prefix is the authority on kind for a cached title.
        meta["media_kind"] = tmdb_dir_kind
    if not meta.get("year"):
        meta["year"] = _parse_year(base_name)
    if "media_kind" not in meta:
        meta["media_kind"] = "movie" if is_movie_hint else "show"
    # An id folder is authoritative metadata (it carries a real TMDB id), so it is
    # NOT flagged as a filename-only guess.
    meta["has_metadata"] = meta.get("source") in ("nfo", "json", "embedded", "tmdb-dir")
    return meta


def _build_title(entry_path: str, is_dir: bool, embed_budget: List[int]) -> Optional[Dict]:
    """Build one list item for a title (a folder, or a loose media file), or None
    when it holds nothing playable."""
    if is_dir:
        dir_path = entry_path
        stem = None
        media_files = _walk_playable(dir_path)
        if not media_files:
            return None
        # Movie vs show: a single playable file with no season/episode markers is a
        # movie; a movie.nfo or exactly one file also leans movie.
        parsed_eps = [se for se in (_parse_se(os.path.basename(f)) for f in media_files) if se[1] is not None]
        has_movie_nfo = any(os.path.isfile(os.path.join(dir_path, n)) for n in _MOVIE_NFO_NAMES)
        is_movie = has_movie_nfo or (len(media_files) == 1 and not parsed_eps)
        art_dir, art_stem = dir_path, None
        rep_for_token = dir_path
    else:
        # loose media file directly under a root -> single-file movie title
        dir_path = os.path.dirname(entry_path)
        stem = os.path.splitext(os.path.basename(entry_path))[0]
        media_files = [entry_path]
        is_movie = True
        art_dir, art_stem = dir_path, stem
        rep_for_token = entry_path

    meta = _resolve_metadata(dir_path, media_files, stem, is_movie, embed_budget)

    art_path = _find_artwork(art_dir, art_stem)
    poster = meta.get("poster") or (art_proxy_url(art_path) if art_path else None)

    # Metadata resolution may have corrected the kind (e.g. a cache tmdb-<id> TV
    # folder with a single cached episode is still a show), so trust it over the
    # early file-count heuristic when deciding movie-vs-show shape.
    final_kind = meta.get("media_kind") or ("movie" if is_movie else "show")
    final_is_movie = final_kind == "movie"
    # Title floor: a real title, else a per-id placeholder ("TMDB 280042") the route
    # replaces via enrichment — never the bare "tmdb" folder name.
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


# --- public API -------------------------------------------------------------
def scan_library() -> List[Dict]:
    """Scan every enabled source root into a flat list of title items (offline —
    no external calls). Blocking disk I/O; call via run_in_threadpool. Sorted by
    title. Bounded by _MAX_TITLES so a huge NAS can't blow up the response."""
    items: List[Dict] = []
    embed_budget = [_EMBED_PROBE_BUDGET]
    seen_ids: set = set()
    for root in _store.enabled_roots():
        try:
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                if name.startswith("."):
                    continue
                full = os.path.join(root, name)
                is_dir = os.path.isdir(full)
                is_loose_media = (not is_dir) and is_web_playable_path(full)
                # Non-web loose files (mkv) only when their source has encoding on.
                if not is_dir and not is_loose_media:
                    if is_playable_path(full):
                        is_loose_media = True
                    else:
                        continue
                try:
                    item = _build_title(full, is_dir, embed_budget)
                except Exception as e:
                    logger.warning(f"[library] failed to build title for {full!r}: {e}")
                    item = None
                if not item:
                    continue
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                items.append(item)
                if len(items) >= _MAX_TITLES:
                    logger.warning(f"[library] hit _MAX_TITLES={_MAX_TITLES}; truncating scan")
                    items.sort(key=lambda x: (x["title"] or "").lower())
                    return items
        except Exception as e:
            logger.warning(f"[library] could not scan root {root!r}: {e}")
    items.sort(key=lambda x: (x["title"] or "").lower())
    return items


def _episodes_for(dir_path: str) -> List[Dict]:
    """Ordered episode descriptors for a show title. Each carries the file token the
    /watch-local route plays."""
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
        # per-episode title: <file>.nfo <title>, else cleaned filename
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
        # No S/E markers anywhere — present the files as season 1, numbered by name.
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
    """Full detail for one title (offline): metadata + episodes (show) or a single
    play descriptor (movie). Returns None when the token doesn't resolve to a title
    inside a currently enabled root. Blocking; call via run_in_threadpool."""
    embed_budget = [_EMBED_PROBE_BUDGET]
    real_dir = safe_resolve_dir(token)
    if real_dir:
        item = _build_title(real_dir, True, embed_budget)
        if not item:
            return None
        if item["media_kind"] == "movie":
            play_file = _largest(_walk_playable(real_dir))
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

    # A loose single-file movie title (file token).
    real_file = safe_resolve(token)
    if real_file:
        item = _build_title(real_file, False, embed_budget)
        if not item:
            return None
        item["play"] = {"id": encode_token(real_file)}
        item["seasons"] = []
        return item

    return None


def search_library(query: str, items: Optional[List[Dict]] = None, limit: int = 20) -> List[Dict]:
    """Filter the scanned library by a title / filename substring. ``items`` may be
    a pre-scanned list (the route passes its cached list to avoid re-walking disk)."""
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

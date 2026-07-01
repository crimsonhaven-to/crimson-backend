"""
ShowBox / Febbox resolver (TMDB-title-keyed direct-file source).

Unlike the embed-host sources (VOE, Vidmoly) this is a cloud **file-host**:
ShowBox indexes a title and hands back a Febbox *file share* (a folder of the
actual video files), and Febbox's player endpoint returns **direct mp4 links**
per quality. So there's no third-party player UI, no ad iframe and no rotating
obfuscation to chase — just direct files, exactly the durable shape the
Jellyfin / PlayIMDb / cinema.bz sources already use.

Division of labour (see ``scrapers/showbox_scraper.py`` for the first half):

    scraper  -> showbox.media search/slug  -> /{tv}/detail/{id}
             -> showbox.media/index/share_link -> febbox share_key
             -> febbox file_share_list (season folder -> episode file)
             -> crimson-febbox:{share_key}:{fid}        (this resolver's marker)

    resolver -> POST febbox.com/file/player  (fid + share_key, Cookie: ui=<token>)
             -> parse ``var sources = [...]`` -> pick best-quality direct mp4
             -> /febbox_proxy?u=<mp4>&s=<sig>            (type mp4 / hls)
    frontend -> CrimsonPlayer plays it like any other source (no iframe).

The make-or-break: everything up to ``/file/player`` is **open** (no auth), but
that final call returns ``{"code":-1,"msg":"please login"}`` without a logged-in
Febbox account cookie. So this whole source is **gated on FEBBOX_UI_TOKEN** — the
``ui`` cookie value from a febbox.com session (grab it from your browser's cookies
on febbox.com after logging in). With it unset the source disables itself and
silently never surfaces, exactly like the Jellyfin source without JELLYFIN_URL.

Why a proxy (not direct play): the ``var sources`` URLs live on a rotating Febbox
OSS CDN; proxying keeps playback same-origin (no CORS surprises), survives host
rotation and gives us Range passthrough for seeking. As with the cinema.bz /
PlayIMDb proxies the CDN host rotates, so every proxied URL is **HMAC-signed** and
the proxy refuses anything unsigned (closes the open-relay / SSRF hole). See
[[playimdb-source]] / [[cinemabz-source]] for the signed-HLS-proxy pattern this
mirrors and [[jellyfin-source]] for the env-gated extracted-stream shape.
"""

import hashlib
import hmac
import json
import logging
import os
import re
from typing import AsyncIterator, List, Optional, Tuple, Union
from urllib.parse import quote, urljoin, urlparse

import httpx
from curl_cffi.requests import AsyncSession

from .base_resolver import BaseResolver
from ._proxy_secret import resolve_secret as _resolve_proxy_secret
from ._ssrf_guard import guarded_client

logger = logging.getLogger(__name__)

# --- Routing ---------------------------------------------------------------
PROXY_PREFIX = "/febbox_proxy"
# The scraper emits "crimson-febbox:{share_key}:{fid}"; this resolver matches on
# the prefix. share_key is mixed-case alnum + hyphens, fid is digits.
EMBED_MARKER = "crimson-febbox"

FEBBOX_BASE = "https://www.febbox.com"
FEBBOX_PLAYER_URL = f"{FEBBOX_BASE}/file/player"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Quality preference, best first. Febbox now serves HLS: the player payload lists
# an "AUTO" adaptive master alongside fixed-resolution renditions ("4K"/"1080p"/…),
# so "auto" leads (adaptive picks the best rung the connection sustains — the safe
# default) and the numeric tiers follow. "org" stays for older direct-file shares.
_QUALITY_ORDER = ("auto", "org", "4k", "2160", "1440", "1080", "720", "480", "360", "240")

# Febbox's HLS payload also lists audio-only renditions ("audio_1", "audio_1_eac3",
# …) as `sources[]` entries next to the video variants. They must never surface as a
# playable quality tile (selecting one plays sound with no picture), so we drop any
# entry whose label carries an audio/codec marker that never appears in a real
# video-quality label.
_AUDIO_ONLY_MARKERS = ("audio", "aac", "eac3", "ac3", "dts", "opus", "flac", "dolby", "atmos")

# How many quality variants to surface per episode (best-first; the cap drops the
# lowest). Febbox typically returns 3-6; this just guards a pathological response.
_MAX_VARIANTS = 6

# Pretty display per normalised quality token — the suffix in the source label
# ("ShowBox (1080p)"). Unmatched labels keep their raw text (see _quality_display).
_QUALITY_DISPLAY = {
    "auto": "Auto",
    "org": "Original",
    "4k": "4K",
    "2160": "4K",
    "1440": "1440p",
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
    "240": "240p",
}


def _ui_token() -> Optional[str]:
    """The febbox ``ui`` session cookie value (FEBBOX_UI_TOKEN), or None."""
    tok = (os.getenv("FEBBOX_UI_TOKEN") or "").strip()
    # Tolerate the user pasting the whole "ui=<token>" cookie pair.
    if tok.startswith("ui="):
        tok = tok[3:]
    return tok or None


def _region() -> str:
    """Febbox OSS region used in the cookie (some shares are region-gated)."""
    return (os.getenv("FEBBOX_REGION") or "USA7").strip()


def is_configured() -> bool:
    """True only when a FEBBOX_UI_TOKEN is set. Without it the player call 401s
    (``please login``), so the whole ShowBox/Febbox source disables itself —
    the scraper short-circuits and no dead tile ever surfaces."""
    return _ui_token() is not None


# --- Proxy URL signing -----------------------------------------------------
# The proxy fetches rotating Febbox OSS hosts, so an unsigned proxy would be an
# open relay / SSRF vector. Every proxied URL carries an HMAC the proxy verifies
# before fetching. The secret must be stable + shared across replicas (a link
# minted by one replica is verified by whichever replica the player's next
# request lands on); see resolvers._proxy_secret.
_SECRET = _resolve_proxy_secret("FEBBOX_PROXY_SECRET")


def _sign(url: str) -> str:
    return hmac.new(_SECRET, url.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _verify(url: str, sig: str) -> bool:
    return bool(url) and hmac.compare_digest(_sign(url), sig or "")


def _proxy_path_for(upstream_url: str) -> str:
    """Same-origin, signed proxy path for an absolute upstream URL."""
    return f"{PROXY_PREFIX}?u={quote(upstream_url, safe='')}&s={_sign(upstream_url)}"


# --- HLS playlist rewriting ------------------------------------------------
# Febbox normally serves progressive mp4, but some qualities come back as HLS;
# handle both so an m3u8 still flows wholly through our origin.
def _is_playlist_url(url: str) -> bool:
    return ".m3u8" in urlparse(url).path.lower()


def _is_subtitle_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".srt") or path.endswith(".vtt")


def _srt_to_vtt(text: str) -> str:
    """Minimal SRT -> WebVTT: prepend the header and dot-separate the millisecond
    field in cue timings (``00:00:01,200`` -> ``00:00:01.200``). SRT numeric
    indices are left as-is — WebVTT accepts them as cue identifiers. Browsers'
    <track> only speak WebVTT, so this runs in the proxy for every .srt we serve."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = ["WEBVTT", ""]
    for line in text.split("\n"):
        if "-->" in line:
            line = line.replace(",", ".")
        out.append(line)
    return "\n".join(out)


def rewrite_playlist(text: str, base_url: str) -> str:
    """Rewrite an HLS m3u8 so every sub-resource is absolutized against
    ``base_url`` and routed back through this signed proxy."""
    def _route(raw: str) -> str:
        return _proxy_path_for(urljoin(base_url, raw.strip()))

    out: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            if 'URI="' in stripped:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: 'URI="' + _route(m.group(1)) + '"',
                    line,
                )
            out.append(line)
        else:
            out.append(_route(stripped))
    return "\n".join(out)


# --- The proxy fetch (used by the api.py route) ----------------------------
async def proxy_fetch(
    url: Optional[str],
    sig: Optional[str],
    range_header: Optional[str] = None,
) -> Tuple[int, str, dict, Union[bytes, AsyncIterator[bytes]]]:
    """Fetch a signed upstream Febbox file URL and return
    ``(status_code, content_type, forward_headers, body)``.

    ``body`` is rewritten ``bytes`` for HLS playlists and an async byte-iterator
    for media (mp4 / segments), streamed through with Range passthrough so
    seeking works. Raises ``ValueError`` for a missing/invalid signature (keeps
    the proxy from being an open relay)."""
    if not url or not url.startswith("https://"):
        raise ValueError("Missing or non-https upstream URL")
    if not _verify(url, sig):
        raise ValueError("Bad or missing signature")

    req_headers = {"User-Agent": UA, "Accept": "*/*"}
    if range_header:
        req_headers["Range"] = range_header

    # No read timeout: media files are large.
    client = guarded_client(
        headers=req_headers,
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=None),
    )
    req = client.build_request("GET", url)
    resp = await client.send(req, stream=True)
    content_type = resp.headers.get("content-type", "application/octet-stream")

    # Subtitles: read fully and (for .srt) convert to WebVTT so the browser's
    # <track> can use them. Served same-origin from here, so no CORS dance.
    if _is_subtitle_url(url):
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        body = raw.decode("utf-8", errors="replace")
        if urlparse(url).path.lower().endswith(".srt"):
            body = _srt_to_vtt(body)
        return resp.status_code, "text/vtt; charset=utf-8", {}, body.encode("utf-8")

    if _is_playlist_url(url):
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        text = rewrite_playlist(raw.decode("utf-8", errors="replace"), url)
        return resp.status_code, "application/vnd.apple.mpegurl", {}, text.encode("utf-8")

    forward = {
        h: resp.headers[h]
        for h in ("content-range", "accept-ranges", "content-length", "cache-control")
        if h in resp.headers
    }

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return resp.status_code, content_type, forward, body_iter()


# --- Febbox player lookup --------------------------------------------------
def _parse_marker(embed_url: str) -> Optional[Tuple[str, str, Optional[int], Optional[int]]]:
    """Parse a ``crimson-febbox:{share_key}:{fid}[:{season}:{episode}]`` marker
    into ``(share_key, fid, season, episode)``, or None if it isn't ours.

    share_key keeps its original case (resolve_streams only lowercases for the
    keyword *match*); season/episode are optional (absent for movies)."""
    parts = embed_url.split(":")
    if len(parts) < 3 or parts[0] != EMBED_MARKER:
        logger.warning(f"[febbox] unrecognised marker: {embed_url}")
        return None
    share_key, fid = parts[1], parts[2]
    season = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    episode = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
    return share_key, fid, season, episode


def _is_audio_only(label: str) -> bool:
    """True for Febbox's audio-only HLS renditions ("audio_1", "audio_1_eac3", …).
    These ride in the same ``var sources`` array as the video variants; surfacing
    one as a quality tile would play sound with no picture, so they're filtered out
    before ranking. The markers never occur in a real video-quality label."""
    low = (label or "").lower()
    return any(m in low for m in _AUDIO_ONLY_MARKERS)


def _quality_token(label: str) -> Optional[str]:
    """The first _QUALITY_ORDER token present in a febbox source label, or None."""
    low = (label or "").lower()
    for tok in _QUALITY_ORDER:
        if tok in low:
            return tok
    return None


def _quality_rank(label: str) -> int:
    """Rank a febbox source label; lower = better (earlier in _QUALITY_ORDER)."""
    tok = _quality_token(label)
    return _QUALITY_ORDER.index(tok) if tok is not None else len(_QUALITY_ORDER)


def _quality_display(label: str) -> str:
    """Human display for a source label's quality ("1080P" -> "1080p", "ORG" ->
    "Original"); an unrecognised label is returned cleaned (e.g. "HD"), or "SD"."""
    tok = _quality_token(label)
    if tok is not None:
        return _QUALITY_DISPLAY.get(tok, tok)
    return (label or "").strip() or "SD"


def _source_label(quality: str) -> str:
    """Per-quality display label. The frontend groups providers by splitting on
    " · " / " (", so "ShowBox (1080p)" collapses under one "ShowBox" card. Kept
    identical across resolve() (/watch proxy) and resolve_direct() (/resolve grant)
    so the two paths dedupe by (source, language)."""
    return f"ShowBox ({quality})" if quality else "ShowBox"


async def fetch_player_html(share_key: str, fid: str) -> Optional[str]:
    """POST the Febbox player endpoint for one file and return its raw HTML
    (which carries both the ``var sources`` quality list and the subtitle
    ``<li data-url=…>`` panel), or None.

    Requires FEBBOX_UI_TOKEN — without the ``ui`` cookie the endpoint answers
    ``{"code":-1,"msg":"please login"}`` and we get nothing."""
    token = _ui_token()
    if not token:
        return None

    headers = {
        "Cookie": f"ui={token}; oss_group={_region()}",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": FEBBOX_BASE,
        "Referer": f"{FEBBOX_BASE}/share/{share_key}",
        "User-Agent": UA,
        "Accept": "*/*",
    }

    # curl_cffi (TLS impersonation) for the API call — febbox fingerprints plain
    # clients; the streaming proxy above stays on guarded httpx for SSRF safety.
    session = AsyncSession(impersonate="chrome", timeout=20.0, allow_redirects=True)
    try:
        resp = await session.post(
            FEBBOX_PLAYER_URL, data={"fid": str(fid), "share_key": share_key}, headers=headers
        )
        return resp.text
    finally:
        await session.close()


def _rank_sources(html: str, fid: str) -> List[Tuple[str, str]]:
    """Parse a player response's ``var sources = [...]`` array into a best-first list
    of ``(file_url, quality_display)`` — ONE entry per distinct quality (the best
    file per quality bucket), capped at ``_MAX_VARIANTS``.

    Febbox returns every transcode of a file (ORG/4K/1080P/720P/…). The resolver
    used to keep only the single best; surfacing each as its own tile gives a manual
    quality picker (lower rungs help slow links / save data), matching what other
    aggregators show per episode."""
    match = re.search(r"var\s+sources\s*=\s*(\[.*?\])\s*;", html, re.S)
    if not match:
        # Surface the login wall / region error explicitly — it's the usual cause.
        try:
            msg = json.loads(html).get("msg")
            logger.warning(f"[febbox] no sources for fid {fid}: {msg!r}")
        except (json.JSONDecodeError, AttributeError):
            logger.warning(f"[febbox] no sources for fid {fid} (unparseable response)")
        return []
    try:
        sources = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning(f"[febbox] could not parse sources array for fid {fid}")
        return []

    candidates = [
        s for s in sources
        if isinstance(s, dict) and isinstance(s.get("file"), str)
        and s["file"].startswith("http") and not _is_audio_only(s.get("label", ""))
    ]
    if not candidates:
        return []
    candidates.sort(key=lambda s: _quality_rank(s.get("label", "")))

    out: List[Tuple[str, str]] = []
    seen: set = set()
    for s in candidates:
        label = s.get("label", "")
        # Dedupe by quality bucket: two "1080P" entries collapse, ORG vs 1080P stay
        # distinct. Unknown labels dedupe on their raw text so they don't all merge.
        key = _quality_token(label) or label.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((s["file"], _quality_display(label)))
        if len(out) >= _MAX_VARIANTS:
            break
    logger.info(f"[febbox] fid {fid}: surfacing {[q for _, q in out]} "
                f"of {[c.get('label') for c in candidates]}")
    return out


# Cap on how many subtitle tracks we surface (one per language, ordered).
_MAX_SUBTITLES = 12
# Languages float to the top of the track list (anime is JPN audio + EN subs).
_PREFERRED_LANGS = ("english", "german", "spanish", "french", "portuguese (br)")


def _episode_patterns(season: int, episode: int) -> List[re.Pattern]:
    """Filename patterns that mark a subtitle as belonging to this episode.
    Febbox returns the *whole show's* subtitle pool per file, so we must keep
    only the ones for the episode being watched."""
    s, e = season, episode
    return [
        re.compile(rf"s0*{s}[._ -]?e0*{e}(?!\d)", re.I),   # S01E01 / s1.e1
        re.compile(rf"(?:^|[^\d])e0*{e}(?!\d)", re.I),       # E01
        re.compile(rf"[ ._-]0*{e}[ ._-].*\.(?:srt|vtt)$", re.I),  # " - 01 " anime style
        re.compile(rf"\b0*{e}(?:en|eng)?\.(?:srt|vtt)$", re.I),   # "01en.srt"
    ]


def _parse_subtitles(html: str, season: int, episode: int) -> List[dict]:
    """Extract subtitle tracks from the player HTML for this episode.

    Each ``<li data-lang data-language data-url=…><p>filename</p>`` entry is a
    candidate; we keep those whose filename matches the target episode, dedupe to
    one track per language, and order with the common languages first. Returns
    ``[{"label","lang","url"}]`` with raw (unproxied) .srt URLs — the caller wraps
    them in the signed proxy (which converts srt -> vtt on the way out)."""
    if season is None or episode is None:
        return []
    pats = _episode_patterns(season, episode)
    # <li ...attrs...><p ...>filename</p>
    entry_re = re.compile(r"<li\b([^>]*)>\s*<p[^>]*>([^<]*)</p>", re.I)
    out: List[dict] = []
    seen_langs: set = set()
    for attrs, fname in entry_re.findall(html):
        url_m = re.search(r'data-url="([^"]+)"', attrs)
        if not url_m:
            continue
        url = url_m.group(1)
        if not _is_subtitle_url(url):
            continue
        name = fname.strip()
        if not any(p.search(name) for p in pats):
            continue  # wrong episode (the pool mixes them)
        lang_m = re.search(r'data-language="([^"]*)"', attrs)
        language = (lang_m.group(1).strip() if lang_m else "") or "Unknown"
        code_m = re.search(r'data-lang="([^"]*)"', attrs)
        code = (code_m.group(1).strip().lower() if code_m else language[:2].lower())
        # Dedupe on the language CODE, not the display label: uploaders sometimes
        # tag the same language inconsistently ("English" vs "En"), and the code
        # is the stable key that collapses those into one track.
        key = code or language.lower()
        if key in seen_langs:
            continue  # one track per language (first decent match wins)
        seen_langs.add(key)
        out.append({"label": language, "lang": code, "url": url})

    def _rank(track: dict) -> int:
        try:
            return _PREFERRED_LANGS.index(track["label"].lower())
        except ValueError:
            return len(_PREFERRED_LANGS)

    out.sort(key=_rank)
    return out[:_MAX_SUBTITLES]


class FebboxResolver(BaseResolver):
    """Resolves a ``crimson-febbox:{share_key}:{fid}:{season}:{episode}`` marker to
    a backend-proxied direct file (ShowBox/Febbox source), plus subtitle tracks.

    Disabled (returns None) unless FEBBOX_UI_TOKEN is set. On success returns a
    ``{"url", "subtitles"}`` dict — ``url`` is a signed ``/febbox_proxy`` path
    (tagged hls/mp4 by api.py, not a /player iframe) and ``subtitles`` is a list of
    ``{"label","lang","url"}`` (each url a signed proxy path serving WebVTT).
    Returns None when the file can't be unlocked (login wall, region gate, expired
    share)."""

    domain_keyword: str = EMBED_MARKER
    source_name: str = "ShowBox"

    async def _unlock(self, embed_url: str) -> Optional[Tuple[List[Tuple[str, str]], List[dict]]]:
        """Shared token-gated lookup behind both resolve paths: marker -> Febbox
        player HTML -> ``([(direct_url, quality_display), …], subtitle_tracks)`` —
        one entry per distinct quality, best-first. The ``ui`` cookie
        (FEBBOX_UI_TOKEN, a C5 secret) is used HERE and nowhere downstream, so the
        secret never has to reach the client. Subtitle ``url`` is the RAW .srt link
        (callers wrap it in the signed proxy). Returns None when locked/unfound."""
        if not is_configured():
            return None
        parsed = _parse_marker(embed_url)
        if not parsed:
            return None
        share_key, fid, season, episode = parsed

        try:
            html = await fetch_player_html(share_key, fid)
        except httpx.RequestError as e:
            logger.warning(f"[febbox] request failed: {type(e).__name__} - {e}")
            return None
        except Exception as e:  # curl_cffi raises its own error types
            logger.warning(f"[febbox] player fetch failed: {type(e).__name__} - {e}")
            return None

        if not html:
            return None

        streams = _rank_sources(html, fid)
        if not streams:
            return None
        return streams, _parse_subtitles(html, season, episode)

    async def resolve(self, embed_url: str):
        """Resolve to a LIST of per-quality stream tiles (one ``var sources`` entry
        each), all routed through the signed /febbox_proxy. resolve_streams accepts a
        list and appends each as its own tile; the frontend groups them under one
        "ShowBox" card."""
        unlocked = await self._unlock(embed_url)
        if not unlocked:
            return None
        streams, subs = unlocked
        subtitles = [
            {"label": t["label"], "lang": t["lang"], "url": _proxy_path_for(t["url"])}
            for t in subs
        ]
        out: List[dict] = []
        for url, quality in streams:
            item = {
                "source": _source_label(quality),
                "type": "hls" if _is_playlist_url(url) else "mp4",
                "url": _proxy_path_for(url),
            }
            if subtitles:
                item["subtitles"] = subtitles
            out.append(item)
        logger.info(f"[febbox] resolved -> {len(out)} proxied quality tile(s), "
                    f"{len(subtitles)} subtitle track(s)")
        return out

    async def resolve_direct(self, embed_url: str) -> Optional[List[dict]]:
        """Resolve to a LIST of **raw** direct file URLs (NOT same-origin
        /febbox_proxy paths) — one per quality — so the client engine delivers the
        bytes itself: extension (E3) or the signed crimson-proxy edge (E2). The heavy
        mp4/HLS never travels through this backend (New System: take the backend out
        of the byte path).

        The token-gated player lookup still runs here (FEBBOX_UI_TOKEN is a C5
        secret), but only the *control* bytes touch us. Febbox's OSS download links
        carry no Referer gate (the proxy fetches them with a UA alone), so the sole
        header hint is the desktop ``userAgent``. Subtitles stay on the signed
        /febbox_proxy — tiny .srt files we convert to WebVTT on the way out, fine
        (and necessary) to relay.

        Each entry is ``{"label", "url", "streamType", "headers": {"userAgent"},
        "subtitles"}`` (the subtitle URLs are relative signed proxy paths; the
        /resolve grant absolutizes them against the backend base). Returns None when
        the file can't be unlocked."""
        unlocked = await self._unlock(embed_url)
        if not unlocked:
            return None
        streams, subs = unlocked
        subtitles = [
            {"label": t["label"], "lang": t["lang"], "url": _proxy_path_for(t["url"])}
            for t in subs
        ]
        out: List[dict] = []
        for url, quality in streams:
            out.append({
                "label": _source_label(quality),
                "url": url,
                "streamType": "hls" if _is_playlist_url(url) else "mp4",
                "headers": {"userAgent": UA},
                "subtitles": subtitles,
            })
        logger.info(f"[febbox] resolved -> {len(out)} DIRECT quality stream(s), "
                    f"{len(subtitles)} subtitle track(s) [bytes off-backend]")
        return out

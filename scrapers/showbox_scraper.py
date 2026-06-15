import logging
import os
import re
from typing import List, Optional

from selectolax.parser import HTMLParser

from .base_scraper import BaseAnimeScraper
# Config + the token-gated final hop live with the resolver; the scraper only
# does the (auth-free) discovery down to a Febbox share_key + episode fid.
from resolvers.febbox import EMBED_MARKER, is_configured

logger = logging.getLogger(__name__)

SHOWBOX_BASE = "https://www.showbox.media"
FEBBOX_FILE_SHARE_LIST = "https://www.febbox.com/file/file_share_list"


def _slugify(text: str) -> str:
    """ShowBox URL slug: lowercase, & -> 'and', spaces/underscores -> '-',
    drop other punctuation, collapse repeats. Mirrors ShowBox's own slugs so a
    detail URL can be constructed directly (the fast path)."""
    if not text:
        return ""
    text = text.lower().strip().replace("&", "and")
    text = re.sub(r"[_\s]+", "-", text)
    text = re.sub(r"[^\w-]+", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def _norm(text: str) -> str:
    """Normalise a title to lowercase alphanumerics for loose comparison."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


class ShowBoxScraper(BaseAnimeScraper):
    """
    ShowBox / Febbox source (direct-file, title-keyed).

    ShowBox indexes a title and points at a Febbox *file share* (a folder of the
    real video files); Febbox then serves **direct mp4 links** per quality — a
    cloud file-host, not an embed player, so there's no ad iframe or rotating
    obfuscation to chase. This is the durable "direct file" prize of the
    P-Stream provider ecosystem, re-implemented in pure Python.

    This scraper does the auth-free discovery half:

        search_anime        : title (+year) -> showbox.media /{tv}/detail/{id}
                              -> showbox.media/index/share_link -> febbox share_key
        get_episode_embeds  : febbox file_share_list -> season folder -> episode
                              file -> ``crimson-febbox:{share_key}:{fid}`` marker

    The token-gated final hop (``POST febbox.com/file/player`` with the ``ui``
    cookie -> direct mp4 -> signed ``/febbox_proxy``) lives in ``FebboxResolver``.
    Like the Jellyfin source, the whole thing **disables itself** unless
    FEBBOX_UI_TOKEN is configured, so it never wastes requests or surfaces a dead
    tile. Treated as TV (this backend always supplies season + episode), mirroring
    the cinema.bz / PlayIMDb scrapers. See [[jellyfin-source]] / [[cinemabz-source]].
    """

    async def _tmdb_year(self, tmdb_id) -> Optional[str]:
        """Show's first-air-date year from TMDB (used to disambiguate the slug /
        search). Optional — without it we fall back to keyword search."""
        key = os.getenv("TMDB_API_KEY")
        if not key or tmdb_id is None:
            return None
        try:
            resp = await self.client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                headers={"Authorization": f"Bearer {key}", "accept": "application/json"},
            )
            if resp.status_code != 200:
                return None
            date = resp.json().get("first_air_date") or ""
            return date[:4] if len(date) >= 4 else None
        except Exception as e:
            logger.info(f"[ShowBox] TMDB year lookup failed: {type(e).__name__} - {e}")
            return None

    def _candidate_titles(self, media_ctx: dict) -> List[str]:
        titles: List[str] = []
        for key in ("title_english", "title", "title_romaji", "title_native"):
            v = media_ctx.get(key)
            if v and v not in titles:
                titles.append(v)
        for syn in media_ctx.get("synonyms") or []:
            if syn and syn not in titles:
                titles.append(syn)
        return titles

    async def _detail_id(self, url: str, norm_targets: set) -> Optional[str]:
        """Fetch a ShowBox detail page; return its numeric content id if the page
        exists and its title plausibly matches one of our candidate titles."""
        try:
            resp = await self.client.get(url)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        html = resp.text
        m = re.search(r"/(?:movie|tv)/detail/(\d+)", html)
        if not m:
            return None

        # Loosely validate the page title so we don't lock onto a wrong show that
        # happens to live at the constructed slug.
        tree = HTMLParser(html)
        page_title = ""
        for sel in ("h2.heading-name a", "h1.heading-name", 'meta[property="og:title"]'):
            node = tree.css_first(sel)
            if node:
                page_title = node.attributes.get("content") if sel.startswith("meta") else node.text()
                if page_title:
                    break
        np = _norm(page_title)
        if np and norm_targets and not any(
            np == t or (len(t) > 3 and (t in np or np in t)) for t in norm_targets
        ):
            logger.info(f"[ShowBox] title mismatch at {url}: {page_title!r}")
            return None
        return m.group(1)

    async def _search_id(self, keyword: str, norm_targets: set) -> Optional[str]:
        """Search ShowBox and return the content id of the best TV match."""
        try:
            resp = await self.client.get(
                f"{SHOWBOX_BASE}/search", params={"keyword": keyword}
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None

        tree = HTMLParser(resp.text)
        best_href = None
        for poster in tree.css("div.film-poster"):
            a = poster.css_first("a.film-poster-ahref") or poster.css_first("a")
            if not a:
                continue
            href = a.attributes.get("href") or ""
            title = a.attributes.get("title") or a.text()
            if "/tv/" not in href:  # we always look up as TV
                continue
            nt = _norm(title)
            if nt and any(nt == t or (len(t) > 3 and (t in nt or nt in t)) for t in norm_targets):
                best_href = href
                break

        if not best_href:
            return None
        detail_url = best_href if best_href.startswith("http") else SHOWBOX_BASE + best_href
        return await self._detail_id(detail_url, norm_targets)

    async def _share_key(self, content_id: str) -> Optional[str]:
        """Resolve a ShowBox content id to its Febbox share_key. Tries the TV
        type first, then movie (some anime are catalogued as movies)."""
        for type_val in ("2", "1"):
            try:
                resp = await self.client.get(
                    f"{SHOWBOX_BASE}/index/share_link",
                    params={"id": content_id, "type": type_val},
                )
                data = resp.json()
            except Exception:
                continue
            link = (data.get("data") or {}).get("link") if isinstance(data, dict) else None
            if link:
                m = re.search(r"/share/([a-zA-Z0-9-]+)", link)
                if m:
                    return m.group(1)
        return None

    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        """Resolve the show to a Febbox ``share_key`` (used as the slug)."""
        if not is_configured():
            return None  # no FEBBOX_UI_TOKEN -> source disabled, skip all work

        candidates = self._candidate_titles(media_ctx)
        if not candidates:
            logger.warning("[ShowBox] no title in media_ctx, cannot search")
            return None
        norm_targets = {_norm(c) for c in candidates}
        norm_targets.discard("")

        year = await self._tmdb_year(media_ctx.get("tmdb_id"))

        content_id: Optional[str] = None

        # Fast path: construct the detail URL directly from slug + year.
        if year:
            for title in candidates[:3]:
                slug = _slugify(title)
                if not slug:
                    continue
                url = f"{SHOWBOX_BASE}/tv/t-{slug}-{year}"
                content_id = await self._detail_id(url, norm_targets)
                if content_id:
                    logger.info(f"[ShowBox] direct slug hit: {url} -> id {content_id}")
                    break

        # Fallback: keyword search.
        if not content_id:
            for title in candidates[:3]:
                kw = f"{title} {year}" if year else title
                content_id = await self._search_id(kw, norm_targets)
                if content_id:
                    logger.info(f"[ShowBox] search hit for {kw!r} -> id {content_id}")
                    break

        if not content_id:
            logger.info(f"[ShowBox] no match for {candidates[0]!r}")
            return None

        share_key = await self._share_key(content_id)
        if not share_key:
            logger.info(f"[ShowBox] no Febbox share for content id {content_id}")
            return None
        logger.info(f"[ShowBox] content id {content_id} -> share_key {share_key}")
        return share_key

    async def _list_share(self, share_key: str, parent_id: Optional[int] = None) -> List[dict]:
        """List a Febbox share folder (auth-free). Returns its file_list."""
        params = {"share_key": share_key, "pwd": ""}
        if parent_id is not None:
            params["parent_id"] = str(parent_id)
            params["page"] = "1"
        try:
            resp = await self.client.get(
                FEBBOX_FILE_SHARE_LIST, params=params, headers={"accept-language": "en"}
            )
            data = resp.json()
        except Exception as e:
            logger.warning(f"[ShowBox] file_share_list failed: {type(e).__name__} - {e}")
            return []
        if not isinstance(data, dict):
            return []
        return (data.get("data") or {}).get("file_list") or []

    @staticmethod
    def _is_video(f: dict) -> bool:
        return not f.get("is_dir") and str(f.get("ext", "")).lower() in ("mp4", "mkv", "avi")

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> List[str]:
        """Locate the episode file's ``fid`` in the Febbox share and emit the
        ``crimson-febbox:{share_key}:{fid}`` marker the resolver unlocks."""
        if not anime_slug or not is_configured():
            return []
        share_key = anime_slug

        root = await self._list_share(share_key)
        if not root:
            return []

        # Episodes live under a "Season N" folder; some shares hold them at the
        # root. Build the candidate file list accordingly.
        files: List[dict] = []
        season_folder = next(
            (f for f in root if f.get("is_dir")
             and re.search(rf"season\s*0*{season_num}\b", str(f.get("file_name", "")), re.I)),
            None,
        )
        if season_folder:
            files = await self._list_share(share_key, season_folder.get("fid"))
        else:
            # No season folders — match episodes directly at the root.
            files = root

        # Match SxxEyy in the filename (the Febbox naming convention).
        ep_re = re.compile(rf"[Ss]0*{season_num}[Ee]0*{episode_num}(?!\d)")
        match = next(
            (f for f in files if self._is_video(f) and ep_re.search(str(f.get("file_name", "")))),
            None,
        )
        # Fallback: a bare "E01"/"Episode 1" when there's no SxxEyy (single-season).
        if not match:
            bare_re = re.compile(rf"(?:^|[^\d])(?:e|ep|episode)\s*0*{episode_num}(?!\d)", re.I)
            match = next(
                (f for f in files if self._is_video(f) and bare_re.search(str(f.get("file_name", "")))),
                None,
            )
        if not match:
            logger.info(
                f"[ShowBox] S{season_num}E{episode_num} not found in share {share_key} "
                f"({len(files)} files)"
            )
            return []

        fid = match.get("fid")
        if not fid:
            return []
        # season/episode ride along so the resolver can filter Febbox's subtitle
        # pool (which mixes episodes) down to this one.
        marker = f"{EMBED_MARKER}:{share_key}:{fid}:{season_num}:{episode_num}"
        logger.info(f"[ShowBox] matched {match.get('file_name')!r} -> {marker}")
        return [marker]

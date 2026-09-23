"""This repo's GitHub Releases as a public changelog: deploys are cut as
published releases, so their notes already are the changelog.

Cached per replica, and refreshed with conditional requests (ETag, then 304) so
the periodic refresh costs almost nothing against GitHub's rate limit. A private
repo works with a token that can read it; without ``GITHUB_TOKEN`` /changelog
answers 503. Drafts are never shown.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

import httpx

from core.config import get_settings

logger = logging.getLogger("crimson.changelog")

GITHUB_API = "https://api.github.com"


class ChangelogService:
    """Called from the route's worker thread and the scheduler's, so the cache is
    locked; the blocking fetch runs outside the lock so a slow GitHub never
    blocks readers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Optional[List[Dict]] = None
        self._fetched_at: float = 0.0
        self._etag: Optional[str] = None

    def configured(self) -> bool:
        return bool(get_settings().github_token)

    @staticmethod
    def _shape(rel: Dict) -> Dict:
        return {
            "tag": rel.get("tag_name"),
            "name": rel.get("name") or rel.get("tag_name"),
            "body": rel.get("body") or "",
            "published_at": rel.get("published_at") or rel.get("created_at"),
            "url": rel.get("html_url"),
            "prerelease": bool(rel.get("prerelease")),
            "author": (rel.get("author") or {}).get("login"),
        }

    def _fetch(self) -> List[Dict]:
        """The current entries; the cached list itself on a 304. Raises on any failure."""
        token = get_settings().github_token
        if not token:
            raise RuntimeError("GITHUB_TOKEN is not configured")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "crimson-backend-changelog",
            "Authorization": f"Bearer {token}",
        }
        if self._etag and self._entries is not None:
            headers["If-None-Match"] = self._etag
        url = f"{GITHUB_API}/repos/{get_settings().github_repo}/releases"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers=headers, params={"per_page": get_settings().changelog_max_entries})
        if resp.status_code == 304 and self._entries is not None:
            return self._entries
        resp.raise_for_status()
        self._etag = resp.headers.get("ETag")
        releases = resp.json()
        if not isinstance(releases, list):
            raise ValueError("Unexpected GitHub releases payload")
        include_pre = get_settings().changelog_include_prereleases
        entries = [
            self._shape(r)
            for r in releases
            if isinstance(r, dict) and not r.get("draft")
            and (include_pre or not r.get("prerelease"))
        ]
        return entries[: get_settings().changelog_max_entries]

    def refresh(self) -> List[Dict]:
        """Fetch now. A failure leaves the cache as it was and raises, so the
        scheduler logs it."""
        entries = self._fetch()
        with self._lock:
            self._entries = entries
            self._fetched_at = time.monotonic()
        return entries

    def get(self) -> Dict:
        """``{entries, stale}``, refreshing first when the cache has expired. A
        failed refresh serves what is cached (possibly nothing) marked stale,
        because a GitHub hiccup must not blank the page."""
        ttl = get_settings().changelog_cache_ttl
        with self._lock:
            expired = self._entries is None or time.monotonic() - self._fetched_at >= ttl
        if expired:
            try:
                self.refresh()
            except Exception as e:
                logger.warning(f"Changelog refresh failed, serving the cached notes: {e}")
        with self._lock:
            stale = self._entries is None or time.monotonic() - self._fetched_at >= ttl
            return {"entries": list(self._entries or []), "stale": stale}


service = ChangelogService()

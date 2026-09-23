"""
Changelog service — surfaces the project's GitHub Releases as a public changelog.

Deploys are cut as *published GitHub Releases* (see .gitlab-ci.yml, whose `.release`
rule builds + deploys a v* tag), so the release notes you already write ARE the
changelog. This service
fetches them from the GitHub REST API and caches them in-process, so /changelog
never blocks on — or hammers — GitHub.

A **private** repo is fine: the REST API returns releases for a private repo when
given a token with read access. Configure via:

  * ``GITHUB_TOKEN`` — a fine-grained PAT with *Contents: read* on this repo, or a
    classic ``repo``-scoped token. Without it the service is "not configured" and
    /changelog returns 503.
  * ``GITHUB_REPO`` — ``owner/repo`` (default ``crimsonhaven-to/crimson-backend``).

Tuning (all optional):
  * ``CHANGELOG_CACHE_TTL`` — seconds between refreshes (default 1800 = 30 min).
  * ``CHANGELOG_MAX_ENTRIES`` — how many releases to keep (default 30, max 100).
  * ``CHANGELOG_INCLUDE_PRERELEASES`` — include pre-releases (default true). Drafts
    are always excluded.

The cache lives per replica (no cross-replica coordination); conditional requests
(ETag → 304) keep the periodic refresh near-free against GitHub's rate limit.
"""

import threading
import time
from typing import Dict, List, Optional

import httpx
from core.config import get_settings

GITHUB_API = "https://api.github.com"


class ChangelogService:
    """In-process, ETag-aware cache over the repo's GitHub Releases.

    Thread-safe: the network fetch runs *outside* the lock (so a slow GitHub call
    never blocks readers), and only the cache mutation is guarded. The /changelog
    route calls :meth:`get` via ``run_in_threadpool`` (the fetch is blocking httpx),
    and the scheduler calls :meth:`refresh` directly from its worker thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Optional[List[Dict]] = None
        self._fetched_at: float = 0.0          # time.monotonic() of last success
        self._etag: Optional[str] = None
        self._last_error: Optional[str] = None

    def configured(self) -> bool:
        """True once a GitHub token is present — otherwise /changelog 503s."""
        return bool(get_settings().github_token)

    @staticmethod
    def _shape(rel: Dict) -> Dict:
        """One release → the public changelog entry shape (Markdown ``body``)."""
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
        """Blocking GitHub fetch. Honours ETag (returns the cached list unchanged on
        304). Raises on a missing token or any HTTP error."""
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
            return self._entries  # not modified since last fetch
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
        """Force a fetch and update the cache. On failure, keeps any previously
        cached entries (fail-open) but records the error, then re-raises so callers
        that care (the scheduler) can log it."""
        entries = self._fetch()
        with self._lock:
            self._entries = entries
            self._fetched_at = time.monotonic()
            self._last_error = None
        return entries

    def get(self) -> Dict:
        """Return ``{entries, stale, error, fetched}`` for the route.

        Serves the cached entries, lazily refreshing when stale or never-fetched.
        If that refresh fails it falls back to whatever is cached (possibly empty)
        rather than erroring — a transient GitHub hiccup must not blank the page.
        """
        with self._lock:
            have = self._entries is not None
            age = time.monotonic() - self._fetched_at
        if not have or age >= get_settings().changelog_cache_ttl:
            try:
                self.refresh()
            except Exception as e:  # fall back to stale/empty cache
                with self._lock:
                    self._last_error = f"{type(e).__name__}: {e}"
        with self._lock:
            stale = self._entries is None or (time.monotonic() - self._fetched_at) >= get_settings().changelog_cache_ttl
            return {
                "entries": list(self._entries or []),
                "fetched": self._entries is not None,
                "stale": stale,
                "error": self._last_error,
            }

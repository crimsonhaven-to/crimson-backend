from typing import Optional

from .base_scraper import BaseAnimeScraper
# The Jellyfin client (auth + API + config) lives with the resolver, which owns
# the heavy lifting — the scraper just locates the episode item.
from resolvers.jellyfin import EMBED_MARKER, api_get, is_configured, _ensure_auth


def _tmdb_of(item: dict) -> str:
    pids = item.get("ProviderIds") or {}
    # Jellyfin casing varies ("Tmdb"); match case-insensitively.
    for k, v in pids.items():
        if k.lower() == "tmdb":
            return str(v)
    return ""


class JellyfinScraper(BaseAnimeScraper):
    """
    Jellyfin scraper — locates an episode in the user's own Jellyfin library.

    Disabled (returns nothing) unless JELLYFIN_URL/USERNAME are configured. The
    pipeline is TMDB-centric, and Jellyfin stores the TMDB id on items, so we
    match the Series by TMDB id first (reliable) and fall back to a title search.
    The "slug" is the Jellyfin Series item id; the emitted embed URL is the
    ``crimson-jellyfin:{episodeItemId}`` marker that ``JellyfinResolver`` turns
    into a proxied, ad-free stream URL.

    See [[movish-player-internals]] for the proxy pattern the resolver mirrors.
    """

    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        """Find the Jellyfin Series id for this TMDB id / title."""
        if not is_configured():
            return None

        tmdb_id = media_ctx.get("tmdb_id")
        title = media_ctx.get("title")

        try:
            _token, uid = await _ensure_auth()
        except Exception as e:
            print(f"[JellyfinScraper] Auth failed: {type(e).__name__} - {e}")
            return None

        try:
            items: list = []

            # 1) Match by TMDB id (most reliable). anyProviderIdEquals is the
            #    fast path; we still verify the id in case the server ignores it.
            if tmdb_id is not None:
                try:
                    data = await api_get(
                        "/Items",
                        {
                            "userId": uid,
                            "recursive": "true",
                            "includeItemTypes": "Series",
                            "anyProviderIdEquals": f"tmdb.{tmdb_id}",
                            "fields": "ProviderIds",
                            "limit": 10,
                        },
                    )
                    items = [it for it in (data.get("Items") or []) if _tmdb_of(it) == str(tmdb_id)]
                except Exception:
                    items = []

            # 2) Fall back to a title search, preferring a TMDB id match.
            if not items and title:
                data = await api_get(
                    "/Items",
                    {
                        "userId": uid,
                        "recursive": "true",
                        "includeItemTypes": "Series",
                        "searchTerm": title,
                        "fields": "ProviderIds",
                        "limit": 20,
                    },
                )
                results = data.get("Items") or []
                if tmdb_id is not None:
                    matched = [it for it in results if _tmdb_of(it) == str(tmdb_id)]
                    items = matched or results
                else:
                    items = results

            if not items:
                print(f"[JellyfinScraper] No series found (tmdb={tmdb_id}, title={title!r}).")
                return None

            series_id = items[0].get("Id")
            print(f"[JellyfinScraper] Matched series {items[0].get('Name')!r} -> {series_id}")
            return series_id
        except Exception as e:
            print(f"[JellyfinScraper] Series lookup failed: {type(e).__name__} - {e}")
            return None

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> list[str]:
        """Locate the episode item in the series and emit its marker URL."""
        if not anime_slug or not is_configured():
            return []

        try:
            _token, uid = await _ensure_auth()
            data = await api_get(
                f"/Shows/{anime_slug}/Episodes",
                {"userId": uid, "season": season_num, "fields": "ProviderIds"},
            )
            episodes = data.get("Items") or []

            match = None
            for ep in episodes:
                if ep.get("IndexNumber") == episode_num and ep.get("ParentIndexNumber") in (season_num, None):
                    match = ep
                    break
            # Some libraries don't filter cleanly by season — last-ditch match on
            # episode index alone within the returned set.
            if match is None:
                match = next((ep for ep in episodes if ep.get("IndexNumber") == episode_num), None)

            if match is None:
                print(f"[JellyfinScraper] Episode S{season_num}E{episode_num} not found.")
                return []

            item_id = match.get("Id")
            embed = f"{EMBED_MARKER}:{item_id}"
            print(f"[JellyfinScraper] Episode item {item_id} -> {embed}")
            return [embed]
        except Exception as e:
            print(f"[JellyfinScraper] Episode lookup failed: {type(e).__name__} - {e}")
            return []

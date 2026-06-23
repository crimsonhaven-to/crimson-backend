import abc

from curl_cffi.requests import AsyncSession


class BaseAnimeScraper(abc.ABC):
    """
    The blueprint for all future anime scrapers.
    Every new provider MUST implement these methods.
    """

    # Browser profile curl_cffi impersonates at the TLS + HTTP/2 layer. A plain
    # httpx client sends a Python-shaped TLS ClientHello (JA3/JA4) and HTTP/2
    # frame ordering that anti-bot front-ends fingerprint instantly; matching a
    # real Chrome is what clears Cloudflare's *passive* checks — e.g. s.to's
    # Turnstile "redirect gate", which a vanilla client trips before it ever
    # gets a response. "chrome" tracks a recent stable Chrome build.
    _IMPERSONATE = "chrome"

    # Whether this scraper can resolve a standalone MOVIE (a TMDB *movie* id, no
    # season/episode). Defaults to False so the title/episode-oriented anime
    # scrapers are simply skipped for movie requests instead of building a bogus
    # season-1/episode-1 URL. The TMDB-keyed movie sources opt in by overriding
    # this to True and honouring media_ctx["media_type"] == "movie".
    SUPPORTS_MOVIES = False

    def __init__(self):
        # Every provider gets its own browser-impersonating async HTTP client.
        # We deliberately don't set a User-Agent: ``impersonate`` already installs
        # one (plus the matching sec-ch-ua/Accept headers) consistent with the
        # spoofed TLS fingerprint. Overriding it would make the UA and the JA3
        # disagree, which is itself a bot signal.
        self.client = AsyncSession(
            impersonate=self._IMPERSONATE,
            timeout=10.0,
            allow_redirects=True,
        )

    @abc.abstractmethod
    async def search_anime(self, media_ctx: dict) -> str | None:
        """
        Step 1: Search the target streaming site using the anime title.
        Returns the unique slug/ID used by that specific website.
        """
        pass

    @abc.abstractmethod
    async def get_episode_embeds(self, anime_slug: str, episode_num: int, season_num: int) -> list:
        """
        Step 2: Go to the episode page on that website and locate the
        third-party embed video player URLs (like MegaF, Mp4Upload, etc).

        Returns a list of embeds. Each entry is either a bare URL string, or a
        ``{"url": <str>, "language": <str|None>}`` dict when the scraper knows the
        audio/subtitle language of that embed (e.g. aniworld's German Dub / Sub).
        The resolve pipeline accepts both forms.
        """
        pass

    async def close(self):
        """Clean up the HTTP client connection when done."""
        await self.client.close()

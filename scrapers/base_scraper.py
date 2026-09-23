import abc

from curl_cffi.requests import AsyncSession


class BaseAnimeScraper(abc.ABC):
    """Finds a title on one source and lists the embeds for an episode.

    The pipeline builds a fresh instance per request: ``search_anime`` maps the
    media context (``tmdb_id``, ``tmdb_season``, ``media_type``, the AniList
    titles and ``synonyms``) to a source-specific id, or None to opt out. Then
    ``get_episode_embeds`` returns embeds for that id, each a URL string or a
    ``{"url", "language"}`` dict, which a resolver with a matching
    ``domain_keyword`` turns into streams. Operator sources emit a
    ``crimson-<name>:<token>`` marker and pair it with a resolver in
    ``resolvers/``.
    """

    # False keeps episode-oriented sources out of movie requests instead of
    # building a bogus season 1 episode 1 lookup.
    SUPPORTS_MOVIES = False

    _client: AsyncSession | None = None

    @property
    def client(self) -> AsyncSession:
        # Built on first use: most sources never touch the network, and the
        # pipeline builds a scraper per /watch, warm-up and probe.
        if self._client is None:
            # Impersonating Chrome's TLS and HTTP/2 fingerprint clears Cloudflare's
            # passive bot checks, which a plain httpx client trips. No User-Agent:
            # impersonate sets one that matches, and a mismatch is itself a signal.
            self._client = AsyncSession(impersonate="chrome", timeout=10.0, allow_redirects=True)
        return self._client

    @abc.abstractmethod
    async def search_anime(self, media_ctx: dict) -> str | None:
        ...

    @abc.abstractmethod
    async def get_episode_embeds(self, anime_slug: str, episode_num: int, season_num: int) -> list:
        ...

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

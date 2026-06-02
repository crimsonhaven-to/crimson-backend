import abc
import httpx

class BaseAnimeScraper(abc.ABC):
    """
    The blueprint for all future anime scrapers. 
    Every new provider MUST implement these methods.
    """
    
    def __init__(self):
        # Every provider gets its own async HTTP client with pre-configured headers
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            timeout=10.0,
            follow_redirects=True
        )

    @abc.abstractmethod
    async def search_anime(self, media_ctx: dict) -> str | None:
        """
        Step 1: Search the target streaming site using the anime title.
        Returns the unique slug/ID used by that specific website.
        """
        pass

    @abc.abstractmethod
    async def get_episode_embeds(self, anime_slug: str, episode_num: int) -> list[str]:
        """
        Step 2: Go to the episode page on that website and locate the 
        third-party embed video player URLs (like MegaF, Mp4Upload, etc).
        """
        pass

    async def close(self):
        """Clean up the HTTP client connection when done."""
        await self.client.aclose()
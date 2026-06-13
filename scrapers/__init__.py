# scrapers/__init__.py
from .gogo_scraper import GogoScraper
from .voe_scraper import VoeScraper
from .movish_scraper import MovishScraper
from .playimdb_scraper import PlayimdbScraper
from .jellyfin_scraper import JellyfinScraper
from .animekai_scraper import AnimekaiScraper
from .animesuge_scraper import AnimeSugeScraper
from .aniworld_scraper import AniworldScraper
from .sto_scraper import StoScraper
from .cinemabz_scraper import CinemabzScraper
from .aniwatch_scraper import AniwatchScraper

ALL_SCRAPERS = [
    GogoScraper,
    AniworldScraper,  # German s.to-family site; feeds VOE/Vidmoly embeds.
    StoScraper,       # s.to: aniworld's general sibling (anime + movies); same VOE/Vidmoly embeds.
    AniwatchScraper,  # WordPress site; feeds "VidSrc" (megaplay) embeds.
    #VoeScraper,
    MovishScraper,
    PlayimdbScraper,
    JellyfinScraper,
    AnimekaiScraper,
    AnimeSugeScraper,
    CinemabzScraper,  # TMDB-keyed HLS aggregator; 3 providers -> 3 tiles
]
# scrapers/__init__.py
from .gogo_scraper import GogoScraper
from .voe_scraper import VoeScraper
from .vidking_scraper import VidkingScraper
from .vidking_test_scraper import VidkingTestScraper
from .movish_scraper import MovishScraper
from .jellyfin_scraper import JellyfinScraper
from .animekai_scraper import AnimekaiScraper
from .animesuge_scraper import AnimeSugeScraper

ALL_SCRAPERS = [
    GogoScraper,
    #VoeScraper,
    # VidkingScraper,  # deprecated — superseded by VidkingTestScraper (ad-free, tested)
    VidkingTestScraper,
    MovishScraper,
    JellyfinScraper,
    AnimekaiScraper,
    AnimeSugeScraper,
]
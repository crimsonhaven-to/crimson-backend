# scrapers/__init__.py
from .gogo_scraper import GogoScraper
from .voe_scraper import VoeScraper
from .vidking_scraper import VidkingScraper
from .vidking_test_scraper import VidkingTestScraper
from .movish_scraper import MovishScraper
from .playimdb_scraper import PlayimdbScraper
from .jellyfin_scraper import JellyfinScraper
from .animekai_scraper import AnimekaiScraper
from .animesuge_scraper import AnimeSugeScraper

ALL_SCRAPERS = [
    GogoScraper,
    #VoeScraper,
    # VidkingScraper,  # ARCHIVED — legacy VidKing (raw vidking.net embed, ads).
    #                  # Superseded by the ad-free VidkingTestScraper below. Import
    #                  # kept so it can be re-enabled by uncommenting this line.
    VidkingTestScraper,
    MovishScraper,
    PlayimdbScraper,
    JellyfinScraper,
    AnimekaiScraper,
    AnimeSugeScraper,
]
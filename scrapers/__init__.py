# scrapers/__init__.py
from .gogo_scraper import GogoScraper
from .voe_scraper import VoeScraper
from .vidking_scraper import VidkingScraper
from .animekai_scraper import AnimekaiScraper
from .flix2day_scraper import Flix2dayScraper

ALL_SCRAPERS = [
    GogoScraper,
    #VoeScraper,
    VidkingScraper,
    AnimekaiScraper,
    Flix2dayScraper,
]
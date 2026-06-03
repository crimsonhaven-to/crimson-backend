# scrapers/__init__.py
from .gogo_scraper import GogoScraper
from .voe_scraper import VoeScraper
from .vidking_scraper import VidkingScraper

ALL_SCRAPERS = [
    GogoScraper,
    VoeScraper,
    VidkingScraper
]
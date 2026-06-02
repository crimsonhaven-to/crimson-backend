# scrapers/__init__.py
from .gogo_scraper import GogoScraper
from .voe_scraper_test import VoeScraper
# Future scrapers get imported here:

ALL_SCRAPERS = [
    GogoScraper,
    VoeScraper
]
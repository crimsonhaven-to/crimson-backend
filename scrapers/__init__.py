# scrapers/__init__.py
from .gogo_scraper import GogoScraper
from .voe_scraper import VoeScraper
# Future scrapers get imported here:

ALL_SCRAPERS = [
    GogoScraper,
    VoeScraper
]
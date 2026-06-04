# scrapers/__init__.py
from .gogo_scraper import GogoScraper
from .voe_scraper import VoeScraper
from .vidking_scraper import VidkingScraper
from .animekai_scraper import AnimekaiScraper
from .animesuge_scraper import AnimeSugeScraper

ALL_SCRAPERS = [
    GogoScraper,
    #VoeScraper,
    VidkingScraper,
    AnimekaiScraper,
    AnimeSugeScraper,
]
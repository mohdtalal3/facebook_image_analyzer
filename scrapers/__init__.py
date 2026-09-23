"""Brand product scrapers — after KIE identifies a food image's product
name, the matching brand scraper looks that product up on the retailer's
own site and returns real product data (name, price, description) that gets
attached to the image analysis and shown on the WordPress page.

Proxy comes straight from .env (STATIC_PROXY, falling back to PROXY) — the
same variables the Facebook scrapers use.

Usage:
    from scrapers import get_searcher
    searcher = get_searcher("ALDI")     # None if the brand has no scraper
    product = searcher.search("Sourdough Pumpkinseed Cranberry Loaf")
"""

import os

from dotenv import load_dotenv

from .aldi import AldiSearcher
from .costco import CostcoSearcher
from .dollar_general import DollarGeneralSearcher
from .dollartree import DollarTreeSearcher
from .fivebelow import FiveBelowSearcher
from .foodlion import FoodLionSearcher
from .heb import HEBSearcher
from .hyvee import HyVeeSearcher
from .kroger import KrogerSearcher
from .publix import PublixSearcher
from .samsclub import SamsClubSearcher
from .target import TargetSearcher
from .trader_joes import TraderJoesSearcher
from .walmart import WalmartSearcher
from .winndixie import WinnDixieSearcher

load_dotenv()

# Canonical brand (brand_mapping) -> searcher class. Brands without an
# entry here are skipped by the scraper-analysis step.
SEARCHERS: dict[str, type] = {
    "ALDI": AldiSearcher,
    "Costco": CostcoSearcher,
    "Dollar General": DollarGeneralSearcher,
    "Dollar Tree": DollarTreeSearcher,
    "Five Below": FiveBelowSearcher,
    "Food Lion": FoodLionSearcher,
    "H-E-B": HEBSearcher,
    "Hy-Vee": HyVeeSearcher,
    "Kroger": KrogerSearcher,
    "Publix": PublixSearcher,
    "Sam's Club": SamsClubSearcher,
    "Target": TargetSearcher,
    "Trader Joe's": TraderJoesSearcher,
    "Walmart": WalmartSearcher,
    "Winn-Dixie": WinnDixieSearcher,
}


def _proxy_from_env() -> str | None:
    """Proxy dedicated to the product scrapers (SCRAPPER_PROXY), falling back
    to the shared Facebook-scraping proxies (STATIC_PROXY, then PROXY)."""
    proxy = (os.getenv("SCRAPPER_PROXY", "").strip())
    return proxy or None


def get_searcher(brand: str):
    """Return a ready-to-use searcher instance for the canonical brand, or
    None if no scraper exists for it."""
    cls = SEARCHERS.get(brand)
    if not cls:
        return None
    return cls(_proxy_from_env())


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

load_dotenv()

# Canonical brand (brand_mapping) -> searcher class. Brands without an
# entry here are skipped by the scraper-analysis step.
SEARCHERS: dict[str, type] = {
    "ALDI": AldiSearcher,
}


def _proxy_from_env() -> str | None:
    proxy = os.getenv("STATIC_PROXY", "").strip() or os.getenv("PROXY", "").strip()
    return proxy or None


def get_searcher(brand: str):
    """Return a ready-to-use searcher instance for the canonical brand, or
    None if no scraper exists for it."""
    cls = SEARCHERS.get(brand)
    if not cls:
        return None
    return cls(_proxy_from_env())


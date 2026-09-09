#!/usr/bin/env python3
"""
Centralized retailer/brand keyword mapping — normalizes every spelling
variation of a retailer name (case, punctuation, spacing, hyphenation) down
to one canonical brand, and maps that canonical brand to its output-folder
slug.

Used by run_facebook.py to:
  1. Detect the single retailer a post's text is about (before spending any
     image-download/analysis cost on it).
  2. Re-check that same canonical brand against each image's KIE-detected
     brand string, to decide whether a post qualifies to be kept.
"""

import re

# Canonical brand -> every known keyword/variant for it (lowercase).
# Matching is done against text with apostrophes/hyphens/underscores
# stripped out (not replaced with spaces) so "H-E-B" / "h e b" / "heb" and
# "D_G" / "d g" / "dg" all normalize the same way as their keyword entries.
BRAND_KEYWORDS: dict[str, list[str]] = {
    "Meijer": ["meijer"],
    "H-E-B": ["heb", "h-e-b", "h e b"],
    "Hy-Vee": ["hyvee", "hy-vee", "hy vee"],
    "Sam's Club": ["sam's", "sams", "sam's club", "sams club"],
    "Walmart": ["walmart", "wal-mart"],
    "Target": ["target"],
    "ALDI": ["aldi"],
    "Trader Joe's": ["tj", "trader joe's", "trader joes"],
    "Costco": ["costco"],
    "Publix": ["publix"],
    "Kroger": ["kroger"],
    "Dollar General": ["dollar general", "dg", "d_g", "d g"],
}

# Canonical brand -> output folder slug (see CONTEXT.md output layout).
BRAND_SLUGS: dict[str, str] = {
    "Meijer": "meijer",
    "H-E-B": "h-e-b",
    "Hy-Vee": "hy-vee",
    "Sam's Club": "sams-club",
    "Walmart": "walmart",
    "Target": "target",
    "ALDI": "aldi",
    "Trader Joe's": "trader-joes",
    "Costco": "costco",
    "Publix": "publix",
    "Kroger": "kroger",
    "Dollar General": "dollar-general",
}


def _normalize(text: str) -> str:
    """Lowercase and strip apostrophes/hyphens/underscores (deleted, not
    replaced with spaces, so hyphenated/underscored single words like
    "H-E-B" or "D_G" collapse to "heb"/"dg"); real word spaces are kept so
    multi-word variants like "hy vee" still match as a phrase."""
    text = text.lower()
    text = re.sub(r"[\'’_-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_KEYWORD_PATTERNS: dict[str, list[re.Pattern]] = {
    brand: [re.compile(r"\b" + re.escape(_normalize(kw)) + r"\b") for kw in keywords]
    for brand, keywords in BRAND_KEYWORDS.items()
}


def detect_brands(text: str | None) -> set[str]:
    """Return the set of canonical brands whose keywords appear in text."""
    if not text:
        return set()
    normalized = _normalize(text)
    found = set()
    for brand, patterns in _KEYWORD_PATTERNS.items():
        if any(p.search(normalized) for p in patterns):
            found.add(brand)
    return found


def detect_single_brand(text: str | None) -> str | None:
    """Return the one canonical brand detected in text, or None if zero or
    more than one distinct brand was detected (ambiguous posts are skipped,
    not guessed at)."""
    brands = detect_brands(text)
    return next(iter(brands)) if len(brands) == 1 else None


def brand_slug(canonical_brand: str) -> str:
    return BRAND_SLUGS.get(canonical_brand, canonical_brand.lower().replace(" ", "-"))

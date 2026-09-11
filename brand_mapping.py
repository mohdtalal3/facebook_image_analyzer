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


_KEYWORD_PATTERNS: dict[str, list[re.Pattern]] = {}
for _brand, _keywords in BRAND_KEYWORDS.items():
    _patterns: list[re.Pattern] = []
    for _kw in _keywords:
        _n = _normalize(_kw)
        # Plain word-boundary match: "#Aldi", "wal-mart", "hy vee", ...
        _patterns.append(re.compile(r"\b" + re.escape(_n) + r"\b"))
        # Hashtag-style fused compounds, where the brand and the next word
        # share one token and a word-boundary regex would never match:
        # "#SamsClub" / "#TraderJoes" / "#DollarGeneral" (multi-word brands
        # fused without the space), and "#ALDIFinds" / "#WalmartFinds" /
        # "#AldiFinds" / "#HyVeeFinds" etc. (brand + finds/haul/deals/run
        # suffix — the standard grocery-haul hashtag format).
        _fused = _n.replace(" ", "")
        if _fused != _n:
            _patterns.append(re.compile(r"\b" + re.escape(_fused) + r"\b"))
        _patterns.append(re.compile(r"\b" + re.escape(_fused) + r"(?:finds?|hauls?|deals?|runs?)\b"))
    _KEYWORD_PATTERNS[_brand] = _patterns


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


def main():
    """Quick test CLI:
      python3 brand_mapping.py "Big #ALDIFinds haul today"
      echo "#SamsClub run" | python3 brand_mapping.py
      python3 brand_mapping.py            (interactive — one text per line, Ctrl+C to quit)
    """
    import sys

    texts = sys.argv[1:]
    if not texts and not sys.stdin.isatty():
        texts = [line.strip() for line in sys.stdin if line.strip()]
    elif not texts:
        print("Interactive brand-detection tester — type post text, Enter to check, Ctrl+C to quit.\n")
        while True:
            try:
                line = input("text> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nbye")
                break
            if not line:
                continue
            _report(line)
        return

    for text in texts:
        _report(text)


def _report(text: str):
    brands = detect_brands(text)
    single = detect_single_brand(text)
    print(f"text   : {text!r}")
    print(f"brands : {sorted(brands) if brands else '(none)'}")
    print(f"single : {single!r}" + (f"  (slug: {brand_slug(single)})" if single else "  (zero or multiple brands — post would be skipped)"))
    print("-" * 60)


if __name__ == "__main__":
    main()

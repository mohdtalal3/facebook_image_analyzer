#!/usr/bin/env python3
"""
Central place for tunable pipeline constants and testing toggles.

Actual secrets (API keys, tokens) stay in .env — this file is for behavior
knobs that used to be scattered across individual modules. Flip these
instead of hunting through run_facebook.py / kie_vision.py / etc.
"""

# ── run_facebook.py pipeline toggles ──
FETCH_COMMENTS = False              # skip comment scraping — not needed right now
ANALYZE_IMAGES = True      # run the KIE upload+analyze workflow at all —
                                 # when False, images are still downloaded/processed
                                 # but never sent to KIE (analysis_status: "skipped")
MAX_IMAGES_PER_POST = 10          # cap images downloaded/processed/analyzed per post —
                                 # None = no limit (all images; a 50-image hard safety cap
                                 # still applies inside the album walk), or a number to cap
IMAGE_WORKERS = 5                   # concurrent per-post image processing/analysis threads
IMAGE_DOWNLOAD_WORKERS = 5          # concurrent per-post image *download* threads (fb_client.download_pending_post_images)
POSTS_PER_SOURCE_DEFAULT = 500      # default --posts-per-source limit for page/group fetch

# ── run_facebook.py publish-prep: scraper analysis ──
SCRAPER_ANALYSIS = True             # after KIE analysis, look each FOOD image's product up on the
                                # brand's own site (scrapers/, e.g. AldiSearcher) and attach
                                # name/price/description — shown on the WordPress page. When
                                # False, publishing uses only the KIE image-analysis data.
PRICE_ON_IMAGE = True                   # stamp the scraped price onto the food image itself as a red
                                # rounded badge (top-right, price_overlay.py) before upload
SCRAPER_WORKERS = 5                 # parallel keyword-search threads in enrich_food_images_with_scrapes —
                                # each thread gets its own searcher session (curl_cffi sessions
                                # are not thread-safe), so N keywords scrape concurrently

# ── run_facebook.py publish-prep: product dedup (before scraper analysis + publishing) ──
DEDUPE_PRODUCTS = True              # remove duplicate products from food/image_analysis.json after
                                # analysis — different posts/pages often show the same product;
                                # duplicates (and their images) are dropped so scrapers and the
                                # WordPress publisher never see them
DEDUPE_THRESHOLD = 0.9             # normalized-name similarity ratio (difflib) above which two
                                # product names count as duplicates

# ── run_facebook.py publish-prep: AI image generation (final stage, after dedup + scraper analysis) ──
GENERATE_AI_IMAGES = True           # send each food image to KIE (nano-banana-pro via generate.py)
                                # to create a new AI-generated image; the AI image replaces the
                                # original for publishing. When False, the original image is
                                # published (and gets the price badge, if PRICE_ON_IMAGE).
                                # The prompt comes from the workspace image_prompt
                                # config (--image-prompt); workspaces without one skip generation.
AI_IMAGE_MAX_BYTES = 512000      # images are compressed under this size before upload to KIE
                                # and the AI result is compressed under it before publishing
AI_IMAGE_WORKERS = 5                # parallel AI image-generation threads (upload→createTask→poll→download
                                # per image; the shared KIE rate limiter keeps the total under the
                                # account cap, so this only overlaps the long poll waits)
AI_IMAGE_COMPARE = False            # when True, the output/published image is a comparison sheet:
                                # the AI-generated image on TOP and the ORIGINAL below it, each
                                # labeled ("AI GENERATED" / "ORIGINAL") so they're easy to compare.
                                # Flip off for production publishing (clean AI image only).

# ── publish_wordpress.py publishing ──
SKIP_PRODUCTS_WITHOUT_PRICE = True   # when True, images whose analysis entry has no scraped
                                # price (scraped.price) are skipped at publish time — they
                                # never upload to WordPress or appear on the page. When
                                # False, everything publishes (price shown only when known).

# ── image_pipeline.py ──
MAX_PROCESSED_BYTES = 204800      # 200 KB cap for processed (grayscale) images
MIN_QUALITY = 20                      # floor for JPEG quality before we start downscaling
MIN_SCALE = 0.25                      # floor for resolution downscale factor

# ── kie_vision.py rate limiter ──
KIE_MAX_REQUESTS_PER_WINDOW = 18      # stay under KIE's ~20 requests/10s account cap
KIE_RATE_WINDOW_SECONDS = 10

# ── zip_export.py ──
ZIP_MAX_AGE_SECONDS = 86400       # sweep exports older than this on every new build

# ── brand filtering / output organization (run_facebook.py) ──
MIN_IMAGES_FOR_KEEP = 2       # a post needs more than 1 image to be kept (see brand_mapping.py)

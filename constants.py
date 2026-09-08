#!/usr/bin/env python3
"""
Central place for tunable pipeline constants and testing toggles.

Actual secrets (API keys, tokens) stay in .env — this file is for behavior
knobs that used to be scattered across individual modules. Flip these
instead of hunting through run_facebook.py / kie_vision.py / etc.
"""

# ── run_facebook.py pipeline toggles ──
FETCH_COMMENTS = False          # skip comment scraping — not needed right now
ANALYZE_IMAGES = True           # run the KIE upload+analyze workflow at all —
                                 # when False, images are still downloaded/processed
                                 # but never sent to KIE (analysis_status: "skipped")
MAX_IMAGES_PER_POST = 2         # cap images downloaded/processed/analyzed per post — for testing
IMAGE_WORKERS = 5               # concurrent per-post image processing/analysis threads
POSTS_PER_SOURCE_DEFAULT = 500  # default --posts-per-source limit for page/group fetch

# ── image_pipeline.py ──
MAX_PROCESSED_BYTES = 200 * 1024  # 200 KB cap for processed (grayscale) images
MIN_QUALITY = 20                  # floor for JPEG quality before we start downscaling
MIN_SCALE = 0.25                  # floor for resolution downscale factor

# ── kie_vision.py rate limiter ──
KIE_MAX_REQUESTS_PER_WINDOW = 18  # stay under KIE's ~20 requests/10s account cap
KIE_RATE_WINDOW_SECONDS = 10

# ── zip_export.py ──
ZIP_MAX_AGE_SECONDS = 24 * 3600   # sweep exports older than this on every new build

#!/usr/bin/env python3
"""
Facebook pipeline — single end-to-end job. Invoked as a subprocess by
job_runner.py. One job does everything for the workspace's ONE brand:

  1. DISCOVERY — resolve every source and fetch its post list first
     (page/group with download_images=False, fetch_extra_images=False; just
     the post ID for a post URL), logging per-source and total found counts
     before any per-post work starts. The brand text filter runs inside the
     scrapers' pagination loop, before a matching post's images are even
     enumerated.
  2. PROCESSING — per discovered post: extra-image discovery via
     last_media_id → download → grayscale/compress → KIE analysis → keep
     only if it has enough images AND its category is selected in the
     workspace (food / non_food) → organize into
     output/<job_id>/<brand-slug>/<category>/post_<id>/.
  3. PUBLISH PREP — per selected category: duplicate-product removal,
     scraper analysis (name/price/description from the brand's own site),
     AI image regeneration + price badges.
  4. PUBLISH — each selected category publishes to its OWN website +
     WordPress page (food and non-food are configured separately in the
     workspace; a category without a page ID is skipped), via
     publish_wordpress.publish_brand().

The workspace's single-brand config arrives as CLI args from job_runner.py:
--brand, --food-publish-target/--food-page-id,
--non-food-publish-target/--non-food-page-id, --image-prompt,
--page-title, --week-start, --categories.

One failing post/source never aborts the whole job — failures are logged
and the run continues. A publish failure DOES fail the job (exit 1).
"""

import argparse
import difflib
import json
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests

import brand_mapping
import fb_client
import image_pipeline
import kie_vision
import scrapers
import generate
from generate import make_comparison_image
from image_pipeline import compress_under_limit
from price_overlay import overlay_price_on_image
from publish_wordpress import publish_brand
from data_store import load_fb_auth, record_scrape_stats
from constants import (
    FETCH_COMMENTS, ANALYZE_IMAGES, MAX_IMAGES_PER_POST, IMAGE_WORKERS,
    POSTS_PER_SOURCE_DEFAULT, MIN_IMAGES_FOR_KEEP,
    AI_IMAGE_COMPARE, AI_IMAGE_MAX_BYTES, AI_IMAGE_WORKERS,
    DEDUPE_PRODUCTS, DEDUPE_THRESHOLD, GENERATE_AI_IMAGES,
    MAX_FOOD_PRODUCTS, MAX_NON_FOOD_PRODUCTS, PAGE_SCAN_WORKERS,
    PRICE_ON_IMAGE, SCRAPER_ANALYSIS, SCRAPER_WORKERS,
    SKIP_PRODUCTS_WITHOUT_PRICE,
)

SOURCE_MAX_ATTEMPTS = 3

# The categories a workspace can select (food / non-food, or both). Anything
# a post is categorized as that isn't selected is discarded after analysis;
# "uncategorized" posts are always kept on disk for debugging but never
# published.
CATEGORIES = ("food", "non_food")


def parse_categories_arg(value: str | None) -> list[str]:
    """Parse the --categories CLI value ("food,non_food") into a validated
    list. Unrecognized/missing values fall back to both categories."""
    selected = [c.strip().lower() for c in (value or "").split(",") if c.strip()]
    valid = [c for c in CATEGORIES if c in selected]
    return valid or list(CATEGORIES)


def _with_source_retries(description: str, fn, max_attempts: int = SOURCE_MAX_ATTEMPTS):
    """Run one source's discovery/processing callable with retries — a
    transient failure (proxy hiccup, temporary block, timeout) gets up to
    `max_attempts` tries with backoff before the source is given up on.
    Returns fn()'s return value, or None if every attempt failed (the
    per-source isolation then just logs it and the job continues with the
    remaining sources)."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e
            print(f"  ⚠️ Attempt {attempt}/{max_attempts} failed for {description}: {e}")
            if attempt < max_attempts:
                wait_time = attempt * 5
                print(f"  ⏳ Retrying source in {wait_time} seconds...")
                time.sleep(wait_time)
    print(f"  ❌ Giving up on {description} after {max_attempts} attempts: {last_error}")
    return None


def parse_date_arg(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d


def make_brand_filter(brand: str | None):
    """Build the post-text brand filter from the workspace's single brand.
    With a brand configured, only posts whose single detected brand is that
    brand pass — every other brand's posts are rejected inside the scraper's
    pagination loop, before their images are even enumerated. Without one,
    any single detected brand passes."""
    if not brand:
        return lambda text: brand_mapping.detect_single_brand(text) is not None
    return lambda text: brand_mapping.detect_single_brand(text) == brand


def make_per_brand_limiter(brand_filter, limit: int):
    """Wrap the brand filter with an acceptance cap (testing aid): once
    `limit` posts of a brand have been accepted, every further post of that
    brand is rejected — for page/group sources this happens inside the
    scraper's pagination loop (the filter IS the text_filter), before that
    post's images are even enumerated. limit <= 0 means unlimited. The
    counter is shared across every source in the job, and each post is
    evaluated exactly once (page/group: at discovery; post URLs: in
    process_post_url), so the cap is per job, not per source."""
    if not limit or limit < 1:
        return brand_filter
    counts: dict[str, int] = {}
    counts_lock = threading.Lock()  # sources are discovered in parallel now

    def limited(text) -> bool:
        brand = brand_mapping.detect_single_brand(text)
        if not brand_filter(text):
            return False
        key = brand or "__unbranded__"   # relaxed sources accept no-brand posts
        with counts_lock:
            if counts.get(key, 0) >= limit:
                return False
            counts[key] = counts.get(key, 0) + 1
        return True

    return limited


def fetch_post_meta_from_html(url: str, cookies: dict | None) -> dict:
    """Best-effort post text/page name/publish time from the post's public
    Open Graph meta tags — the GraphQL comment-fetch flow for a bare post
    URL doesn't return this, so we fall back to a plain HTML fetch."""
    meta = {"text": None, "page_name": None, "published_at": None}
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "en-US,en;q=0.9",
        }
        proxies = fb_client.single_post_image.PROXIES
        resp = requests.get(url, headers=headers, cookies=cookies, proxies=proxies, timeout=20)
        html = resp.text

        desc_match = re.search(r'<meta property="og:description" content="([^"]*)"', html)
        if desc_match:
            meta["text"] = unescape(desc_match.group(1))

        site_match = re.search(r'<meta property="og:site_name" content="([^"]*)"', html)
        title_match = re.search(r'<meta property="og:title" content="([^"]*)"', html)
        if site_match and site_match.group(1):
            meta["page_name"] = unescape(site_match.group(1))
        elif title_match:
            meta["page_name"] = unescape(title_match.group(1))

        pub_match = re.search(r'"publish_time":(\d+)', html) or re.search(r'"creation_time":(\d+)', html)
        if pub_match:
            meta["published_at"] = datetime.fromtimestamp(int(pub_match.group(1)), tz=timezone.utc).isoformat()
    except Exception as e:
        print(f"  ⚠️  Could not fetch post meta from HTML: {e}")
    print(f"  📝 Post text from HTML meta: {(meta.get('text') or '(none found)')[:200]!r}")
    return meta


def _process_one_image(orig_path_str: str, index: int, processed_dir: Path, post_id: str) -> tuple[str, dict, dict]:
    """Process + analyze a single image. Never raises — a failure is recorded
    as analysis_status: 'failed' rather than losing the image entirely."""
    orig_path = Path(orig_path_str)
    image_id = f"image_{index:03d}"
    mapping_filename = f"post_{post_id}_{orig_path.name}"
    analysis = {"brand": None, "product_name": None, "category": None, "subcategory": None, "analysis_status": "failed"}
    processed_filename = None

    try:
        processed_path = image_pipeline.process_image(
            str(orig_path), str(processed_dir / f"{orig_path.stem}_processed.jpg")
        )
        processed_filename = Path(processed_path).name

        if not ANALYZE_IMAGES:
            analysis = {"brand": None, "product_name": None, "category": None, "subcategory": None, "analysis_status": "skipped"}
        else:
            try:
                public_url = kie_vision.upload_image(processed_path)
                result = kie_vision.analyze_product_image(public_url)
                analysis = {**result, "analysis_status": "success"}
            except Exception as e:
                print(f"  ⚠️  KIE analysis failed for {orig_path.name}: {e}")
    except Exception as e:
        print(f"  ⚠️  Image processing failed for {orig_path.name}: {e}")

    if analysis.get("analysis_status") == "success":
        category = analysis.get("category") or "uncategorized"
        product = analysis.get("product_name")
        print(f"  🖼️  {orig_path.name} → {category}" + (f" — {product}" if product else ""))

    image_entry = {
        "image_id": image_id,
        "original_filename": orig_path.name,
        "processed_filename": processed_filename,
        "analysis": analysis,
    }
    mapping_entry = {
        "post_id": post_id,
        "brand": analysis.get("brand"),
        "product_name": analysis.get("product_name"),
        "category": analysis.get("category"),
        "subcategory": analysis.get("subcategory"),
    }
    return mapping_filename, image_entry, mapping_entry


def build_analysis_and_images(post_dir: Path, original_paths: list[str], post_id: str):
    """Process + analyze every image for a post concurrently. A failure on
    one image is recorded as analysis_status: 'failed' and never aborts the
    others. Output order is preserved regardless of completion order."""
    processed_dir = post_dir / "images" / "processed"
    sorted_paths = sorted(original_paths)

    # Stage markers are printed once per post here (single-threaded), NOT
    # inside the per-image workers — per-image prints from the thread pool
    # used to duplicate and interleave mid-line in the job log.
    print("[STAGE] processing_images")
    if ANALYZE_IMAGES:
        print("[STAGE] analyzing_products")

    results: dict[int, tuple[str, dict, dict]] = {}
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as executor:
        futures = {
            executor.submit(_process_one_image, p, i, processed_dir, post_id): i
            for i, p in enumerate(sorted_paths, start=1)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as e:
                print(f"  ⚠️  Unexpected error processing image #{i}: {e}")

    images_list = []
    mapping = {}
    for i in sorted(results):
        mapping_filename, image_entry, mapping_entry = results[i]
        images_list.append(image_entry)
        mapping[mapping_filename] = mapping_entry

    return images_list, mapping


def finalize_post(staging_root: Path, post_id: str, post_url, page_url, page_name, post_text,
                   published_at, comment_count, comments, orig_image_paths) -> dict:
    """Writes the post's data under staging_root/post_<id> — a scratch
    location, not the final output path. organize_or_discard() below splits
    it by each image's KIE category into
    output/<job_id>/<brand>/<category>/post_<id>/ (or deletes it) once the
    image-count/category filters have been applied."""
    post_dir = staging_root / f"post_{post_id}"
    canonical_orig_dir = post_dir / "images" / "original"
    canonical_orig_dir.mkdir(parents=True, exist_ok=True)

    orig_image_paths = orig_image_paths[:MAX_IMAGES_PER_POST]

    final_paths = []
    for p in orig_image_paths:
        p = Path(p)
        if p.parent == canonical_orig_dir:
            final_paths.append(p)
        else:
            dest = canonical_orig_dir / p.name
            try:
                shutil.copy2(p, dest)
                final_paths.append(dest)
            except Exception as e:
                print(f"  ⚠️  Could not copy image {p}: {e}")

    images_list, mapping = build_analysis_and_images(post_dir, [str(p) for p in final_paths], post_id)

    print("[STAGE] creating_json")
    post_json = {
        "post": {
            "post_id": post_id,
            "post_url": post_url,
            "page_url": page_url,
            "page_name": page_name,
            "post_text": post_text,
            "published_at": published_at,
            "comment_count": comment_count,
        },
        "comments": comments,
        "images": images_list,
    }
    (post_dir / "post.json").write_text(json.dumps(post_json, indent=2, ensure_ascii=False), encoding="utf-8")

    analysis_dir = post_dir / "analysis"
    analysis_dir.mkdir(exist_ok=True)
    (analysis_dir / "image_analysis.json").write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  💾 Saved post {post_id} — {len(images_list)} image(s), {len(comments)} comment(s)")
    return mapping


def post_already_output(job_dir: Path, post_id: str) -> bool:
    """A post is 'already handled' if its folder exists anywhere under
    job_dir — either already organized into <brand>/<category>/post_<id>,
    or still sitting in _staging from an interrupted run."""
    return any(job_dir.rglob(f"post_{post_id}"))


def post_qualifies(mapping: dict) -> tuple[bool, str]:
    """Image-requirement filter (see CONTEXT.md): keep the post only if it
    has more than one image. The brand match is already decided by the post
    text (brand_mapping.detect_single_brand) before any images are even
    processed — this does not re-check brand against each image's KIE
    analysis."""
    if len(mapping) < MIN_IMAGES_FOR_KEEP:
        return False, f"only {len(mapping)} image(s), need >= {MIN_IMAGES_FOR_KEEP}"
    return True, "ok"


def organize_or_discard(job_dir: Path, staging_root: Path, post_id: str, text_brand: str,
                        mapping: dict, allowed_categories: list[str]) -> dict:
    """Applies the image-requirement filter, then splits the staged post's
    images BY THEIR OWN KIE category (a real haul post mixes food and
    non-food products, so the category is decided per image, not per post)
    and organizes each group into
    output/<job_id>/<brand>/<category>/post/post_<id>/.

    Each destination post_<id>/ folder holds only the images of its category
    (post.json's image list, originals, processed copies, and the per-post
    analysis are all filtered to match), so every category folder stays
    self-consistent. Images whose category isn't selected in the workspace
    (food / non_food) are discarded; "uncategorized" (null) images always
    stay on disk for debugging, though they are never published.

    Also mirrors each group's images into a flat <brand>/<category>/images/
    folder (filenames prefixed `post_<id>_` to avoid collisions across
    posts) and merges the group's mapping into a category-wide
    <brand>/<category>/image_analysis.json — the file every later stage
    (dedupe, scraper analysis, AI images, publishing) reads.

    Returns the mapping entries that were written (empty dict if the post
    was discarded entirely)."""
    staged_dir = staging_root / f"post_{post_id}"
    keep, reason = post_qualifies(mapping)
    if not keep:
        print(f"  🗑️  Skipping post {post_id} — {reason}")
        shutil.rmtree(staged_dir, ignore_errors=True)
        return {}

    brand_slug = brand_mapping.brand_slug(text_brand)

    # Group the post's images by their own KIE category (null → uncategorized).
    groups: dict[str, dict] = {}
    for filename, entry in mapping.items():
        category = entry.get("category") or "uncategorized"
        groups.setdefault(category, {})[filename] = entry

    # Keep groups whose category is selected; uncategorized always stays on
    # disk (debug-only, never published).
    writable = {category: entries for category, entries in groups.items()
                if category == "uncategorized" or category in allowed_categories}
    skipped = [c for c in groups if c not in writable]
    if skipped:
        skipped_count = sum(len(groups[c]) for c in skipped)
        print(f"  🚫 Post {post_id}: {skipped_count} image(s) in unselected categories ({', '.join(sorted(skipped))}) discarded")
    if not writable:
        print(f"  🗑️  Discarding post {post_id} — none of its image categories are selected in this workspace")
        shutil.rmtree(staged_dir, ignore_errors=True)
        return {}

    written: dict = {}
    try:
        post_json = json.loads((staged_dir / "post.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠️  Could not read staged post.json for {post_id}: {e}")
        post_json = {}

    for category, entries in sorted(writable.items()):
        category_dir = job_dir / brand_slug / category
        dest_dir = category_dir / "post" / f"post_{post_id}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Filter the staged post.json down to this category's images.
        category_post_json = json.loads(json.dumps(post_json))  # deep copy
        keep_orig_names, keep_proc_names = set(), set()
        selected_images = []
        for img in category_post_json.get("images") or []:
            img_category = (img.get("analysis") or {}).get("category") or "uncategorized"
            if img_category != category:
                continue
            selected_images.append(img)
            if img.get("original_filename"):
                keep_orig_names.add(img["original_filename"])
            if img.get("processed_filename"):
                keep_proc_names.add(img["processed_filename"])
        category_post_json["images"] = selected_images
        (dest_dir / "post.json").write_text(
            json.dumps(category_post_json, indent=2, ensure_ascii=False), encoding="utf-8")

        # Copy this category's originals + processed images into the post dir.
        for sub, names in (("original", keep_orig_names), ("processed", keep_proc_names)):
            src_dir = staged_dir / "images" / sub
            if not src_dir.exists() or not names:
                continue
            dst_dir = dest_dir / "images" / sub
            dst_dir.mkdir(parents=True, exist_ok=True)
            for f in src_dir.iterdir():
                if f.is_file() and f.name in names:
                    try:
                        shutil.copy2(f, dst_dir / f.name)
                    except Exception as e:
                        print(f"  ⚠️  Could not copy {f.name} into {brand_slug}/{category}/post/post_{post_id}/: {e}")

        analysis_dir = dest_dir / "analysis"
        analysis_dir.mkdir(exist_ok=True)
        (analysis_dir / "image_analysis.json").write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

        # Flat copy for quick browsing + the combined category-wide mapping.
        flat_images_dir = category_dir / "images"
        flat_images_dir.mkdir(exist_ok=True)
        for name in sorted(keep_orig_names):
            src = dest_dir / "images" / "original" / name
            if src.exists():
                try:
                    shutil.copy2(src, flat_images_dir / f"post_{post_id}_{name}")
                except Exception as e:
                    print(f"  ⚠️  Could not copy {name} into {brand_slug}/{category}/images/: {e}")

        category_analysis_file = category_dir / "image_analysis.json"
        combined = {}
        if category_analysis_file.exists():
            try:
                combined = json.loads(category_analysis_file.read_text(encoding="utf-8"))
            except Exception:
                combined = {}
        combined.update(entries)
        category_analysis_file.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"  📁 Kept {len(entries)} image(s) of post {post_id} → {brand_slug}/{category}/")
        written.update(entries)

    shutil.rmtree(staged_dir, ignore_errors=True)
    return written


def discover_post_url(url: str, cookies: dict) -> str:
    """Discovery phase for a bare post URL: resolve its post ID only — the
    post's text/media/images are all fetched in process_post_url() below.
    Raises on resolution failure so _with_source_retries retries it."""
    post_id = fb_client.resolve_post_id(url, cookies=cookies)
    if not post_id:
        raise RuntimeError(f"Could not resolve post ID from {url}")
    return post_id


def process_post_url(job_dir: Path, post_id: str, url: str, cookies: dict, skip_ids: set,
                     brand_filter, allowed_categories: list[str]) -> tuple[str | None, dict]:
    """Full processing for one bare post URL: meta fetch → brand check →
    album download via media_id → process/analyze → organize. The brand
    filter here is the effective (limit-aware) one — this is the only place
    a post URL's text is checked, so the testing cap applies to post URLs
    too."""
    if post_id in skip_ids:
        print(f"  ⏭️  Already processed in a previous run: {post_id}")
        return None, {}
    if post_already_output(job_dir, post_id):
        print(f"  ⏭️  Already processed in this job: {post_id}")
        return None, {}

    meta = fetch_post_meta_from_html(url, cookies)

    text_brand = brand_mapping.detect_single_brand(meta.get("text"))
    print(f"  🏷️  Detected brand from post text: {text_brand or '(none / ambiguous)'}")
    if not text_brand:
        print(f"  🗑️  Skipping post {post_id} — no single retailer brand detected in post text")
        return post_id, {}
    if not brand_filter(meta.get("text")):
        print(f"  🗑️  Skipping post {post_id} — brand '{text_brand}' does not match this workspace's brand")
        return post_id, {}

    post_info = None
    comments = []
    if FETCH_COMMENTS:
        print("[STAGE] fetching_comments")
        try:
            comments, post_info = fb_client.fetch_comments_for_post(post_id, cookies=cookies)
        except Exception as e:
            print(f"  ⚠️  Comment fetch failed for {post_id}: {e}")
    else:
        # Comment scraping is disabled, but the comments API response is
        # still the only source of media_id (needed for images) for a bare
        # post URL — one request (first page, no replies) is enough since
        # media_id comes from the first comment edge.
        try:
            _, post_info = fb_client.fetch_comments_for_post(post_id, cookies=cookies,
                                                             max_pages=1, with_replies=False)
        except Exception as e:
            print(f"  ⚠️  Could not resolve media_id for {post_id}: {e}")

    staging_root = job_dir / "_staging"
    image_paths = []
    if post_info and post_info.get("media_id"):
        print("[STAGE] downloading_images")
        orig_dir = staging_root / f"post_{post_id}" / "images" / "original"
        try:
            image_paths = fb_client.fetch_single_post_images(
                post_info["media_id"], post_id, str(orig_dir), cookies, max_images=MAX_IMAGES_PER_POST
            )
        except Exception as e:
            print(f"  ⚠️  Image fetch failed for {post_id}: {e}")

    mapping = finalize_post(
        staging_root, post_id, post_url=url, page_url=url,
        page_name=meta.get("page_name"), post_text=meta.get("text"),
        published_at=meta.get("published_at"), comment_count=len(comments),
        comments=comments, orig_image_paths=image_paths,
    )
    written = organize_or_discard(job_dir, staging_root, post_id, text_brand, mapping, allowed_categories)
    return post_id, written


def discover_page_or_group_posts(job_dir: Path, url: str, src_type: str, cookies: dict,
                                  min_comments: int, start_dt, end_dt, posts_per_source: int,
                                  brand_filter) -> dict | None:
    """Discovery phase for a page/group source: resolves the source and
    fetches its post list (text, permalink, comment count, image URLs, etc.)
    with download_images=False — every post's images are discovered/counted
    but their bytes are NOT downloaded yet.

    A brand-keyword text_filter is also passed straight into the scraper's
    fetch_posts(): it runs the instant a post's text is available, BEFORE
    that post's images are even enumerated — so a post with no single
    detected retailer never triggers extract_media() at all (see CONTEXT.md
    Design Decision 19).

    fetch_extra_images=False on top of that: for a post with more photos
    than its own GraphQL node carries, only those node photos are discovered
    here — the paginated lookup for the rest (one GraphQL request per extra
    photo) is deferred to process_page_or_group_posts() ->
    fb_client.download_pending_post_images(), via the `last_media_id`
    recorded on each post. Returns everything process_page_or_group_posts()
    below needs, or None if the source couldn't be resolved."""
    print("[STAGE] fetching_posts")
    # Unique per call (not just per URL) so a source-level retry gets a clean
    # scratch dir — otherwise the scraper's post_already_exists() check would
    # skip every post saved by the failed attempt and drop it from the
    # returned post list.
    scratch_root = job_dir / f"_scratch_{src_type}_{abs(hash(url)) % 100000}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
    text_filter = brand_filter

    if src_type == "group":
        source_id = fb_client.resolve_group_id(url, cookies=cookies)
        if not source_id:
            raise RuntimeError(f"Could not resolve group ID from {url}")
        posts = fb_client.fetch_group_posts(
            source_id, limit=posts_per_source, min_comments=min_comments,
            start_date=start_dt, end_date=end_dt, save_root=str(scratch_root),
            max_images_per_post=MAX_IMAGES_PER_POST, download_images=False,
            text_filter=text_filter, fetch_extra_images=False,
        )
        text_key, name_key = "message", "group_name"
    else:
        source_id = fb_client.resolve_page_id(url, cookies=cookies)
        if not source_id:
            raise RuntimeError(f"Could not resolve page ID from {url}")
        posts = fb_client.fetch_page_posts(
            source_id, limit=posts_per_source, min_comments=min_comments,
            start_date=start_dt, end_date=end_dt, save_root=str(scratch_root),
            max_images_per_post=MAX_IMAGES_PER_POST, download_images=False,
            text_filter=text_filter, fetch_extra_images=False,
        )
        text_key, name_key = "text", "page_name"

    return {
        "url": url, "posts": posts, "text_key": text_key, "name_key": name_key,
        "scratch_root": scratch_root, "src_type": src_type,
    }


def process_page_or_group_posts(job_dir: Path, discovered: dict, cookies: dict,
                                 skip_ids: set, brand_filter,
                                 allowed_categories: list[str]) -> tuple[list[str], dict]:
    """Processing phase for one discovered page/group source: brand
    re-check, comments, image download/processing/analysis, and
    filter/organize — for every post discover_page_or_group_posts() already
    found."""
    url = discovered["url"]
    posts = discovered["posts"]
    text_key = discovered["text_key"]
    name_key = discovered["name_key"]
    scratch_root = discovered["scratch_root"]
    src_type = discovered["src_type"]

    processed_ids = []
    all_mapping = {}
    staging_root = job_dir / "_staging"

    for post in posts:
        post_id = post.get("post_id")
        if not post_id or post_id in skip_ids:
            continue
        if post_already_output(job_dir, post_id):
            continue

        # The real brand filter already ran inside discover_page_or_group_posts()'s
        # text_filter, before this post's images were even enumerated — every
        # post reaching this line already passed it. This just re-derives the
        # actual brand string (the filter only returned True/False) for
        # organize_or_discard() below; it's a defensive fallback, not the
        # primary filter point. NOTE: the plain filter is used here, NOT the
        # limit-aware one — the limiter's counter already counted this post
        # at discovery, and counting it again here would push it over the cap.
        text_brand = brand_mapping.detect_single_brand(post.get(text_key))
        relaxed_brand = discovered.get("relaxed_brand")
        if not text_brand and relaxed_brand:
            # Source URL matches the workspace's brand (e.g. facebook.com/ALDI.USA
            # for ALDI) — the source IS the brand, so a post whose text doesn't
            # name any brand is still the brand's post. Treat it as such.
            text_brand = relaxed_brand
            print(f"  🔗 Post {post_id} has no brand in text — source URL matches '{relaxed_brand}', treating as {relaxed_brand}")
        print(f"  🏷️  Post {post_id} brand: {text_brand or '(none / ambiguous)'} — text: {(post.get(text_key) or '')[:150]!r}")
        if not text_brand:
            print(f"  🗑️  Skipping post {post_id} — no single retailer brand detected in post text")
            processed_ids.append(post_id)
            continue
        if text_brand != relaxed_brand and not brand_filter(post.get(text_key)):
            print(f"  🗑️  Skipping post {post_id} — brand '{text_brand}' does not match this workspace's brand")
            processed_ids.append(post_id)
            continue

        comments = []
        if FETCH_COMMENTS:
            print("[STAGE] fetching_comments")
            try:
                comments, _ = fb_client.fetch_comments_for_post(post_id, cookies=cookies)
            except Exception as e:
                print(f"  ⚠️  Comment fetch failed for {post_id}: {e}")
                comments = []

        print("[STAGE] downloading_images")
        try:
            orig_images = fb_client.download_pending_post_images(
                src_type, post, str(scratch_root), max_images=MAX_IMAGES_PER_POST
            )
        except Exception as e:
            print(f"  ⚠️  Image download failed for {post_id}: {e}")
            orig_images = []

        mapping = finalize_post(
            staging_root, post_id,
            post_url=post.get("permalink") or url, page_url=url,
            page_name=post.get(name_key), post_text=post.get(text_key),
            published_at=post.get("published_at"),
            comment_count=post.get("comment_count", len(comments)),
            comments=comments, orig_image_paths=orig_images,
        )
        written = organize_or_discard(job_dir, staging_root, post_id, text_brand, mapping, allowed_categories)
        all_mapping.update(written)
        processed_ids.append(post_id)

    shutil.rmtree(scratch_root, ignore_errors=True)
    return processed_ids, all_mapping


# ── PUBLISH PREP — duplicate removal, scraper analysis, AI images ────────────
# These stages run per selected category on the organized output
# (output/<job_id>/<brand-slug>/<category>/) before publishing. They are
# idempotent (flags on each analysis entry — scraped / price_overlaid /
# ai_image — make re-runs skip already-processed images).

def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/extra whitespace — the comparison form
    for duplicate-product detection."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in name.lower()).split())


def is_duplicate_name(name: str, seen_norms: list[str], threshold: float) -> bool:
    """True if `name` (already normalized) is near-identical to any name seen
    so far — difflib similarity ratio >= threshold. Exact matches after
    normalization always count."""
    return any(name == other or difflib.SequenceMatcher(None, name, other).ratio() >= threshold
               for other in seen_norms)


def dedupe_category_images(job_dir: Path, brand_slug: str, category: str) -> int:
    """Remove duplicate products from <category>/image_analysis.json —
    different posts/pages often show the same product, so before scraper
    analysis and publishing, every entry's product name is normalized and
    compared against the names already accepted (difflib ratio >=
    constants.DEDUPE_THRESHOLD); duplicates are dropped from the JSON and
    their flat image files deleted, so scrapers and the WordPress publisher
    never see them. First occurrence wins (JSON insertion order). Toggled by
    constants.DEDUPE_PRODUCTS. Returns the number of duplicates removed.
    Never raises."""
    if not DEDUPE_PRODUCTS:
        return 0
    analysis_file = job_dir / brand_slug / category / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    images_dir = job_dir / brand_slug / category / "images"
    # Popularity count FIRST (before duplicates are dropped): how many posts
    # showed each product name. The kept entry records it as "appearances" —
    # the product-cap stage later prioritizes products seen on many pages.
    name_counts: dict[str, int] = {}
    for entry in analysis.values():
        name = entry.get("product_name")
        if name:
            norm = normalize_name(name)
            name_counts[norm] = name_counts.get(norm, 0) + 1

    images_dir = job_dir / brand_slug / category / "images"
    seen_norms: list[str] = []
    duplicates: list[str] = []
    for filename, entry in analysis.items():
        name = entry.get("product_name")
        if not name:
            continue  # no KIE name — never treated as a duplicate
        norm = normalize_name(name)
        if is_duplicate_name(norm, seen_norms, DEDUPE_THRESHOLD):
            duplicates.append(filename)
            print(f"  ⏭️  Duplicate: {filename} — '{name}'")
        else:
            seen_norms.append(norm)
            entry["appearances"] = name_counts.get(norm, 1)

    if not duplicates:
        print(f"✅ {category}: no duplicate products found.")
        return 0

    for filename in duplicates:
        del analysis[filename]
        img_path = images_dir / filename
        try:
            if img_path.exists():
                img_path.unlink()
        except Exception as e:
            print(f"  ⚠️  Could not remove duplicate image {filename}: {e}")

    try:
        analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Could not write deduped analysis back to {analysis_file}: {e}")
    print(f"⏭️  Removed {len(duplicates)} duplicate product(s) from {analysis_file.name}.")
    return len(duplicates)


def cap_category_products(job_dir: Path, brand_slug: str, category: str) -> int:
    """Trim the clean dataset (post-dedupe, post-scrape) to at most
    MAX_FOOD_PRODUCTS / MAX_NON_FOOD_PRODUCTS entries per category, BEFORE AI
    image generation — only products that will actually be published should
    cost AI-generation credits. Removed products' entries and image files are
    deleted, same as dedupe.

    Selection priority (food): products that appeared on many posts first
    (the "appearances" count dedupe recorded), then all Meat & Seafood
    products, then the rest spread round-robin across subcategories so every
    section of the published page stays populated. Non-food has no
    subcategories: duplicates first, then original order. Returns the number
    of products removed. Never raises."""
    cap = MAX_FOOD_PRODUCTS if category == "food" else MAX_NON_FOOD_PRODUCTS
    if not cap or cap < 1:
        return 0
    analysis_file = job_dir / brand_slug / category / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    images_dir = job_dir / brand_slug / category / "images"

    # No-price products are never published (SKIP_PRODUCTS_WITHOUT_PRICE) —
    # drop them here so they don't consume cap slots either.
    if SKIP_PRODUCTS_WITHOUT_PRICE:
        no_price = [fn for fn, e in analysis.items()
                    if not (e.get("scraped") or {}).get("price")]
        for fn in no_price:
            del analysis[fn]
            img = images_dir / fn
            try:
                if img.exists():
                    img.unlink()
            except Exception as e:
                print(f"  ⚠️  Could not remove image {fn}: {e}")
        if no_price:
            print(f"  ⏭️  Removed {len(no_price)} product(s) without a scraped price before capping")

    order = list(analysis.keys())
    if len(order) <= cap:
        print(f"✅ {category}: {len(analysis)} product(s) within cap of {cap} — nothing removed.")
        return 0

    def _appearances(fn):
        return (analysis[fn] or {}).get("appearances") or 1

    def _is_meat(fn):
        return (analysis[fn].get("subcategory") == "Meat & Seafood")

    selected: list[str] = []

    # 1) Crowd-verified products first — most-seen across posts, original
    # order breaking ties.
    dupes = sorted((fn for fn in order if _appearances(fn) >= 2),
                   key=lambda fn: (-_appearances(fn), order.index(fn)))
    selected.extend(dupes[:cap])

    # 2) All Meat & Seafood products (food only).
    if category == "food" and len(selected) < cap:
        for fn in order:
            if len(selected) >= cap:
                break
            if fn not in selected and _is_meat(fn):
                selected.append(fn)

    # 3) Fill remaining slots round-robin across subcategories (original
    # order within each) so every page section stays populated.
    by_sub: dict[str, list[str]] = {}
    for fn in order:
        if fn not in selected:
            by_sub.setdefault(analysis[fn].get("subcategory") or "__other__", []).append(fn)
    subs = list(by_sub)
    while len(selected) < cap and any(by_sub[s] for s in subs):
        for sub in subs:
            if by_sub[sub]:
                selected.append(by_sub[sub].pop(0))
                if len(selected) >= cap:
                    break

    removed = [fn for fn in order if fn not in selected]
    for fn in removed:
        del analysis[fn]
        img = images_dir / fn
        try:
            if img.exists():
                img.unlink()
        except Exception as e:
            print(f"  ⚠️  Could not remove image {fn}: {e}")

    try:
        analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Could not write capped analysis back to {analysis_file}: {e}")

    dup_kept = sum(1 for fn in selected if _appearances(fn) >= 2)
    meat_kept = sum(1 for fn in selected if _is_meat(fn))
    print(f"✂️  {category}: capped {len(order)} → {len(selected)} product(s), removed {len(removed)} "
          f"(multi-post products: {dup_kept}, Meat & Seafood: {meat_kept})")
    return len(removed)


def enrich_images_with_scrapes(job_dir: Path, brand: str, brand_slug: str, category: str) -> int:
    """Scraper analysis: for every image in this category whose KIE analysis
    found a product name, look that product up on the brand's own site
    (scrapers/) and attach {name, price, size, description, product_url} to
    its analysis entry in <category>/image_analysis.json — publish_brand()
    renders those on the WordPress page. Toggled by
    constants.SCRAPER_ANALYSIS. Returns the number of images enriched. Never
    raises — a failed lookup just leaves that image without scraped data."""
    if not SCRAPER_ANALYSIS:
        return 0
    searcher = scrapers.get_searcher(brand)
    if not searcher:
        print(f"ℹ️  No product scraper for {brand} — skipping scraper analysis.")
        return 0

    analysis_file = job_dir / brand_slug / category / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    to_scrape = {k: v for k, v in analysis.items() if v.get("product_name")}
    if not to_scrape:
        print(f"ℹ️  {category}: no images with KIE product names — nothing to scrape.")
        return 0

    print("\n" + "=" * 70)
    print(f"SCRAPER ANALYSIS — looking up {len(to_scrape)} product(s) on {brand} ({category})")
    print("=" * 70)

    enriched = 0
    with_price = 0
    pending = [(fn, entry) for fn, entry in analysis.items()
               if entry.get("product_name") and not entry.get("scraped")]

    # Parallel keyword scraping: SCRAPER_WORKERS threads search concurrently.
    # Each thread gets its OWN searcher (fresh curl_cffi session — sessions
    # are not thread-safe), warmed once per thread. Results are merged back
    # into the analysis dict in the main thread, keyed by filename, so the
    # JSON write below stays identical to the sequential version.
    thread_local = threading.local()

    def _thread_searcher():
        s = getattr(thread_local, "searcher", None)
        if s is None:
            s = scrapers.get_searcher(brand)
            try:
                s.warmup()
            except Exception as e:
                print(f"  ⚠️  Scraper warmup failed: {e}")
            thread_local.searcher = s
        return s

    def _scrape_one(item):
        filename, entry = item
        thread_searcher = _thread_searcher()
        query = entry.get("product_name")
        # Target, Costco and Five Below (Instacart) search work best with the
        # KIE brand prepended to the product name (e.g. "Amos Peelerz Mummies")
        if brand in ("Target", "Costco", "Five Below") and entry.get("brand"):
            query = f"{entry['brand']} {query}"
        try:
            result = thread_searcher.search(query)
        except Exception as e:
            print(f"  ⚠️  Scrape failed for {filename} ({entry.get('product_name')!r}): {e}")
            result = None
        time.sleep(0.5)  # be polite to the retailer's site
        return item, result

    if pending:
        print(f"  🧵 Scraping {len(pending)} keyword(s) with {SCRAPER_WORKERS} parallel thread(s)")
    with ThreadPoolExecutor(max_workers=SCRAPER_WORKERS) as executor:
        for (filename, entry), result in executor.map(_scrape_one, pending):
            if result:
                entry["scraped"] = {
                    "name": result.get("name"),
                    "price": result.get("price"),
                    "size": result.get("size"),
                    "description": result.get("description"),
                    "product_url": result.get("product_url"),
                }
                enriched += 1
                if result.get("price"):
                    with_price += 1
                print(f"  🔗 {filename} → {result.get('name')!r} — {result.get('price') or 'price not listed'}")
            else:
                print(f"  🚫 {filename} — no product match for {entry.get('product_name')!r}")

    try:
        analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Could not write enriched analysis back to {analysis_file}: {e}")

    try:
        record_scrape_stats(brand, brand_slug, category, job_dir.name,
                            total=len(to_scrape), found=enriched, with_price=with_price)
    except Exception as e:
        print(f"  ⚠️  Could not record scrape stats: {e}")

    print(f"✅ Scraper analysis: enriched {enriched}/{len(to_scrape)} {category} image(s).")
    return enriched


def apply_price_badges(job_dir: Path, brand_slug: str, category: str) -> int:
    """Stamp scraped prices onto this category's images (used when AI
    generation is off — the badge goes on the original). Marks entries
    price_overlaid so images never get double-badged on re-runs. Returns the
    number badged."""
    if not PRICE_ON_IMAGE:
        return 0
    analysis_file = job_dir / brand_slug / category / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    images_dir = job_dir / brand_slug / category / "images"
    badged = 0
    for filename, entry in analysis.items():
        if entry.get("price_overlaid"):
            continue
        price = (entry.get("scraped") or {}).get("price")
        img_path = images_dir / filename
        if price and img_path.exists() and overlay_price_on_image(img_path, price):
            entry["price_overlaid"] = True
            badged += 1
            print(f"  🏷️  Price {price} stamped onto {filename}")

    if badged:
        try:
            analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"  ⚠️  Could not write price-overlay flags back to {analysis_file}: {e}")
    return badged


def render_image_prompt(template: str, brand: str, entry: dict) -> str:
    """Prepare the workspace image prompt for sending: strip comment lines
    (lines starting with '#'), then substitute {store} and {product_name}
    from the KIE analysis + scraped data. Unknown placeholders are left
    untouched."""
    product_name = (entry.get("scraped") or {}).get("name") or entry.get("product_name") or ""
    body = "\n".join(
        line for line in template.splitlines()
        if not line.lstrip().startswith("#")
    )
    body = body.replace("{store}", brand).replace("{product_name}", product_name)
    return body.strip()


def generate_ai_images(job_dir: Path, brand: str, brand_slug: str, category: str,
                       image_prompt: str | None = None) -> int:
    """AI image generation stage — the last step before publishing. For every
    image in this category, create a new AI-generated version via KIE
    (generate.py, nano-banana-pro) and use it for publishing instead of the
    original: the upload copy and the AI result are both kept under
    constants.AI_IMAGE_MAX_BYTES (quality-first compression,
    image_pipeline.compress_under_limit — no unnecessary quality loss). The
    price badge is applied AFTER generation — onto the AI image when one was
    produced, otherwise onto the original. Prompt: the workspace image
    prompt (--image-prompt) — without one, generation is skipped. Toggled by
    constants.GENERATE_AI_IMAGES. Returns the number of images regenerated.
    Never raises per image — a failure just publishes the original."""
    if not GENERATE_AI_IMAGES:
        apply_price_badges(job_dir, brand_slug, category)
        return 0

    analysis_file = job_dir / brand_slug / category / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    images_dir = job_dir / brand_slug / category / "images"
    entries = {fn: e for fn, e in analysis.items() if (images_dir / fn).exists()}
    if SKIP_PRODUCTS_WITHOUT_PRICE:
        # Same rule as publish_wordpress.publish_brand: products without a
        # scraped price are never published, so don't waste KIE credits
        # generating AI versions of them either.
        no_price = [fn for fn, e in entries.items()
                    if not (e.get("scraped") or {}).get("price")]
        for fn in no_price:
            entries.pop(fn)
        if no_price:
            print(f"⏭️  Skipping AI generation for {len(no_price)} image(s) "
                  f"without a scraped price (SKIP_PRODUCTS_WITHOUT_PRICE)")
    if not entries:
        return 0

    prompt_template = (image_prompt or "").strip()
    if not prompt_template:
        print("⚠️  No AI image prompt configured for this workspace — skipping AI generation.")
        apply_price_badges(job_dir, brand_slug, category)
        return 0
    scratch_dir = job_dir / f"_ai_{brand_slug}"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 70)
    print(f"AI IMAGE GENERATION — regenerating {len(entries)} {category} image(s) via KIE nano-banana-pro")
    print("=" * 70)

    generated = 0

    # Parallel AI generation: AI_IMAGE_WORKERS threads each run the full
    # upload→createTask→poll→download pipeline for a different image, so the
    # long poll waits (15-20s per task) overlap instead of stacking up. All
    # KIE calls are paced through the shared thread-safe rate limiter, and
    # each worker touches only its own files + its own analysis entry, so
    # there is no shared mutable state. The JSON write below happens once,
    # after all workers finish.
    def _generate_one(item):
        filename, entry = item
        img_path = images_dir / filename
        price = (entry.get("scraped") or {}).get("price")
        ok = False

        if not entry.get("ai_image"):  # already generated in a previous run — don't regenerate
            try:
                public_url = generate.upload_image(str(img_path))
                kie_vision.rate_limiter.acquire()
                prompt = render_image_prompt(prompt_template, brand, entry)
                task_id = generate.create_task(public_url, prompt=prompt)
                result_url = generate.poll_task(task_id)
                result_path = scratch_dir / f"result_{filename}"
                generate.download_image(result_url, str(result_path))
                compress_under_limit(str(result_path), AI_IMAGE_MAX_BYTES)
                if AI_IMAGE_COMPARE:
                    # Comparison sheet replaces the published image: AI result
                    # on top, original below, both labeled — easy to compare.
                    if make_comparison_image(result_path, img_path, result_path):
                        print(f"  🔀 {filename} — comparison sheet (AI top / original below)")
                shutil.move(str(result_path), str(img_path))
                entry["ai_image"] = True
                ok = True
                print(f"  🎨 {filename} — AI image generated")
            except Exception as e:
                print(f"  ⚠️  AI generation failed for {filename}: {e} — publishing the original")

        # Price badge goes on the AI image when one exists, else the original.
        if price and PRICE_ON_IMAGE and not entry.get("price_overlaid"):
            if overlay_price_on_image(img_path, price):
                entry["price_overlaid"] = True
                print(f"  🏷️  Price {price} stamped onto {filename}")
        return ok

    with ThreadPoolExecutor(max_workers=AI_IMAGE_WORKERS) as executor:
        for ok in executor.map(_generate_one, entries.items()):
            if ok:
                generated += 1

    shutil.rmtree(scratch_dir, ignore_errors=True)
    try:
        analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Could not write AI-image flags back to {analysis_file}: {e}")
    print(f"✅ AI image generation: {generated}/{len(entries)} {category} image(s) regenerated.")
    return generated


def main():
    parser = argparse.ArgumentParser(description="Facebook product-image analysis + publishing pipeline")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--sources-file", required=True, help="JSON file: [{\"url\":..., \"type\": \"page|group|post|null\"}]")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD (page/group sources only)")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD (page/group sources only, default: today)")
    parser.add_argument("--min-comments", type=int, default=0)
    parser.add_argument("--skip-post-ids-file", default=None, help="JSON list of post_ids to skip (cross-run dedup)")
    parser.add_argument("--brand", default=None,
                        help="The workspace's single canonical retailer brand — only posts "
                             "whose detected brand matches are scraped/published")
    parser.add_argument("--posts-per-source", type=int, default=POSTS_PER_SOURCE_DEFAULT)
    parser.add_argument("--brand-post-limit", type=int, default=0,
                        help="Testing cap: max posts accepted across all sources "
                             "(0 = unlimited).")
    parser.add_argument("--output-root", default="output")
    parser.add_argument("--food-publish-target", default="retailshout", choices=["retailshout", "aos"])
    parser.add_argument("--food-page-id", default=None,
                        help="Food WordPress page ID — food publishing is skipped when absent")
    parser.add_argument("--non-food-publish-target", default="retailshout", choices=["retailshout", "aos"])
    parser.add_argument("--non-food-page-id", default=None,
                        help="Non-food WordPress page ID — non-food publishing is skipped when absent")
    parser.add_argument("--image-prompt", default=None,
                        help="Workspace AI image prompt — without one, AI generation is skipped")
    parser.add_argument("--page-title", default=None,
                        help="WordPress page title template ({brand}/{date_range} placeholders, workspace config)")
    parser.add_argument("--week-start", default=None,
                        help="Week start day for the page title's date window (e.g. friday for ALDI, tuesday for Publix)")
    parser.add_argument("--categories", default="food,non_food",
                        help="Comma-separated categories to keep + publish: food, non_food, or both")
    args = parser.parse_args()

    categories = parse_categories_arg(args.categories)
    brand_slug = brand_mapping.brand_slug(args.brand) if args.brand else None

    brand_filter = make_brand_filter(args.brand)
    if args.brand:
        print(f"Brand filter: only {args.brand} posts pass — every other brand's posts are skipped.")
    else:
        print("Brand filter: no brand configured — every single-detected brand passes.")
    effective_filter = make_per_brand_limiter(brand_filter, args.brand_post_limit)
    if args.brand_post_limit and args.brand_post_limit > 0:
        print(f"Post limit: max {args.brand_post_limit} post(s) (testing mode).")

    skip_ids = set()
    if args.skip_post_ids_file and Path(args.skip_post_ids_file).exists():
        skip_ids = set(json.loads(Path(args.skip_post_ids_file).read_text(encoding="utf-8")))

    start_dt = parse_date_arg(args.start_date, end_of_day=False)
    end_dt = parse_date_arg(args.end_date, end_of_day=True) or datetime.now(timezone.utc)

    fb_auth = load_fb_auth()
    cookies, _proxies = fb_client.apply_auth(fb_auth.get("cookie_string", ""), fb_auth.get("fb_dtsg", ""))
    if not cookies:
        print("⚠️  No Facebook session cookie configured (Settings page) — public-only scraping, may be unreliable.")

    sources = json.loads(Path(args.sources_file).read_text(encoding="utf-8"))

    job_dir = Path(args.output_root) / args.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def _publish_dest(category: str, page_id: str | None, target: str) -> str:
        return f"{category} → {target} (page {page_id})" if page_id else f"{category} → not configured (skipped)"

    print("=" * 70)
    print(f"Facebook pipeline — job {args.job_id}")
    print(f"Brand: {args.brand or '(any)'}  |  Categories: {', '.join(categories)}")
    print(f"Publishing: {_publish_dest('Food', args.food_page_id, args.food_publish_target)}  |  "
          f"{_publish_dest('Non-Food', args.non_food_page_id, args.non_food_publish_target)}")
    print(f"Sources: {len(sources)}  |  Date window: {start_dt or '(open)'} → {end_dt}  |  "
          f"Min comments: {args.min_comments}")
    print("=" * 70)

    # ── Phase 1: discovery — scrape every post link from every source first,
    # before any per-post brand detection / image processing / KIE analysis
    # starts. This surfaces "how much is there to process" up front instead
    # of interleaving it with the (much slower) processing phase.
    print("\n" + "=" * 70)
    print("PHASE 1/3 — Discovering posts from all sources")
    print("=" * 70)

    discovered_post_sources: list[tuple[str, str]] = []       # (url, post_id)
    discovered_page_group_sources: list[dict] = []            # discover_page_or_group_posts() results
    total_found = 0

    def _discover_source(idx: int, src: dict):
        """Discovery for ONE source — runs in a worker thread. Returns
        ('post', url, post_id) / ('page_group', discovered_dict) / None."""
        url = src.get("url", "").strip()
        if not url:
            return None
        src_type = src.get("type")
        if src_type not in ("post", "page", "group"):
            print(f"⚠️  Source has no valid type set ({src_type!r}) — defaulting to 'page'. "
                  f"Check workspaces.json if this wasn't submitted through the UI.")
            src_type = "page"
        print(f"\n[{idx}/{len(sources)}] Discovering: {url}  (type={src_type})")

        try:
            if src_type == "post":
                post_id = _with_source_retries(url, lambda: discover_post_url(url, cookies))
                if post_id:
                    print(f"  🔗 Found 1 post")
                    return ("post", url, post_id)
                return None

            # A source URL that contains the brand name (e.g.
            # facebook.com/ALDI.USA for brand ALDI) IS the brand's own
            # page/group — its posts often don't name the brand in the
            # text, so accept no-brand posts there too. Posts detecting a
            # DIFFERENT brand are still rejected.
            def _relaxed(text, _brand=args.brand):
                return brand_mapping.detect_single_brand(text) in (_brand, None)
            relaxed = bool(args.brand) and args.brand.lower() in url.lower()
            src_filter = make_per_brand_limiter(_relaxed, args.brand_post_limit) if relaxed else effective_filter
            if relaxed:
                print("  🔗 Source URL contains the brand — posts without brand text are accepted too")
            discovered = _with_source_retries(url, lambda: discover_page_or_group_posts(
                job_dir, url, src_type, cookies, args.min_comments,
                start_dt, end_dt, args.posts_per_source, src_filter,
            ))
            if discovered is not None:
                discovered["relaxed_brand"] = args.brand if relaxed else None
                print(f"  🔗 Found {len(discovered['posts'])} post(s)")
                return ("page_group", discovered)
        except Exception as e:
            print(f"❌ Discovery failed entirely for source: {url}: {e}")
        return None

    # Parallel source discovery: PAGE_SCAN_WORKERS threads scan different
    # page/group URLs concurrently — the post-list pagination of one source
    # (the slow part) overlaps the others'. The scrapers are per-call
    # thread-safe (page/group id passed through, not set on module globals),
    # and results are merged back in the main thread in source order.
    worker_count = max(1, min(PAGE_SCAN_WORKERS, len(sources)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for result in executor.map(
            lambda pair: _discover_source(pair[0], pair[1]),
            enumerate(sources, start=1),
        ):
            if result is None:
                continue
            if result[0] == "post":
                _, url, post_id = result
                discovered_post_sources.append((url, post_id))
                total_found += 1
            else:
                discovered = result[1]
                discovered_page_group_sources.append(discovered)
                total_found += len(discovered["posts"])

    print(f"\n✅ Discovery complete — found {total_found} post(s) across "
          f"{len(discovered_post_sources) + len(discovered_page_group_sources)}/{len(sources)} source(s).")

    # Full list of discovered post links (log + discovered_posts.txt in the
    # job dir) so the run can be verified at a glance.
    link_lines = []
    for url, _post_id in discovered_post_sources:
        link_lines.append(url)
    for discovered in discovered_page_group_sources:
        if not discovered["posts"]:
            continue
        link_lines.append(f"{discovered['url']}  ({len(discovered['posts'])} post(s))")
        for post in discovered["posts"]:
            link = post.get("permalink") or f"https://www.facebook.com/{post.get('post_id')}"
            link_lines.append(f"    • {link}")
    if link_lines:
        print("\n📋 Discovered post links:")
        for line in link_lines:
            print(f"  {line}")
        try:
            (job_dir / "discovered_posts.txt").write_text("\n".join(link_lines) + "\n", encoding="utf-8")
        except Exception as e:
            print(f"⚠️  Could not write discovered_posts.txt: {e}")

    # ── Phase 2: processing — for everything Phase 1 found: extra-image
    # discovery via last_media_id → download → process → KIE analysis →
    # image-count + category filters → organize into
    # <brand-slug>/<category>/post_<id>/.
    print("\n" + "=" * 70)
    print("PHASE 2/3 — Processing discovered posts")
    print("=" * 70)

    all_processed_ids: list[str] = []
    all_mapping: dict = {}

    for url, post_id in discovered_post_sources:
        print(f"\nProcessing post: {url}")
        result = _with_source_retries(
            url, lambda: process_post_url(job_dir, post_id, url, cookies, skip_ids,
                                          effective_filter, categories))
        if result:
            processed_id, mapping = result
            if processed_id:
                all_processed_ids.append(processed_id)
                all_mapping.update(mapping)

    for discovered in discovered_page_group_sources:
        print(f"\nProcessing source: {discovered['url']}  ({len(discovered['posts'])} post(s))")
        result = _with_source_retries(
            discovered["url"],
            lambda d=discovered: process_page_or_group_posts(job_dir, d, cookies, skip_ids,
                                                             brand_filter, categories))
        if result:
            ids, mapping = result
            all_processed_ids.extend(ids)
            all_mapping.update(mapping)

    shutil.rmtree(job_dir / "_staging", ignore_errors=True)

    # Number every entry (1, 2, 3, ...) in insertion order for easy later reference —
    # only in the job-wide merged mapping, not the per-post analysis.json files.
    indexed_mapping = {
        filename: {"index": i, **entry}
        for i, (filename, entry) in enumerate(all_mapping.items(), start=1)
    }
    kept_post_ids = sorted({entry["post_id"] for entry in all_mapping.values()})

    (job_dir / "image_analysis_mapping.json").write_text(
        json.dumps(indexed_mapping, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (job_dir / "manifest.json").write_text(
        json.dumps({
            "phase": "pipeline",
            "brand": args.brand, "brand_slug": brand_slug, "categories": categories,
            "processed_post_ids": all_processed_ids, "post_count": len(all_processed_ids),
            "kept_post_ids": kept_post_ids, "kept_count": len(kept_post_ids),
        }, indent=2),
        encoding="utf-8",
    )

    print(f"\n✅ Processing done. Processed {len(all_processed_ids)} post(s) across {len(sources)} source(s) — "
          f"{len(kept_post_ids)} kept after brand/image/category filtering.")

    # ── Phase 3: publish prep + publish — dedupe → scraper analysis → AI
    # images per selected category, then each category publishes to its OWN
    # website + WordPress page (a category without a page ID is skipped).
    if not kept_post_ids:
        print("\nℹ️  No posts kept — skipping publish prep and publishing.")
        print("[STAGE] done")
        return

    print("\n" + "=" * 70)
    print(f"PHASE 3/3 — Publish prep + publishing ({', '.join(categories)})")
    print("=" * 70)

    for category in categories:
        print(f"\n── {category} ──")
        print("[STAGE] deduplicating")
        dedupe_category_images(job_dir, brand_slug, category)
        print("[STAGE] scraping_products")
        enrich_images_with_scrapes(job_dir, args.brand, brand_slug, category)
        print("[STAGE] capping_products")
        cap_category_products(job_dir, brand_slug, category)
        print("[STAGE] generating_ai_images")
        generate_ai_images(job_dir, args.brand, brand_slug, category, image_prompt=args.image_prompt)

    # Publish plan: each selected category with a configured page publishes
    # separately — food to its site/page, non-food to its own.
    publish_plan = []
    if "food" in categories and args.food_page_id:
        publish_plan.append(("food", args.food_page_id, args.food_publish_target))
    if "non_food" in categories and args.non_food_page_id:
        publish_plan.append(("non_food", args.non_food_page_id, args.non_food_publish_target))

    published_any = False
    for category, page_id, target in publish_plan:
        images_dir = job_dir / brand_slug / category / "images"
        image_count = sum(1 for p in images_dir.iterdir() if p.is_file()) if images_dir.exists() else 0
        if not image_count:
            print(f"\nℹ️  No {category} images kept — skipping {category} publish.")
            continue
        print("\n" + "=" * 70)
        print(f"PUBLISHING {category} images to {target} (page {page_id})")
        print("=" * 70)
        print("[STAGE] publishing")
        ok, message = publish_brand(
            parent_job_id=args.job_id,
            brand=args.brand,
            brand_slug=brand_slug,
            category=category,
            page_id=page_id,
            publish_target=target,
            output_root=args.output_root,
            status="publish",
            page_title=args.page_title,
            week_start_day=args.week_start,
        )
        if not ok:
            print(f"❌ Publish failed: {message}")
            sys.exit(1)
        published_any = True

    if not publish_plan:
        print("\nℹ️  No WordPress page ID configured for the selected categories — skipping publish step.")

    print("[STAGE] done")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Per-brand sub-workflow — PHASE 2 of the two-phase pipeline. One instance
runs per brand, launched automatically by job_runner.py after the discovery
job (run_facebook.py --phase discover) completes.

Reads the brand's manifest (output/<parent_job_id>/<brand-slug>/posts.json),
written by the discovery phase — each entry carries the post's text, its
already-discovered image URLs, and `last_media_id` — then for every post:

  1. Extra-image discovery: pages in any images beyond the ones already in
     the post's GraphQL node via `last_media_id` (fb_client.download_pending_post_images
     for page/group; the media-id album walk for bare post URLs) — this is
     the "discover more images if available" step, and it lives HERE in the
     sub-workflow, not in the discovery job
  2. Downloads the image bytes
  3. Processes each image (grayscale + compress <= 200KB)
  4. Analyzes each processed image via KIE GPT-5.6 Luna
  5. Keeps the post only if it has enough images, organizing it into
     output/<parent_job_id>/<brand-slug>/<category>/post_<id>/
  6. If the brand has a WordPress page ID configured: uploads the food
     images (titled with their KIE product names) and updates the page

Everything lands under the PARENT job's output dir, so the parent job's
ZIP export and per-brand folder layout stay exactly as before.
"""

import argparse
import difflib
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import brand_mapping
import fb_client
import generate
import kie_vision
import scrapers
from constants import (
    AI_IMAGE_MAX_BYTES, AI_IMAGE_WORKERS, DEDUPE_PRODUCTS, DEDUPE_THRESHOLD,
    FETCH_COMMENTS, GENERATE_AI_IMAGES, MAX_IMAGES_PER_POST, PRICE_ON_IMAGE,
    SCRAPER_ANALYSIS, SCRAPER_WORKERS,
)
from data_store import load_fb_auth
from image_pipeline import compress_under_limit
from price_overlay import overlay_price_on_image

from run_facebook import (
    finalize_post, organize_or_discard, post_already_output,
)
from publish_wordpress import publish_brand


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


def source_url_of(entry: dict) -> str:
    return entry.get("source_url") or (entry.get("post") or {}).get("permalink") or ""


def process_brand_post(job_dir: Path, staging_root: Path, scratch_root: Path,
                       entry: dict, cookies: dict) -> tuple[str | None, dict]:
    """Download + process + analyze + organize ONE post for this brand.
    Returns (post_id, mapping-if-kept)."""
    post = entry["post"]
    src_type = entry["src_type"]
    post_id = post["post_id"]
    text_key = entry.get("text_key", "text")
    name_key = entry.get("name_key", "page_name")
    source_url = entry.get("source_url")

    if post_already_output(job_dir, post_id):
        print(f"  ⏭️  Already processed: {post_id}")
        return post_id, {}

    # Brand was already filtered during discovery — re-derive the canonical
    # brand string here for organize_or_discard().
    text_brand = brand_mapping.detect_single_brand(post.get(text_key))
    print(f"  🏷️  Post {post_id} brand: {text_brand or '(none / ambiguous)'} — text: {(post.get(text_key) or '')[:150]!r}")
    if not text_brand:
        print(f"  🗑️  Skipping post {post_id} — no single retailer brand detected in post text")
        return post_id, {}

    comments = []
    if FETCH_COMMENTS:
        print("[STAGE] fetching_comments")
        try:
            comments, _ = fb_client.fetch_comments_for_post(post_id, cookies=cookies)
        except Exception as e:
            print(f"  ⚠️  Comment fetch failed for {post_id}: {e}")
            comments = []

    print("[STAGE] downloading_images")
    image_paths: list[str] = []
    if src_type == "post":
        # Bare post URL: walk the image album via the media_id captured
        # during discovery (this IS the extra-image discovery for post URLs).
        if post.get("media_id"):
            orig_dir = staging_root / f"post_{post_id}" / "images" / "original"
            try:
                image_paths = fb_client.fetch_single_post_images(
                    post["media_id"], post_id, str(orig_dir), cookies,
                    max_images=MAX_IMAGES_PER_POST,
                )
            except Exception as e:
                print(f"  ⚠️  Image fetch failed for {post_id}: {e}")
        else:
            print(f"  ⚠️  No media_id for {post_id} — cannot fetch images")
    else:
        # Page/group: collect every image URL (discovery-time photos plus
        # anything paged in via last_media_id — the "more images if
        # available" walk), then download them concurrently.
        try:
            image_paths = fb_client.download_pending_post_images(
                src_type, post, str(scratch_root), max_images=MAX_IMAGES_PER_POST
            )
        except Exception as e:
            print(f"  ⚠️  Image download failed for {post_id}: {e}")

    mapping = finalize_post(
        staging_root, post_id,
        post_url=post.get("permalink") or source_url_of(entry), page_url=entry.get("source_url"),
        page_name=post.get(entry.get("name_key", "page_name")),
        post_text=post.get(entry.get("text_key", "text")),
        published_at=post.get("published_at"),
        comment_count=post.get("comment_count", len(comments)),
        comments=comments, orig_image_paths=image_paths,
    )
    kept = organize_or_discard(job_dir, staging_root, post_id, text_brand, mapping)
    return post_id, (mapping if kept else {})


def dedupe_food_images(job_dir: Path, brand_slug: str) -> int:
    """Remove duplicate products from food/image_analysis.json — different
    posts/pages often show the same product, so before scraper analysis and
    publishing, every entry's product name is normalized and compared against
    the names already accepted (difflib ratio >= constants.DEDUPE_THRESHOLD);
    duplicates are dropped from the JSON and their flat image files deleted,
    so scrapers and the WordPress publisher never see them. First occurrence
    wins (JSON insertion order). Toggled by constants.DEDUPE_PRODUCTS.
    Returns the number of duplicates removed. Never raises."""
    if not DEDUPE_PRODUCTS:
        return 0
    analysis_file = job_dir / brand_slug / "food" / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    images_dir = job_dir / brand_slug / "food" / "images"
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

    if not duplicates:
        print("✅ No duplicate products found.")
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


def enrich_food_images_with_scrapes(job_dir: Path, brand: str, brand_slug: str) -> int:
    """Scraper analysis: for every FOOD image whose KIE analysis found a
    product name, look that product up on the brand's own site (scrapers/)
    and attach {name, price, description, product_url} to its analysis entry
    in food/image_analysis.json — publish_brand() renders those on the
    WordPress page. Toggled by constants.SCRAPER_ANALYSIS. Returns the
    number of images enriched. Never raises — a failed lookup just leaves
    that image without scraped data."""
    if not SCRAPER_ANALYSIS:
        return 0
    searcher = scrapers.get_searcher(brand)
    if not searcher:
        print(f"ℹ️  No product scraper for {brand} — skipping scraper analysis.")
        return 0

    analysis_file = job_dir / brand_slug / "food" / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    to_scrape = {k: v for k, v in analysis.items() if v.get("product_name")}
    if not to_scrape:
        print("ℹ️  No food images with KIE product names — nothing to scrape.")
        return 0

    print("\n" + "=" * 70)
    print(f"SCRAPER ANALYSIS — looking up {len(to_scrape)} product(s) on {brand}")
    print("=" * 70)

    enriched = 0
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
        try:
            result = thread_searcher.search(entry.get("product_name"))
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
                print(f"  🔗 {filename} → {result.get('name')!r} — {result.get('price') or 'price not listed'}")
            else:
                print(f"  🚫 {filename} — no product match for {entry.get('product_name')!r}")

    try:
        analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  Could not write enriched analysis back to {analysis_file}: {e}")
    print(f"✅ Scraper analysis: enriched {enriched}/{len(to_scrape)} food image(s).")
    return enriched


def apply_price_badges(job_dir: Path, brand_slug: str) -> int:
    """Stamp scraped prices onto the food images (used when AI generation is
    off — the badge goes on the original). Marks entries price_overlaid so
    images never get double-badged on re-runs. Returns the number badged."""
    if not PRICE_ON_IMAGE:
        return 0
    analysis_file = job_dir / brand_slug / "food" / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    images_dir = job_dir / brand_slug / "food" / "images"
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
    """Prepare a workspace per-brand image prompt for sending: strip comment
    lines (lines starting with '#'), then substitute {store} and
    {product_name} from the KIE analysis + scraped data. Unknown placeholders
    are left untouched."""
    product_name = entry.get("product_name") or (entry.get("scraped") or {}).get("name") or ""
    body = "\n".join(
        line for line in template.splitlines()
        if not line.lstrip().startswith("#")
    )
    body = body.replace("{store}", brand).replace("{product_name}", product_name)
    return body.strip()


def generate_ai_images(job_dir: Path, brand: str, brand_slug: str, image_prompt: str | None = None) -> int:
    """AI image generation stage — the last step before publishing. For every
    food image, create a new AI-generated version via KIE (generate.py,
    nano-banana-pro) and use it for publishing instead of the original: the
    upload copy and the AI result are both kept under
    constants.AI_IMAGE_MAX_BYTES (quality-first compression,
    image_pipeline.compress_under_limit — no unnecessary quality loss). The
    price badge is applied AFTER generation — onto the AI image when one was
    produced, otherwise onto the original. Prompt: the workspace's per-brand
    image prompt (--image-prompt) — brands without one skip generation.
    Toggled by constants.GENERATE_AI_IMAGES. Returns the number of images
    regenerated. Never raises per image — a failure just publishes the
    original."""
    if not GENERATE_AI_IMAGES:
        apply_price_badges(job_dir, brand_slug)
        return 0

    analysis_file = job_dir / brand_slug / "food" / "image_analysis.json"
    if not analysis_file.exists():
        return 0
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Could not read {analysis_file}: {e}")
        return 0

    images_dir = job_dir / brand_slug / "food" / "images"
    entries = {fn: e for fn, e in analysis.items() if (images_dir / fn).exists()}
    if not entries:
        return 0

    prompt_template = (image_prompt or "").strip()
    if not prompt_template:
        print("⚠️  No AI image prompt configured for this brand (workspace image_prompt) — skipping AI generation.")
        apply_price_badges(job_dir, brand_slug)
        return 0
    scratch_dir = job_dir / f"_ai_{brand_slug}"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 70)
    print(f"AI IMAGE GENERATION — regenerating {len(entries)} food image(s) via KIE nano-banana-pro")
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
                upload_path = scratch_dir / f"upload_{filename}"
                shutil.copyfile(img_path, upload_path)
                compress_under_limit(str(upload_path), AI_IMAGE_MAX_BYTES)
                public_url = generate.upload_image(str(upload_path))
                kie_vision.rate_limiter.acquire()
                prompt = render_image_prompt(prompt_template, brand, entry)
                task_id = generate.create_task(public_url, prompt=prompt)
                result_url = generate.poll_task(task_id)
                result_path = scratch_dir / f"result_{filename}"
                generate.download_image(result_url, str(result_path))
                compress_under_limit(str(result_path), AI_IMAGE_MAX_BYTES)
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
    print(f"✅ AI image generation: {generated}/{len(entries)} image(s) regenerated.")
    return generated


def main():
    parser = argparse.ArgumentParser(description="Per-brand sub-workflow: images + analysis + publish")
    parser.add_argument("--job-id", required=True, help="This brand job's own id (log context)")
    parser.add_argument("--parent-job-id", required=True, help="Discovery job id whose output/ holds posts.json")
    parser.add_argument("--brand", required=True)
    parser.add_argument("--brand-slug", required=True)
    parser.add_argument("--page-id", default=None, help="WordPress page ID — publishing is skipped when absent")
    parser.add_argument("--publish-target", default="retailshout", choices=["retailshout", "aos"])
    parser.add_argument("--output-root", default="output")
    parser.add_argument("--image-prompt", default=None,
                        help="Per-brand AI image prompt (workspace config) — brands without one skip AI generation")
    parser.add_argument("--page-title", default=None,
                        help="Per-brand WordPress page title template ({brand}/{date_range} placeholders, workspace config)")
    parser.add_argument("--week-start", default=None,
                        help="Week start day for the page title's date window (e.g. friday for ALDI, tuesday for Publix)")
    args = parser.parse_args()

    posts_file = Path(args.output_root) / args.parent_job_id / args.brand_slug / "posts.json"
    if not posts_file.exists():
        print(f"❌ No posts manifest at {posts_file} — nothing to do.")
        sys.exit(1)
    bundle = json.loads(posts_file.read_text(encoding="utf-8"))
    posts = bundle.get("posts") or []

    print("=" * 70)
    print(f"Brand sub-workflow — {args.brand}  (job {args.job_id[:8]}..., parent {args.parent_job_id[:8]}...)")
    print(f"Posts to process: {len(posts)}  |  Publish target: {args.publish_target}"
          f"{f' | WP page {args.page_id}' if args.page_id else ' | publishing disabled (no page ID)'}")
    print("=" * 70)

    fb_auth = load_fb_auth()
    cookies, _proxies = fb_client.apply_auth(fb_auth.get("cookie_string", ""), fb_auth.get("fb_dtsg", ""))
    if not cookies:
        print("⚠️  No Facebook session cookie configured (Settings page) — public-only scraping, may be unreliable.")

    job_dir = Path(args.output_root) / args.parent_job_id
    staging_root = job_dir / f"_staging_{args.brand_slug}"
    scratch_root = job_dir / f"_scratch_{args.brand_slug}"

    processed_ids: list[str] = []
    all_mapping: dict = {}

    for idx, entry in enumerate(posts, start=1):
        post_id = entry.get("post_id")
        print(f"\n[{idx}/{len(posts)}] Post {post_id} ({entry.get('src_type')}) — {entry.get('source_url')}")
        try:
            pid, mapping = process_brand_post(job_dir, staging_root, scratch_root, entry, cookies)
            if pid:
                processed_ids.append(pid)
                all_mapping.update(mapping)
        except Exception as e:
            print(f"❌ Post failed entirely: {post_id}: {e}")

    shutil.rmtree(staging_root, ignore_errors=True)
    shutil.rmtree(scratch_root, ignore_errors=True)

    kept_post_ids = sorted({m["post_id"] for m in all_mapping.values()})
    (job_dir / args.brand_slug / "brand_result.json").write_text(
        json.dumps({
            "brand": args.brand, "brand_slug": args.brand_slug,
            "posts_in_manifest": len(posts),
            "processed_post_ids": processed_ids,
            "kept_post_ids": kept_post_ids,
            "image_count": len(all_mapping),
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n✅ {args.brand}: processed {len(processed_ids)} post(s), kept "
          f"{len(kept_post_ids)} with images.")

    # ── Publish: upload this brand's food images to WordPress and update
    # its page (skipped when the brand has no page ID configured).
    if args.page_id:
        print("\n" + "=" * 70)
        print("PUBLISHING food images to WordPress")
        print("=" * 70)
        dedupe_food_images(job_dir, args.brand_slug)
        enrich_food_images_with_scrapes(job_dir, args.brand, args.brand_slug)
        generate_ai_images(job_dir, args.brand, args.brand_slug, image_prompt=args.image_prompt)
        ok, message = publish_brand(
            parent_job_id=args.parent_job_id,
            brand=args.brand,
            brand_slug=args.brand_slug,
            page_id=args.page_id,
            publish_target=args.publish_target,
            output_root=args.output_root,
            status="publish",
            page_title=args.page_title,
            week_start_day=args.week_start,
        )
        if not ok:
            print(f"❌ Publish failed: {message}")
            sys.exit(1)
    else:
        print("\nℹ️  No WordPress page ID configured for this brand — skipping publish step.")

    print("[STAGE] done")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Facebook product-image analysis pipeline — the Facebook equivalent of the
old run_full.py. Invoked as a subprocess by job_runner.py.

For every configured source (page / group / post URL):
  1. Identify + resolve the source
  2. Fetch posts in the [start_date, end_date] window (page/group) or the
     single post (post URL), applying the min-comments threshold
  3. Detect a single canonical retailer brand from the post's text
     (brand_mapping.py) — zero or multiple detected brands skips the post
     before any image processing/analysis cost is spent
  4. Fetch comments + images for each remaining post
  5. Process each image (grayscale + compress <= 200KB)
  6. Analyze each processed image via KIE GPT-5.6 Luna
  7. Keep the post only if it has more than one image AND every image's
     detected brand matches the text brand; otherwise discard it
  8. Save a kept post's JSON + image->analysis mapping under
     output/<job_id>/<brand-slug>/<category>/post_<id>/, and merge it into
     the job-wide image->analysis mapping

One failing post/image/API call never aborts the whole job — failures are
logged and recorded as analysis_status: "failed" so no data is silently lost.
"""

import argparse
import json
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests

import brand_mapping
import fb_client
import image_pipeline
import kie_vision
from data_store import load_fb_auth
from constants import (
    FETCH_COMMENTS, ANALYZE_IMAGES, MAX_IMAGES_PER_POST, IMAGE_WORKERS,
    POSTS_PER_SOURCE_DEFAULT, MIN_IMAGES_FOR_KEEP,
)


def parse_date_arg(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d


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
    return meta


def _process_one_image(orig_path_str: str, index: int, processed_dir: Path, post_id: str) -> tuple[str, dict, dict]:
    """Process + analyze a single image. Never raises — a failure is recorded
    as analysis_status: 'failed' rather than losing the image entirely."""
    orig_path = Path(orig_path_str)
    image_id = f"image_{index:03d}"
    mapping_filename = f"post_{post_id}_{orig_path.name}"
    analysis = {"brand": None, "product_name": None, "category": None, "analysis_status": "failed"}
    processed_filename = None

    print("[STAGE] processing_images")
    try:
        processed_path = image_pipeline.process_image(
            str(orig_path), str(processed_dir / f"{orig_path.stem}_processed.jpg")
        )
        processed_filename = Path(processed_path).name

        if not ANALYZE_IMAGES:
            analysis = {"brand": None, "product_name": None, "category": None, "analysis_status": "skipped"}
        else:
            print("[STAGE] analyzing_products")
            try:
                public_url = kie_vision.upload_image(processed_path)
                result = kie_vision.analyze_product_image(public_url)
                analysis = {**result, "analysis_status": "success"}
            except Exception as e:
                print(f"  ⚠️  KIE analysis failed for {orig_path.name}: {e}")
    except Exception as e:
        print(f"  ⚠️  Image processing failed for {orig_path.name}: {e}")

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
    }
    return mapping_filename, image_entry, mapping_entry


def build_analysis_and_images(post_dir: Path, original_paths: list[str], post_id: str):
    """Process + analyze every image for a post concurrently. A failure on
    one image is recorded as analysis_status: 'failed' and never aborts the
    others. Output order is preserved regardless of completion order."""
    processed_dir = post_dir / "images" / "processed"
    sorted_paths = sorted(original_paths)

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
    location, not the final output path. organize_or_discard() below moves
    it into output/<job_id>/<brand>/<category>/ (or deletes it) once the
    brand/image-count filter has been applied."""
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


def category_from_mapping(mapping: dict) -> str:
    """One category per post: unanimous non-null image category wins,
    otherwise uncategorized (mixed or undetermined)."""
    categories = {entry.get("category") for entry in mapping.values() if entry.get("category")}
    if len(categories) == 1:
        return next(iter(categories))
    return "uncategorized"


def organize_or_discard(job_dir: Path, staging_root: Path, post_id: str, text_brand: str, mapping: dict) -> bool:
    """Applies the image-requirement filter and either moves the staged post
    into output/<job_id>/<brand>/<category>/post/post_<id>/ or discards it.

    Also mirrors the kept post's images into a flat <brand>/<category>/images/
    folder and merges its mapping into a category-wide
    <brand>/<category>/image_analysis.json — so a user can see every image +
    its analysis for a whole brand/category at a glance, without opening each
    post_<id>/ folder one by one. Every individual post_<id>/ folder is kept
    grouped together under a single <brand>/<category>/post/ folder, for
    drilling into one specific post while debugging.

    Returns True if the post was kept."""
    staged_dir = staging_root / f"post_{post_id}"
    keep, reason = post_qualifies(mapping)
    if not keep:
        print(f"  🗑️  Skipping post {post_id} — {reason}")
        shutil.rmtree(staged_dir, ignore_errors=True)
        return False

    category = category_from_mapping(mapping)
    brand_slug = brand_mapping.brand_slug(text_brand)
    category_dir = job_dir / brand_slug / category
    dest_dir = category_dir / "post" / f"post_{post_id}"
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged_dir), str(dest_dir))

    flat_images_dir = category_dir / "images"
    flat_images_dir.mkdir(exist_ok=True)
    orig_dir = dest_dir / "images" / "original"
    if orig_dir.exists():
        for img in orig_dir.iterdir():
            if img.is_file():
                try:
                    shutil.copy2(img, flat_images_dir / f"post_{post_id}_{img.name}")
                except Exception as e:
                    print(f"  ⚠️  Could not copy {img.name} into {brand_slug}/{category}/images/: {e}")

    category_analysis_file = category_dir / "image_analysis.json"
    combined = {}
    if category_analysis_file.exists():
        try:
            combined = json.loads(category_analysis_file.read_text(encoding="utf-8"))
        except Exception:
            combined = {}
    combined.update(mapping)
    category_analysis_file.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  📁 Kept post {post_id} → {brand_slug}/{category}/")
    return True


def discover_post_url(url: str, cookies: dict) -> str | None:
    """Discovery phase for a bare post URL: resolve its post ID only — no
    text/comments/images are fetched yet."""
    post_id = fb_client.resolve_post_id(url, cookies=cookies)
    if not post_id:
        print(f"  ❌ Could not resolve post ID from {url}")
    return post_id


def process_post_url(job_dir: Path, post_id: str, url: str, cookies: dict, skip_ids: set) -> tuple[str | None, dict]:
    if post_id in skip_ids:
        print(f"  ⏭️  Already processed in a previous run: {post_id}")
        return None, {}
    if post_already_output(job_dir, post_id):
        print(f"  ⏭️  Already processed in this job: {post_id}")
        return None, {}

    meta = fetch_post_meta_from_html(url, cookies)

    text_brand = brand_mapping.detect_single_brand(meta.get("text"))
    if not text_brand:
        print(f"  🗑️  Skipping post {post_id} — no single retailer brand detected in post text")
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
        # post URL — we call it but discard the comment text/replies.
        try:
            _, post_info = fb_client.fetch_comments_for_post(post_id, cookies=cookies)
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
    kept = organize_or_discard(job_dir, staging_root, post_id, text_brand, mapping)
    return post_id, (mapping if kept else {})


def discover_page_or_group_posts(job_dir: Path, url: str, src_type: str, cookies: dict,
                                  min_comments: int, start_dt, end_dt, posts_per_source: int) -> dict | None:
    """Discovery phase for a page/group source: resolves the source and
    fetches its post list (text, permalink, comment count, image URLs, etc.)
    with download_images=False — every post's images are discovered/counted
    but their bytes are NOT downloaded yet.

    A brand-keyword text_filter is also passed straight into the scraper's
    fetch_posts(): it runs the instant a post's text is available, BEFORE
    that post's images are even enumerated — so a post with no single
    detected retailer never triggers extract_media() at all (see CONTEXT.md
    Design Decision 19).

    fetch_extra_images=False on top of that: for a post with more than 5
    photos, only the 5 already present in its own GraphQL node are
    discovered here — the paginated lookup for the rest (one GraphQL request
    per extra photo, previously incurred even for a discovery-only pass) is
    deferred to process_page_or_group_posts() -> fb_client.download_pending_post_images(),
    via the `last_media_id` recorded on each post. Returns everything
    process_page_or_group_posts() below needs, or None if the source
    couldn't be resolved."""
    print("[STAGE] fetching_posts")
    scratch_root = job_dir / f"_scratch_{src_type}_{abs(hash(url)) % 100000}"
    text_filter = lambda text: brand_mapping.detect_single_brand(text) is not None

    if src_type == "group":
        source_id = fb_client.resolve_group_id(url, cookies=cookies)
        if not source_id:
            print(f"  ❌ Could not resolve group ID from {url}")
            return None
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
            print(f"  ❌ Could not resolve page ID from {url}")
            return None
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
                                 skip_ids: set) -> tuple[list[str], dict]:
    """Processing phase for one discovered page/group source: brand
    detection, comments, image download/processing/analysis, and
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
        # primary filter point.
        text_brand = brand_mapping.detect_single_brand(post.get(text_key))
        if not text_brand:
            print(f"  🗑️  Skipping post {post_id} — no single retailer brand detected in post text")
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
        kept = organize_or_discard(job_dir, staging_root, post_id, text_brand, mapping)
        if kept:
            all_mapping.update(mapping)
        processed_ids.append(post_id)

    shutil.rmtree(scratch_root, ignore_errors=True)
    return processed_ids, all_mapping


def main():
    parser = argparse.ArgumentParser(description="Facebook product-image analysis pipeline")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--sources-file", required=True, help="JSON file: [{\"url\":..., \"type\": \"page|group|post|null\"}]")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD (page/group sources only)")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD (page/group sources only, default: today)")
    parser.add_argument("--min-comments", type=int, default=0)
    parser.add_argument("--skip-post-ids-file", default=None, help="JSON list of post_ids to skip (cross-run dedup)")
    parser.add_argument("--posts-per-source", type=int, default=POSTS_PER_SOURCE_DEFAULT)
    parser.add_argument("--output-root", default="output")
    args = parser.parse_args()

    sources = json.loads(Path(args.sources_file).read_text(encoding="utf-8"))

    skip_ids = set()
    if args.skip_post_ids_file and Path(args.skip_post_ids_file).exists():
        skip_ids = set(json.loads(Path(args.skip_post_ids_file).read_text(encoding="utf-8")))

    start_dt = parse_date_arg(args.start_date, end_of_day=False)
    end_dt = parse_date_arg(args.end_date, end_of_day=True) or datetime.now(timezone.utc)

    fb_auth = load_fb_auth()
    cookies, _proxies = fb_client.apply_auth(fb_auth.get("cookie_string", ""), fb_auth.get("fb_dtsg", ""))
    if not cookies:
        print("⚠️  No Facebook session cookie configured (Settings page) — public-only scraping, may be unreliable.")

    job_dir = Path(args.output_root) / args.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"Facebook pipeline — job {args.job_id}")
    print(f"Sources: {len(sources)}  |  Date window: {start_dt or '(open)'} → {end_dt}  |  Min comments: {args.min_comments}")
    print("=" * 70)

    # ── Phase 1: discovery — scrape every post link from every source first,
    # before any per-post brand detection / image processing / KIE analysis
    # starts. This surfaces "how much is there to process" up front instead
    # of interleaving it with the (much slower) processing phase.
    print("\n" + "=" * 70)
    print("PHASE 1/2 — Discovering posts from all sources")
    print("=" * 70)

    discovered_post_sources: list[tuple[str, str]] = []       # (url, post_id)
    discovered_page_group_sources: list[dict] = []            # discover_page_or_group_posts() results
    total_found = 0

    for idx, src in enumerate(sources, start=1):
        url = src.get("url", "").strip()
        if not url:
            continue
        src_type = src.get("type")
        if src_type not in ("post", "page", "group"):
            print(f"⚠️  Source has no valid type set ({src_type!r}) — defaulting to 'page'. "
                  f"Check workspaces.json if this wasn't submitted through the UI.")
            src_type = "page"
        print(f"\n[{idx}/{len(sources)}] Discovering: {url}  (type={src_type})")

        try:
            if src_type == "post":
                post_id = discover_post_url(url, cookies)
                if post_id:
                    discovered_post_sources.append((url, post_id))
                    print(f"  🔗 Found 1 post")
                    total_found += 1
            else:
                discovered = discover_page_or_group_posts(
                    job_dir, url, src_type, cookies, args.min_comments,
                    start_dt, end_dt, args.posts_per_source,
                )
                if discovered is not None:
                    discovered_page_group_sources.append(discovered)
                    print(f"  🔗 Found {len(discovered['posts'])} post(s)")
                    total_found += len(discovered["posts"])
        except Exception as e:
            print(f"❌ Discovery failed entirely for source: {url}: {e}")
            continue

    print(f"\n✅ Discovery complete — found {total_found} post(s) across "
          f"{len(discovered_post_sources) + len(discovered_page_group_sources)}/{len(sources)} source(s).")

    # ── Phase 2: processing — brand detection, image download/analysis,
    # and filter/organize for every post found above.
    print("\n" + "=" * 70)
    print("PHASE 2/2 — Processing discovered posts")
    print("=" * 70)

    all_processed_ids: list[str] = []
    all_mapping: dict = {}

    for url, post_id in discovered_post_sources:
        print(f"\nProcessing post: {url}")
        try:
            processed_id, mapping = process_post_url(job_dir, post_id, url, cookies, skip_ids)
            if processed_id:
                all_processed_ids.append(processed_id)
                all_mapping.update(mapping)
        except Exception as e:
            print(f"❌ Source failed entirely: {url}: {e}")

    for discovered in discovered_page_group_sources:
        print(f"\nProcessing source: {discovered['url']}  ({len(discovered['posts'])} post(s))")
        try:
            ids, mapping = process_page_or_group_posts(job_dir, discovered, cookies, skip_ids)
            all_processed_ids.extend(ids)
            all_mapping.update(mapping)
        except Exception as e:
            print(f"❌ Source failed entirely: {discovered['url']}: {e}")

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
            "processed_post_ids": all_processed_ids, "post_count": len(all_processed_ids),
            "kept_post_ids": kept_post_ids, "kept_count": len(kept_post_ids),
        }, indent=2),
        encoding="utf-8",
    )

    print(f"\n✅ Done. Processed {len(all_processed_ids)} post(s) across {len(sources)} source(s) — "
          f"{len(kept_post_ids)} kept after brand/image filtering.")
    print("[STAGE] done")


if __name__ == "__main__":
    main()

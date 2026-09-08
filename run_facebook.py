#!/usr/bin/env python3
"""
Facebook product-image analysis pipeline — the Facebook equivalent of the
old run_full.py. Invoked as a subprocess by job_runner.py.

For every configured source (page / group / post URL):
  1. Identify + resolve the source
  2. Fetch posts in the [start_date, end_date] window (page/group) or the
     single post (post URL), applying the min-comments threshold
  3. Fetch comments + images for each selected post
  4. Process each image (grayscale + compress <= 200KB)
  5. Analyze each processed image via KIE GPT-5.6 Luna
  6. Save per-post JSON + a job-wide image->analysis mapping

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

import fb_client
import image_pipeline
import kie_vision
from data_store import load_fb_auth

# ── Temporary testing toggles ──
FETCH_COMMENTS = False       # skip comment scraping — not needed right now
MAX_IMAGES_PER_POST = 2      # cap images processed/analyzed per post — for testing


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


IMAGE_WORKERS = 5  # concurrent image processing/analysis workers per post — kie_vision.rate_limiter
                    # still caps actual KIE calls process-wide, so this just controls local parallelism


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


def finalize_post(job_dir: Path, post_id: str, post_url, page_url, page_name, post_text,
                   published_at, comment_count, comments, orig_image_paths) -> dict:
    post_dir = job_dir / f"post_{post_id}"
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


def handle_post_url(job_dir: Path, url: str, cookies: dict, skip_ids: set) -> tuple[str | None, dict]:
    post_id = fb_client.resolve_post_id(url, cookies=cookies)
    if not post_id:
        print(f"  ❌ Could not resolve post ID from {url}")
        return None, {}
    if post_id in skip_ids:
        print(f"  ⏭️  Already processed in a previous run: {post_id}")
        return None, {}
    if (job_dir / f"post_{post_id}" / "post.json").exists():
        print(f"  ⏭️  Already processed in this job: {post_id}")
        return None, {}

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

    meta = fetch_post_meta_from_html(url, cookies)

    image_paths = []
    if post_info and post_info.get("media_id"):
        print("[STAGE] downloading_images")
        orig_dir = job_dir / f"post_{post_id}" / "images" / "original"
        try:
            image_paths = fb_client.fetch_single_post_images(
                post_info["media_id"], post_id, str(orig_dir), cookies, max_images=MAX_IMAGES_PER_POST
            )
        except Exception as e:
            print(f"  ⚠️  Image fetch failed for {post_id}: {e}")

    mapping = finalize_post(
        job_dir, post_id, post_url=url, page_url=url,
        page_name=meta.get("page_name"), post_text=meta.get("text"),
        published_at=meta.get("published_at"), comment_count=len(comments),
        comments=comments, orig_image_paths=image_paths,
    )
    return post_id, mapping


def handle_page_or_group(job_dir: Path, url: str, src_type: str, cookies: dict,
                          min_comments: int, start_dt, end_dt, posts_per_source: int,
                          skip_ids: set) -> tuple[list[str], dict]:
    scratch_root = job_dir / f"_scratch_{src_type}_{abs(hash(url)) % 100000}"

    print("[STAGE] fetching_posts")
    if src_type == "group":
        source_id = fb_client.resolve_group_id(url, cookies=cookies)
        if not source_id:
            print(f"  ❌ Could not resolve group ID from {url}")
            return [], {}
        posts = fb_client.fetch_group_posts(
            source_id, limit=posts_per_source, min_comments=min_comments,
            start_date=start_dt, end_date=end_dt, save_root=str(scratch_root),
            max_images_per_post=MAX_IMAGES_PER_POST,
        )
        text_key, name_key = "message", "group_name"
    else:
        source_id = fb_client.resolve_page_id(url, cookies=cookies)
        if not source_id:
            print(f"  ❌ Could not resolve page ID from {url}")
            return [], {}
        posts = fb_client.fetch_page_posts(
            source_id, limit=posts_per_source, min_comments=min_comments,
            start_date=start_dt, end_date=end_dt, save_root=str(scratch_root),
            max_images_per_post=MAX_IMAGES_PER_POST,
        )
        text_key, name_key = "text", "page_name"

    processed_ids = []
    all_mapping = {}

    for post in posts:
        post_id = post.get("post_id")
        if not post_id or post_id in skip_ids:
            continue
        if (job_dir / f"post_{post_id}" / "post.json").exists():
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
        orig_images = []
        matches = list(scratch_root.glob(f"*/{post_id}")) if scratch_root.exists() else []
        if matches:
            for f in matches[0].iterdir():
                if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                    orig_images.append(str(f))

        mapping = finalize_post(
            job_dir, post_id,
            post_url=post.get("permalink") or url, page_url=url,
            page_name=post.get(name_key), post_text=post.get(text_key),
            published_at=post.get("published_at"),
            comment_count=post.get("comment_count", len(comments)),
            comments=comments, orig_image_paths=orig_images,
        )
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
    parser.add_argument("--posts-per-source", type=int, default=500)
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

    all_processed_ids: list[str] = []
    all_mapping: dict = {}

    for idx, src in enumerate(sources, start=1):
        url = src.get("url", "").strip()
        if not url:
            continue
        src_type = src.get("type")
        if src_type not in ("post", "page", "group"):
            print(f"⚠️  Source has no valid type set ({src_type!r}) — defaulting to 'page'. "
                  f"Check workspaces.json if this wasn't submitted through the UI.")
            src_type = "page"
        print(f"\n[{idx}/{len(sources)}] Source: {url}  (type={src_type})")

        try:
            if src_type == "post":
                post_id, mapping = handle_post_url(job_dir, url, cookies, skip_ids)
                if post_id:
                    all_processed_ids.append(post_id)
                    all_mapping.update(mapping)
            else:
                ids, mapping = handle_page_or_group(
                    job_dir, url, src_type, cookies, args.min_comments,
                    start_dt, end_dt, args.posts_per_source, skip_ids,
                )
                all_processed_ids.extend(ids)
                all_mapping.update(mapping)
        except Exception as e:
            print(f"❌ Source failed entirely: {url}: {e}")
            continue

    (job_dir / "image_analysis_mapping.json").write_text(
        json.dumps(all_mapping, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (job_dir / "manifest.json").write_text(
        json.dumps({"processed_post_ids": all_processed_ids, "post_count": len(all_processed_ids)}, indent=2),
        encoding="utf-8",
    )

    print(f"\n✅ Done. Processed {len(all_processed_ids)} post(s) across {len(sources)} source(s).")
    print("[STAGE] done")


if __name__ == "__main__":
    main()

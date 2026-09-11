#!/usr/bin/env python3
"""
Thin wrapper around the `facebook/` scraper package (an independently
maintained scraping library, vendored as a subfolder) so the rest of this
app can call it without depending on its removed PyQt6 GUI.

Responsibilities:
- ID resolution from URL (source type is always explicit — set by the
  Post/Page/Group URL fields in the UI — not guessed from URL shape)
- Pushing session auth (cookies + fb_dtsg) and proxy selection into the
  scraper modules' module-level globals (the same mechanism the old GUI used)
- Thin fetch wrappers used by run_facebook.py
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
FACEBOOK_PKG_DIR = BASE_DIR / "facebook"

if str(FACEBOOK_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(FACEBOOK_PKG_DIR))

import post_scraper                    # noqa: E402
import group_post_scraper_v2            # noqa: E402
import comment_scraper                  # noqa: E402
import single_post_image                # noqa: E402
from proxy_utils import select_proxy    # noqa: E402
from main import (                      # noqa: E402
    extract_user_id_from_url,
    extract_group_id_from_url,
    extract_post_id_from_url,
    fetch_comments_for_post as _fetch_comments_for_post,
)
from constants import IMAGE_DOWNLOAD_WORKERS  # noqa: E402


def parse_cookies(cookie_string: str) -> dict:
    """Parse a 'key1=value1; key2=value2' cookie string into a dict."""
    cookies = {}
    if not cookie_string:
        return cookies
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def apply_auth(cookie_string: str, fb_dtsg: str):
    """Push cookies/fb_dtsg/proxy selection into every scraper module.

    Mirrors what the old facebook_ui.py ScraperThread did before each run.
    """
    cookies = parse_cookies(cookie_string) if cookie_string else {}
    dtsg = fb_dtsg or ""
    is_static = bool(cookies)

    proxies = select_proxy(is_static)

    for mod in (post_scraper, group_post_scraper_v2, comment_scraper, single_post_image):
        mod.PROXIES = proxies
        mod.IS_STATIC_PROXY = is_static

    post_scraper.COOKIES = cookies
    post_scraper.FB_DTSG = dtsg
    group_post_scraper_v2.COOKIES = cookies
    group_post_scraper_v2.FB_DTSG = dtsg
    comment_scraper.FB_DTSG = dtsg
    single_post_image.FB_DTSG = dtsg

    return cookies, proxies


def resolve_page_id(url: str, cookies: dict | None = None) -> str | None:
    return extract_user_id_from_url(url, cookies=cookies)


def resolve_group_id(url: str, cookies: dict | None = None) -> str | None:
    return extract_group_id_from_url(url, cookies=cookies)


def resolve_post_id(url: str, cookies: dict | None = None) -> str | None:
    return extract_post_id_from_url(url, cookies=cookies)


def fetch_page_posts(page_id: str, limit: int, min_comments: int = 0,
                      start_date=None, end_date=None, save_root="page_post",
                      batch_size=10, on_batch_complete=None, max_images_per_post=None,
                      download_images=True, text_filter=None, fetch_extra_images=True):
    post_scraper.USER_ID = page_id
    post_scraper.BASE_HEADERS["referer"] = f"https://www.facebook.com/profile.php?id={page_id}"
    return post_scraper.fetch_posts(
        limit=limit, min_comments=min_comments, batch_size=batch_size,
        on_batch_complete=on_batch_complete, start_date=start_date, end_date=end_date,
        save_root=save_root, max_images_per_post=max_images_per_post,
        download_images=download_images, text_filter=text_filter,
        fetch_extra_images=fetch_extra_images,
    )


def fetch_group_posts(group_id: str, limit: int, min_comments: int = 0,
                       start_date=None, end_date=None, save_root="group_post",
                       batch_size=10, on_batch_complete=None, max_images_per_post=None,
                       download_images=True, text_filter=None, fetch_extra_images=True):
    group_post_scraper_v2.GROUP_ID = group_id
    group_post_scraper_v2.HEADERS["referer"] = f"https://www.facebook.com/groups/{group_id}/"
    return group_post_scraper_v2.fetch_posts(
        limit=limit, min_comments=min_comments, batch_size=batch_size,
        on_batch_complete=on_batch_complete, start_date=start_date, end_date=end_date,
        save_root=save_root, max_images_per_post=max_images_per_post,
        download_images=download_images, text_filter=text_filter,
        fetch_extra_images=fetch_extra_images,
    )


def _dedupe_photos(photos: list[dict]) -> list[dict]:
    """Drop duplicate photo entries by media id (falling back to url when no
    id is present) — first occurrence wins, order otherwise preserved (a
    later duplicate is simply omitted, everything after it shifts up to fill
    the gap). Needed here because the discovery-phase photo list and the
    deferred `fetch_remaining_images()` pagination result are combined below
    — and that pagination starts AT `last_media_id`, which is itself already
    the last photo discovery collected, so its own node can come back as the
    first "remaining" result."""
    seen = set()
    deduped = []
    for p in photos:
        key = p.get("id") or p.get("url")
        if key is None or key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def sanitize_folder_name(name: str | None) -> str:
    """Mirrors the inline page/group-name sanitization in
    post_scraper.py / group_post_scraper_v2.py so callers outside the
    scraper (e.g. run_facebook.py) can compute the same media save-dir."""
    if name:
        cleaned = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip()
        if cleaned:
            return cleaned
    return "Unknown"


def download_pending_post_images(src_type: str, post: dict, save_root: str,
                                  max_images: int | None = None) -> list[str]:
    """Downloads the image bytes for one post whose images were only
    discovered — not downloaded — during a download_images=False
    fetch_page_posts()/fetch_group_posts() call. Called once a post has
    passed the brand-text filter, so non-matching posts never trigger an
    image download at all.

    Two-step, not interleaved: first every image URL for this post is
    collected (discovery-time photos already have theirs; anything past
    that is paged in via `post["last_media_id"]`, the same media-ID walk the
    bare-post-URL flow uses) — this part is inherently sequential, since
    each next node only comes from the previous response. Only once every
    URL is known does downloading start, via a small thread pool
    (`constants.IMAGE_DOWNLOAD_WORKERS`) so a multi-image post's downloads
    run concurrently instead of one full request-then-save round trip at a
    time. `fetch_remaining_images(download_images=False)` is self-terminating
    (stops the moment Facebook stops returning a next node), so a post with
    no more images beyond what discovery found costs at most one extra
    "nothing more" request rather than assuming a fixed initial photo count.

    The combined list is deduped (_dedupe_photos()) before downloading —
    fetch_remaining_images() starts its walk AT last_media_id, which is
    itself already the last photo discovery collected, so its own node can
    come back as the first "remaining" result. Order is preserved; a
    duplicate is simply omitted, not swapped for another entry."""
    post_id = post.get("post_id")
    name_folder = sanitize_folder_name(post.get("group_name") if src_type == "group" else post.get("page_name"))
    save_dir = os.path.join(save_root, name_folder)
    last_media_id = post.get("last_media_id")

    if src_type == "group":
        photo_entries = list(post.get("photos") or [])
        module = group_post_scraper_v2
    else:
        photo_entries = [m for m in (post.get("media") or []) if m.get("type") == "photo"]
        module = post_scraper

    if last_media_id and (max_images is None or len(photo_entries) < max_images):
        remaining = module.fetch_remaining_images(
            last_media_id, post_id, len(photo_entries), save_dir, max_images=max_images, download_images=False,
        )
        photo_entries.extend(remaining)

    photo_entries = _dedupe_photos(photo_entries)

    if max_images is not None:
        photo_entries = photo_entries[:max_images]

    saved: list[str | None] = [None] * len(photo_entries)
    to_fetch = []
    for i, entry in enumerate(photo_entries):
        if entry.get("saved_as"):
            saved[i] = os.path.join(save_dir, str(post_id), entry["saved_as"])
        elif entry.get("url"):
            to_fetch.append((i, entry["url"]))

    if to_fetch:
        with ThreadPoolExecutor(max_workers=min(IMAGE_DOWNLOAD_WORKERS, len(to_fetch))) as executor:
            futures = {
                executor.submit(module.download_image, url, post_id, i + 1, save_dir): i
                for i, url in to_fetch
            }
            for future in as_completed(futures):
                i = futures[future]
                filename = future.result()
                if filename:
                    saved[i] = os.path.join(save_dir, str(post_id), filename)

    return [p for p in saved if p]


def fetch_comments_for_post(post_id: str, cookies: dict | None = None,
                            max_pages: int | None = None, with_replies: bool = True):
    """Returns (comments, post_info) — post_info has 'media_id' for single-post image fetching.

    For media_id-only callers: max_pages=1, with_replies=False → one request."""
    return _fetch_comments_for_post(post_id, cookies=cookies, max_pages=max_pages,
                                    with_replies=with_replies)


def fetch_single_post_images(media_id: str, post_id: str, out_dir: str, cookies: dict | None = None,
                              max_images: int | None = None) -> list[str]:
    """Download images in a post's album via media-ID iteration.

    Stops early once `max_images` have been downloaded (None = no limit).
    Returns a list of saved absolute file paths.

    Two-step like download_pending_post_images(): the album walk itself is
    sequential (each next media node only comes from the previous GraphQL
    response, retried 3x on transient failure), but no bytes are downloaded
    during the walk — every discovered URL is collected first, then all
    downloads run concurrently via a small thread pool
    (constants.IMAGE_DOWNLOAD_WORKERS), each with its own pre-assigned image
    index so filenames stay post_id.jpg / post_id_2.jpg / etc regardless of
    completion order."""
    os.makedirs(out_dir, exist_ok=True)
    image_urls: list[str] = []
    current_node = media_id
    visited = set()

    while current_node and current_node not in visited:
        if max_images is not None and len(image_urls) >= max_images:
            break
        visited.add(current_node)
        payload = single_post_image.build_payload(current_node, post_id, cookies)

        r = None
        for attempt in range(1, 4):  # 3 attempts before giving up on this node
            try:
                resp = requests.post(
                    single_post_image.GRAPHQL_URL,
                    headers=single_post_image.HEADERS,
                    data=payload,
                    cookies=cookies,
                    proxies=single_post_image.PROXIES,
                    timeout=30,
                )
                if resp.status_code == 200:
                    r = resp
                    break
                print(f"  ⚠️ Attempt {attempt}/3: Status {resp.status_code} fetching media node")
            except Exception as e:
                print(f"  ⚠️ Attempt {attempt}/3: {e} fetching media node")
            if attempt < 3:
                time.sleep(attempt * 2)
        if r is None:
            break

        cleaned_blocks = single_post_image.process_raw_graphql(r.text)
        if not cleaned_blocks:
            break

        for block in cleaned_blocks:
            if "currMedia" in block:
                image_url = block["currMedia"].get("image", {}).get("uri")
                if image_url:
                    image_urls.append(image_url)
                break

        next_node = None
        for block in cleaned_blocks:
            if "nextMediaAfterNodeId" in block and block["nextMediaAfterNodeId"]:
                node_id_next = block["nextMediaAfterNodeId"].get("id")
                if node_id_next:
                    next_node = node_id_next
                    break

        current_node = next_node

    saved: list[str | None] = [None] * len(image_urls)
    if image_urls:
        with ThreadPoolExecutor(max_workers=min(IMAGE_DOWNLOAD_WORKERS, len(image_urls))) as executor:
            futures = {
                executor.submit(single_post_image.download_image, url, out_dir, post_id, i + 1): i
                for i, url in enumerate(image_urls)
            }
            for future in as_completed(futures):
                i = futures[future]
                filename = future.result()
                if filename:
                    saved[i] = os.path.join(out_dir, filename)

    return [p for p in saved if p]

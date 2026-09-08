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
from pathlib import Path

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

    proxies = select_proxy(bool(cookies))

    for mod in (post_scraper, group_post_scraper_v2, comment_scraper, single_post_image):
        mod.PROXIES = proxies

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
                      batch_size=10, on_batch_complete=None, max_images_per_post=None):
    post_scraper.USER_ID = page_id
    post_scraper.BASE_HEADERS["referer"] = f"https://www.facebook.com/profile.php?id={page_id}"
    return post_scraper.fetch_posts(
        limit=limit, min_comments=min_comments, batch_size=batch_size,
        on_batch_complete=on_batch_complete, start_date=start_date, end_date=end_date,
        save_root=save_root, max_images_per_post=max_images_per_post,
    )


def fetch_group_posts(group_id: str, limit: int, min_comments: int = 0,
                       start_date=None, end_date=None, save_root="group_post",
                       batch_size=10, on_batch_complete=None, max_images_per_post=None):
    group_post_scraper_v2.GROUP_ID = group_id
    group_post_scraper_v2.HEADERS["referer"] = f"https://www.facebook.com/groups/{group_id}/"
    return group_post_scraper_v2.fetch_posts(
        limit=limit, min_comments=min_comments, batch_size=batch_size,
        on_batch_complete=on_batch_complete, start_date=start_date, end_date=end_date,
        save_root=save_root, max_images_per_post=max_images_per_post,
    )


def fetch_comments_for_post(post_id: str, cookies: dict | None = None):
    """Returns (comments, post_info) — post_info has 'media_id' for single-post image fetching."""
    return _fetch_comments_for_post(post_id, cookies=cookies)


def fetch_single_post_images(media_id: str, post_id: str, out_dir: str, cookies: dict | None = None,
                              max_images: int | None = None) -> list[str]:
    """Download images in a post's album via media-ID iteration.

    Stops early once `max_images` have been downloaded (None = no limit).
    Returns a list of saved absolute file paths.
    """
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    current_node = media_id
    visited = set()
    image_count = 0

    while current_node and current_node not in visited:
        if max_images is not None and image_count >= max_images:
            break
        visited.add(current_node)
        payload = single_post_image.build_payload(current_node, post_id, cookies)

        import requests
        r = requests.post(
            single_post_image.GRAPHQL_URL,
            headers=single_post_image.HEADERS,
            data=payload,
            cookies=cookies,
            proxies=single_post_image.PROXIES,
            timeout=30,
        )
        cleaned_blocks = single_post_image.process_raw_graphql(r.text)
        if not cleaned_blocks:
            break

        image_url = None
        for block in cleaned_blocks:
            if "currMedia" in block:
                image_url = block["currMedia"].get("image", {}).get("uri")
                break

        if image_url:
            image_count += 1
            filename = single_post_image.download_image(image_url, out_dir, post_id, image_count)
            if filename:
                saved.append(os.path.join(out_dir, filename))

        next_node = None
        for block in cleaned_blocks:
            if "nextMediaAfterNodeId" in block and block["nextMediaAfterNodeId"]:
                node_id_next = block["nextMediaAfterNodeId"].get("id")
                if node_id_next:
                    next_node = node_id_next
                    break

        current_node = next_node

    return saved

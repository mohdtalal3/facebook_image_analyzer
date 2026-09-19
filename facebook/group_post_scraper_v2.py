import requests
import json
import time
import os
import uuid
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"

# ========= CONFIG (FILL THESE) =========
GROUP_ID = "363757814515154"  # group id
GROUP_NAME = None  # Will be extracted automatically
DOC_ID = "25716860671307636"  # GroupsCometFeedRegularStoriesPaginationQuery

HEADERS = {
    "user-agent": "Mozilla/5.0",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://www.facebook.com",
    "referer": f"https://www.facebook.com/groups/{GROUP_ID}/",
}

# Get proxy configuration
PROXY = os.getenv('PROXY')
PROXIES = {'http': PROXY, 'https': PROXY} if PROXY else None

# Cookies (set by UI when provided)
COOKIES = {}

# FB_DTSG token (set by UI when provided)
FB_DTSG = ""

# Whether PROXIES is currently a STATIC_PROXY (cookie session) or a
# ROTATING_PROXY (no cookies) — set by fb_client.apply_auth(). Determines how
# retry_request() reacts to a proxy failure (see proxy_utils.rotate_or_retry()).
IS_STATIC_PROXY = False

if PROXY:
    print("Using proxy (fallback PROXY env var)")


def extract_group_name(node):
    """Extract group name from post node"""
    try:
        # Try from context_layout > story > comet_sections > title > story > to
        context_layout = node.get('comet_sections', {}).get('context_layout', {})
        story = context_layout.get('story', {})
        title_section = story.get('comet_sections', {}).get('title', {})
        title_story = title_section.get('story', {})
        to_obj = title_story.get('to', {})
        if to_obj.get('__typename') == 'Group':
            return to_obj.get('name')
        
        # Try from content > story > target_group (if available)
        content = node.get('comet_sections', {}).get('content', {})
        content_story = content.get('story', {})
        target_group = content_story.get('target_group', {})
        if target_group and 'name' in target_group:
            return target_group.get('name')
        
        # Try from feedback > associated_group
        feedback = node.get('feedback', {})
        associated_group = feedback.get('associated_group', {})
        if associated_group and 'name' in associated_group:
            return associated_group.get('name')
        
        return None
    except Exception:
        return None

# ========= RETRY HELPER =========
def retry_request(url, headers, data, proxies, max_retries=5):
    """Make a POST request with retry logic"""
    global PROXIES
    from proxy_utils import rotate_or_retry, is_proxy_infra_error, is_ip_blocked

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers=headers, data=data, proxies=proxies, cookies=COOKIES, timeout=30)
            if r.status_code == 200:
                return r
            if is_proxy_infra_error(status_code=r.status_code):
                new_p = rotate_or_retry(IS_STATIC_PROXY, f"  🚫 Attempt {attempt}/{max_retries}: Proxy auth failed (HTTP {r.status_code})")
                if new_p:
                    proxies = new_p
                    PROXIES = new_p
            elif is_ip_blocked(status_code=r.status_code, response_text=r.text):
                new_p = rotate_or_retry(IS_STATIC_PROXY, f"  🛽 Attempt {attempt}/{max_retries}: Facebook blocked this IP (HTTP {r.status_code})")
                if new_p:
                    proxies = new_p
                    PROXIES = new_p
            else:
                print(f"  ⚠️ Attempt {attempt}/{max_retries}: Status {r.status_code}")
        except requests.exceptions.ProxyError as e:
            new_p = rotate_or_retry(IS_STATIC_PROXY, f"  🚫 Attempt {attempt}/{max_retries}: Proxy unreachable")
            if new_p:
                proxies = new_p
                PROXIES = new_p
        except Exception as e:
            if is_proxy_infra_error(exc=e):
                new_p = rotate_or_retry(IS_STATIC_PROXY, f"  🚫 Attempt {attempt}/{max_retries}: Proxy connection error")
                if new_p:
                    proxies = new_p
                    PROXIES = new_p
            else:
                print(f"  ⚠️ Attempt {attempt}/{max_retries}: {str(e)}")

        if attempt < max_retries:
            wait_time = attempt * 2
            print(f"  ⏳ Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    raise Exception(f"Failed after {max_retries} attempts")


def download_image(url, post_id, image_index=1, save_dir="group_post", max_retries=3):
    """Download image from URL and save as {post_id}.jpg or {post_id}_2.jpg etc.
    Retries up to max_retries times with backoff before giving up (returns None)."""
    if not url or not post_id:
        return None

    # Create post-specific directory
    post_dir = os.path.join(save_dir, str(post_id))
    os.makedirs(post_dir, exist_ok=True)

    # Get file extension from URL or default to .jpg
    ext = ".jpg"
    if ".png" in url.lower():
        ext = ".png"
    elif ".jpeg" in url.lower():
        ext = ".jpeg"

    # Name as {post_id}.jpg or {post_id}_2.jpg etc
    filename = f"{post_id}{ext}" if image_index == 1 else f"{post_id}_{image_index}{ext}"
    filepath = os.path.join(post_dir, filename)

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            # Save the image
            with open(filepath, 'wb') as f:
                f.write(response.content)

            print(f"  📥 Downloaded image: {filename}")
            return filename
        except Exception as e:
            print(f"  ⚠️ Download attempt {attempt}/{max_retries} failed for {filename}: {e}")
            if attempt < max_retries:
                wait_time = attempt * 2
                print(f"  ⏳ Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

    print(f"  ❌ Failed to download image after {max_retries} attempts: {filename}")
    return None


def fetch_remaining_images(last_media_id, post_id, current_image_count, save_dir="group_post", max_images=None,
                            download_images=True):
    """Fetch remaining images using media ID iteration (for posts with 5+ images).
    Stops as soon as `max_images` total (current_image_count + fetched) is reached.
    When download_images is False, image URLs are still discovered/counted
    (each still needs one GraphQL call to find the next node) but the actual
    image bytes are not downloaded — 'saved_as' is left None."""
    if not last_media_id or not post_id:
        return []
    if max_images is not None and current_image_count >= max_images:
        return []

    print(f"  🔄 Fetching remaining images after image #{current_image_count}...")

    DOC_ID_PHOTO = "26168653472729001"  # CometPhotoRootContentQuery
    HEADERS_PHOTO = {
        "user-agent": "Mozilla/5.0",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.facebook.com",
        "x-fb-friendly-name": "CometPhotoRootContentQuery"
    }

    remaining_photos = []
    current_node = last_media_id
    visited = set()
    image_index = current_image_count + 1
    hard_cap = min(100, max_images) if max_images is not None else 100

    while current_node and current_node not in visited and image_index <= hard_cap:  # Max 100 images safety limit
        visited.add(current_node)
        
        variables = {
            "isMediaset": True,
            "renderLocation": "comet_media_viewer",
            "nodeID": current_node,
            "mediasetToken": f"pcb.{post_id}",
            "scale": 2,
            "feedLocation": "COMET_MEDIA_VIEWER",
            "feedbackSource": 65,
            "focusCommentID": None,
            "privacySelectorRenderLocation": "COMET_MEDIA_VIEWER",
            "useDefaultActor": False,
            "shouldShowComments": True
        }
        
        payload = {
            "av": COOKIES.get("c_user", "0"),
            "__user": COOKIES.get("c_user", "0"),
            "__a": "1",
            "fb_dtsg": FB_DTSG if FB_DTSG else "",
            "doc_id": DOC_ID_PHOTO,
            "variables": json.dumps(variables)
        }
        
        try:
            r = None
            for attempt in range(1, 4):  # 3 attempts before giving up on this node
                try:
                    resp = requests.post(GRAPHQL_URL, headers=HEADERS_PHOTO, data=payload, proxies=PROXIES, cookies=COOKIES, timeout=30)
                    if resp.status_code == 200:
                        r = resp
                        break
                    print(f"  ⚠️ Attempt {attempt}/3: Status {resp.status_code} fetching next image")
                except Exception as e:
                    print(f"  ⚠️ Attempt {attempt}/3: {e} fetching next image")
                if attempt < 3:
                    time.sleep(attempt * 2)
            if r is None:
                break

            # Parse response
            cleaned_blocks = parse_fb_response(r.text)
            if not cleaned_blocks:
                break
            
            # Extract current image URL
            image_url = None
            for block in cleaned_blocks:
                if "currMedia" in block:
                    image_url = block["currMedia"].get("image", {}).get("uri")
                    break
            
            if image_url:
                saved_filename = download_image(image_url, post_id, image_index, save_dir) if download_images else None
                remaining_photos.append({
                    'id': current_node,
                    'url': image_url,
                    'saved_as': saved_filename
                })
                image_index += 1
            
            # Extract next node
            next_node = None
            for block in cleaned_blocks:
                if "nextMediaAfterNodeId" in block and block["nextMediaAfterNodeId"]:
                    node_id = block["nextMediaAfterNodeId"].get("id")
                    if node_id:
                        next_node = node_id
                        break
            
            if next_node:
                current_node = next_node
                time.sleep(0.5)  # Small delay between requests
            else:
                break  # No more images
                
        except Exception as e:
            print(f"  ⚠️ Error fetching next image: {e}")
            break
    
    if remaining_photos:
        print(f"  ✅ Fetched {len(remaining_photos)} additional images")
    
    return remaining_photos


def extract_data_blocks(raw_text):
    """Extract all 'data' blocks from raw text"""
    blocks = []
    i = 0
    n = len(raw_text)

    while True:
        idx = raw_text.find('"data"', i)
        if idx == -1:
            break

        brace_start = raw_text.find('{', idx)
        if brace_start == -1:
            break

        depth = 0
        for j in range(brace_start, n):
            if raw_text[j] == '{':
                depth += 1
            elif raw_text[j] == '}':
                depth -= 1
                if depth == 0:
                    block_text = raw_text[brace_start:j+1]
                    try:
                        block = json.loads(block_text)
                        blocks.append(block)
                    except Exception:
                        pass
                    i = j + 1
                    break
        else:
            break

    return blocks


def clean_data_blocks(blocks):
    """Clean unwanted keys from data blocks"""
    cleaned = []

    for block in blocks:
        if not isinstance(block, dict):
            continue

        block.pop("errors", None)
        block.pop("extensions", None)

        cleaned.append(block)

    return cleaned


def parse_fb_response(text):
    """Parse Facebook response using the same logic as post_scraper"""
    text = text.replace("for (;;);", "").strip()
    extracted = extract_data_blocks(text)
    cleaned = clean_data_blocks(extracted)
    return cleaned


def _find_first_key(node, key):
    """Recursively search a nested dict/list for the first occurrence of `key`."""
    if isinstance(node, dict):
        if key in node and node[key] is not None:
            return node[key]
        for v in node.values():
            found = _find_first_key(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_first_key(item, key)
            if found is not None:
                return found
    return None


def extract_creation_time(node):
    """Best-effort extraction of the post's creation time (unix seconds). See
    post_scraper.extract_creation_time for the same rationale."""
    value = _find_first_key(node, "creation_time")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return None


def extract_comment_count(node):
    """Extract comment count from post node"""
    try:
        # Path 1: feedback.comment_rendering_instance.comments.total_count
        comment_count = node.get("feedback", {}).get("comment_rendering_instance", {}).get("comments", {}).get("total_count")
        if comment_count is not None:
            return comment_count
        
        # Path 2: comet_sections.feedback.story.story_ufi_container.story.feedback_context.feedback_target_with_context.comment_rendering_instance.comments.total_count
        comet_sections = node.get("comet_sections", {})
        feedback_section = comet_sections.get("feedback", {})
        story = feedback_section.get("story", {})
        story_ufi_container = story.get("story_ufi_container", {})
        ufi_story = story_ufi_container.get("story", {})
        feedback_context = ufi_story.get("feedback_context", {})
        feedback_target = feedback_context.get("feedback_target_with_context", {})
        comment_count = feedback_target.get("comment_rendering_instance", {}).get("comments", {}).get("total_count")
        if comment_count is not None:
            return comment_count
        
        # Path 3: comet_sections.feedback.story.story_ufi_container.story.feedback_context.feedback_target_with_context.comet_ufi_summary_and_actions_renderer.feedback.comment_rendering_instance.comments.total_count
        comet_ufi = feedback_target.get("comet_ufi_summary_and_actions_renderer", {}).get("feedback", {})
        comment_count = comet_ufi.get("comment_rendering_instance", {}).get("comments", {}).get("total_count")
        if comment_count is not None:
            return comment_count
        
        # Path 4: comet_sections.feedback.story.feedback_context.feedback_target_with_context.comment_rendering_instance.comments.total_count (old structure)
        comet_sections = node.get("comet_sections", {})
        feedback_section = comet_sections.get("feedback", {})
        story = feedback_section.get("story", {})
        feedback_context = story.get("feedback_context", {})
        feedback_target = feedback_context.get("feedback_target_with_context", {})
        comment_count = feedback_target.get("comment_rendering_instance", {}).get("comments", {}).get("total_count")
        if comment_count is not None:
            return comment_count
        
        # Path 5: feedback.comments_count_summary_renderer.feedback.comment_rendering_instance.comments.total_count
        comments_renderer = node.get("feedback", {}).get("comments_count_summary_renderer", {}).get("feedback", {})
        comment_count = comments_renderer.get("comment_rendering_instance", {}).get("comments", {}).get("total_count")
        if comment_count is not None:
            return comment_count
        
        # Path 6: comet_sections.feedback.story.story_ufi_container.story.feedback_context.feedback_target_with_context.comet_ufi_summary_and_actions_renderer.feedback.comments_count_summary_renderer.feedback.comment_rendering_instance.comments.total_count
        comet_sections = node.get("comet_sections", {})
        feedback_section = comet_sections.get("feedback", {})
        story = feedback_section.get("story", {})
        story_ufi_container = story.get("story_ufi_container", {})
        ufi_story = story_ufi_container.get("story", {})
        feedback_context = ufi_story.get("feedback_context", {})
        feedback_target = feedback_context.get("feedback_target_with_context", {})
        comet_ufi = feedback_target.get("comet_ufi_summary_and_actions_renderer", {}).get("feedback", {})
        comments_count_renderer = comet_ufi.get("comments_count_summary_renderer", {}).get("feedback", {})
        comment_count = comments_count_renderer.get("comment_rendering_instance", {}).get("comments", {}).get("total_count")
        if comment_count is not None:
            return comment_count
            
        return 0
    except Exception:
        return 0


def is_reel_or_video_post(node):
    """Check if the post is a reel or video post"""
    if not node or node.get('__typename') != 'Story':
        return False
    
    # Check for reel in story type or anywhere in node
    node_typename = node.get('__typename', '')
    if 'reel' in node_typename.lower():
        return True
    
    # Check comet_sections for reel content
    comet_sections = node.get('comet_sections', {})
    content = comet_sections.get('content', {})
    
    content_typename = content.get('__typename', '')
    if 'reel' in content_typename.lower():
        return True
    
    # Check attachments for video/reel content
    attachments = node.get('attachments', [])
    for attachment in attachments:
        # Check for video media type
        if 'media' in attachment and attachment['media'].get('__typename') == 'Video':
            return True
        
        # Check for reel substring in media object
        if 'media' in attachment and 'reel' in str(attachment['media']).lower():
            return True
        
        # Check in styles > attachment > media for video or reel
        styles_media = attachment.get('styles', {}).get('attachment', {}).get('media', {})
        if styles_media.get('__typename') == 'Video':
            return True
        if 'reel' in str(styles_media).lower():
            return True
        
        # Check all_subattachments for videos or reels
        for subattachment in attachment.get('all_subattachments', {}).get('nodes', []):
            if 'media' in subattachment and subattachment['media'].get('__typename') == 'Video':
                return True
            if 'media' in subattachment and 'reel' in str(subattachment['media']).lower():
                return True
    
    return False


def _dedupe_photos(photos):
    """Drop duplicate photo entries by media id (falling back to url when no
    id is present) — first occurrence wins. Order is otherwise preserved: a
    later duplicate is simply omitted, everything after it shifts up to fill
    the gap, nothing is reordered. Needed because fetch_remaining_images()
    starts its pagination walk AT last_media_id, which is itself already one
    of the photos collected from the initial attachments — its own node can
    come back as the first 'remaining' result."""
    seen = set()
    deduped = []
    for p in photos:
        key = p.get("id") or p.get("url")
        if key is None or key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def extract_media(node, post_id, save_dir="group_post", max_images=None, download_images=True,
                   fetch_extra=True):
    """Extract photo and video URLs from a post.
    max_images caps how many photos are actually downloaded (None = no limit).
    Videos are never downloaded (unsupported), so they don't count against the cap.

    download_images=False discovers/counts each photo's URL without
    downloading the bytes — 'saved_as' is left None for every entry. Used
    for a cheap discovery pass where images should only be downloaded later,
    for posts that end up qualifying.

    fetch_extra=False skips fetch_remaining_images() entirely (the paginated
    lookup for posts with more than the 5 photos already present in the
    story node's attachments — each of those costs its own GraphQL request,
    even just to discover a URL). Only the up-to-5 "free" photos already in
    the node are returned; `last_media_id` (also returned) lets the caller
    fetch those extra images later, on demand, for posts that end up
    qualifying (see fb_client.download_pending_post_images())."""
    media = {
        'photos': [],
        'videos': []
    }

    # Track image index for this post
    image_index = 0
    last_media_id = None

    def _cap_reached():
        return max_images is not None and image_index >= max_images

    attachments = node.get('attachments', [])

    for attachment in attachments:
        if _cap_reached():
            break
        # Handle photo attachments
        if 'media' in attachment and attachment['media'].get('__typename') == 'Photo' and not _cap_reached():
            # Try to get photo from styles > attachment > media structure
            photo_data = attachment.get('styles', {}).get('attachment', {}).get('media', {})
            if 'photo_image' in photo_data:
                image_index += 1
                media_id = attachment['media'].get('id')
                last_media_id = media_id  # Track the last media ID
                image_url = photo_data['photo_image'].get('uri')
                saved_filename = download_image(image_url, post_id, image_index, save_dir) if download_images else None
                media['photos'].append({
                    'id': media_id,
                    'url': image_url,
                    'width': photo_data['photo_image'].get('width'),
                    'height': photo_data['photo_image'].get('height'),
                    'saved_as': saved_filename
                })

        # Handle albums (multiple photos)
        if 'all_subattachments' in attachment:
            for subattachment in attachment.get('all_subattachments', {}).get('nodes', []):
                if _cap_reached():
                    break
                if 'media' in subattachment and subattachment['media'].get('__typename') == 'Photo':
                    image_index += 1
                    photo_data = subattachment.get('media', {})
                    media_id = photo_data.get('id')
                    last_media_id = media_id  # Track the last media ID
                    if 'image' in photo_data:
                        image_url = photo_data['image'].get('uri')
                        saved_filename = download_image(image_url, post_id, image_index, save_dir) if download_images else None
                        media['photos'].append({
                            'id': media_id,
                            'url': image_url,
                            'width': photo_data['image'].get('width'),
                            'height': photo_data['image'].get('height'),
                            'saved_as': saved_filename
                        })

        # Handle video attachments
        if 'media' in attachment and attachment['media'].get('__typename') == 'Video':
            video_data = attachment.get('media', {})
            media['videos'].append({
                'id': video_data.get('id'),
                'url': video_data.get('playable_url'),
                'thumbnail': video_data.get('preferred_thumbnail', {}).get('image', {}).get('uri')
            })

    # Try for more images whenever we have a last_media_id to continue from — the
    # initial attachments don't reliably cap out at exactly 5 (it varies), so an
    # exact-count check here would silently miss images whenever the real
    # per-node/connection limit isn't 5. fetch_remaining_images() self-terminates
    # the moment Facebook stops returning a next node, so this costs at most one
    # extra "no more images" request rather than an assumption that can go wrong.
    # Skipped entirely once the cap is already reached, or when the caller wants to
    # defer this (fetch_extra=False) to a later, on-demand call for posts that end
    # up qualifying. fetch_remaining_images also stops on its own once max_images
    # is hit.
    if last_media_id and not _cap_reached() and fetch_extra:
        remaining_photos = fetch_remaining_images(last_media_id, post_id, image_index, save_dir, max_images=max_images,
                                                   download_images=download_images)
        media['photos'].extend(remaining_photos)

    media['photos'] = _dedupe_photos(media['photos'])
    return media, last_media_id


def post_already_exists(post_id, base_folder, name_folder):
    """Check if a post has already been scraped by checking if its JSON file exists"""
    if not post_id or not name_folder:
        return False
    
    post_file = os.path.join(base_folder, name_folder, str(post_id), f"{post_id}.json")
    return os.path.exists(post_file)


def extract_post_data(node, group_name=None, published_at=None, save_root="group_post", max_images=None,
                       download_images=True, text_filter=None, fetch_extra_images=True):
    """Extract relevant data from a post node.

    text_filter: Optional callable(message_text) -> bool. When it returns
    False, this returns None immediately — BEFORE extract_media() below is
    ever called — so a rejected post never triggers a single
    photo-URL-discovery request, let alone an image download.

    fetch_extra_images: passed straight through to extract_media() — see its
    docstring."""
    if not node or node.get('__typename') != 'Story':
        return None

    # Get the post content from the nested structure (`or {}` guards against
    # explicit JSON nulls anywhere in the chain)
    content_story = (((node.get('comet_sections') or {}).get('content') or {}).get('story')) or {}

    # Extract message/text
    message = ''
    message_obj = content_story.get('message', {})
    if message_obj:
        message = message_obj.get('text', '')

    if text_filter and not text_filter(message):
        return None

    post_id = node.get('post_id')
    if not post_id:
        return None

    # Extract comment count
    comment_count = extract_comment_count(node)

    # Extract group name if not provided
    if not group_name:
        group_name = extract_group_name(node)

    # Sanitize group name folder
    if group_name:
        name_folder = "".join(c for c in group_name if c.isalnum() or c in (' ', '-', '_')).strip()
        if not name_folder:
            name_folder = "Unknown"
    else:
        name_folder = "Unknown"

    # Prepare save directory for media
    media_save_dir = os.path.join(save_root, name_folder)

    # Extract media once — this downloads images, calling it twice would
    # download every image in the post twice for no reason.
    media, last_media_id = extract_media(
        node, post_id, media_save_dir, max_images=max_images, download_images=download_images,
        fetch_extra=fetch_extra_images,
    )

    post_data = {
        'id': node.get('id'),
        'post_id': post_id,
        'message': message,
        'comment_count': comment_count,
        'group_name': group_name,
        'permalink': node.get('permalink_url', ''),
        'published_at': published_at,
        'photos': media['photos'],
        'videos': media['videos'],
        'last_media_id': last_media_id,
    }

    # Save individual post to folder structure: {save_root}/{group_name}/{post_id}/{post_id}.json
    post_dir = os.path.join(save_root, name_folder, str(post_id))
    os.makedirs(post_dir, exist_ok=True)

    post_file = os.path.join(post_dir, f"{post_id}.json")
    with open(post_file, "w", encoding="utf-8") as f:
        json.dump(post_data, f, ensure_ascii=False, indent=2)
    print(f"✓ Saved to {post_file}")

    return post_data


def fetch_posts(limit=10, min_comments=0, batch_size=10, on_batch_complete=None,
                 start_date=None, end_date=None, save_root="group_post", max_pages=300,
                 max_images_per_post=None, download_images=True, text_filter=None,
                 fetch_extra_images=True, group_id=None):
    """Fetch posts from Facebook group

    Args:
        limit: Maximum number of posts to fetch
        min_comments: Minimum number of comments required for a post to be included (0 = no filter)
        batch_size: Number of posts to fetch before calling on_batch_complete callback
        on_batch_complete: Optional callback function(batch_posts, total_so_far, limit) called after each batch
        start_date: Optional timezone-aware datetime — posts older than this are excluded
        end_date: Optional timezone-aware datetime — posts newer than this are excluded
        save_root: Base directory posts/media are saved under (default "group_post")
        max_pages: Safety cap on GraphQL pagination requests
        max_images_per_post: Caps how many photos are actually downloaded per post (None = no limit)
        download_images: When False, every post's image URLs/counts are still
            discovered (post["photos"]) but no image bytes are downloaded —
            for a cheap discovery pass. Download them later for chosen posts
            via fb_client.download_pending_post_images().
        text_filter: Optional callable(message_text) -> bool, passed straight
            through to extract_post_data() — see its docstring. Posts it
            rejects are not counted against `limit` (only posts that pass
            count as "found").
        fetch_extra_images: passed straight through to extract_post_data() /
            extract_media() — see extract_media()'s docstring. When False,
            only the up-to-5 "free" photos per post are discovered; extra
            images (5+) are fetched later, on demand, via `last_media_id`.
    """
    global GROUP_NAME
    # Per-call id/name — concurrent callers (threaded page scanning) each get
    # their own, instead of racing on the module-level GROUP_ID/GROUP_NAME.
    group_id = group_id or GROUP_ID
    group_name = GROUP_NAME
    all_posts = []
    batch_posts = []
    cursor = None
    page_num = 1
    consecutive_too_old = 0

    if min_comments > 0:
        print(f"📊 Filtering posts with at least {min_comments} comments")

    if start_date or end_date:
        print(f"📅 Date window: {start_date.isoformat() if start_date else '(open)'} → {end_date.isoformat() if end_date else '(open)'}")

    if batch_size > 0 and batch_size < limit:
        print(f"📦 Processing in batches of {batch_size} posts")

    while len(all_posts) < limit and page_num <= max_pages:
        print(f"\nFetching page {page_num}...")
        
        variables = {
            "count": 3,
            "cursor": cursor,
            "feedLocation": "GROUP",
            "feedType": "DISCUSSION",
            "feedbackSource": 0,
            "filterTopicId": None,
            "focusCommentID": None,
            "privacySelectorRenderLocation": "COMET_STREAM",
            "renderLocation": "group",
            "scale": 2,
            #"sortingSetting": "TOP_POSTS",
            "stream_initial_count": 1,
            "useDefaultActor": False,
            "id": group_id,
        }
        
        payload = {
            "av": COOKIES.get("c_user", "0"),
            "__user": COOKIES.get("c_user", "0"),
            "__a": "1",
            "fb_dtsg": FB_DTSG if FB_DTSG else "",
            "doc_id": DOC_ID,
            "variables": json.dumps(variables),
        }
        
        # Retry loop for empty response handling
        max_empty_retries = 3
        empty_retry_count = 0
        data = []
        
        while empty_retry_count < max_empty_retries:
            try:
                headers = {**HEADERS, "referer": f"https://www.facebook.com/groups/{group_id}/"}
                r = retry_request(GRAPHQL_URL, headers, payload, PROXIES)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"Request failed: {e}")
                break
            
            # Parse the response
            data = parse_fb_response(r.text)
            
            if data and len(data) > 0:
                # Got valid data, break retry loop
                break
            else:
                empty_retry_count += 1
                if empty_retry_count < max_empty_retries:
                    print(f"  ⚠️ Empty response, retrying ({empty_retry_count}/{max_empty_retries})...")
                    time.sleep(2)  # Wait before retry
                else:
                    print(f"  ❌ Empty response after {max_empty_retries} attempts, skipping page")
        
        if not data or len(data) == 0:
            print("❌ No data received after retries, stopping pagination")
            break
        
        # Save raw response for debugging
        # with open(f"group_raw_page_{page_num}.json", "w", encoding="utf-8") as f:
        #     json.dump(data, f, ensure_ascii=False, indent=2)
        # print(f"Saved group_raw_page_{page_num}.json")
        
        # Extract posts from the response array
        posts_found = 0
        next_cursor = None
        
        for item in data:
            if not isinstance(item, dict):
                continue
            
            # `or {}` guards against explicit JSON nulls — a key that exists
            # with value null makes .get(key, {}) return None, and the .get
            # chain below would crash on it.
            node = item.get('node') or {}
            node_typename = node.get('__typename')
            
            # Collect Story nodes from multiple sources
            story_nodes = []
            
            # Direct Story node
            if node_typename == 'Story':
                story_nodes.append(node)
            
            # Story nodes inside Group edges
            elif node_typename == 'Group':
                edges = (node.get('group_feed') or {}).get('edges') or []
                for edge in edges:
                    edge_node = edge.get('node') or {}
                    if edge_node.get('__typename') == 'Story':
                        story_nodes.append(edge_node)
            
            # Process all found Story nodes
            for story_node in story_nodes:
                # Skip reels and video posts
                if is_reel_or_video_post(story_node):
                    print(f"  ⏭️  Skipping reel/video post")
                    continue

                # Date-window filtering (best-effort — creation_time isn't always present)
                creation_ts = extract_creation_time(story_node)
                published_at = None
                if creation_ts:
                    published_dt = datetime.fromtimestamp(creation_ts, tz=timezone.utc)
                    published_at = published_dt.isoformat()
                    if end_date and published_dt > end_date:
                        print(f"  ⏭️  Skipping post newer than end date ({published_at})")
                        continue
                    if start_date and published_dt < start_date:
                        consecutive_too_old += 1
                        print(f"  ⏭️  Skipping post older than start date ({published_at})")
                        continue
                    consecutive_too_old = 0
                elif start_date or end_date:
                    print(f"  ⚠️  Could not determine post date — date filter not applied to this post")

                # Check comment count threshold
                comment_count = extract_comment_count(story_node)
                if min_comments > 0 and comment_count < min_comments:
                    print(f"  ⏭️  Skipping post with only {comment_count} comments (need {min_comments}+)")
                    continue

                # Extract group name from first post if not set
                if not group_name:
                    group_name = extract_group_name(story_node)
                    if group_name:
                        print(f"📂 Group name: {group_name}")

                # Check if post already exists
                temp_post_id = story_node.get('post_id')
                temp_group_name = group_name or extract_group_name(story_node)
                if temp_group_name:
                    temp_name_folder = "".join(c for c in temp_group_name if c.isalnum() or c in (' ', '-', '_')).strip() or "Unknown"
                    if post_already_exists(temp_post_id, save_root, temp_name_folder):
                        print(f"  ⏭️  Skipping already scraped post: {temp_post_id}")
                        continue

                # Per-post failure isolation: one malformed node crashing
                # extract_post_data() must not kill the whole group scrape —
                # the post is skipped, pagination continues.
                try:
                    post_data = extract_post_data(
                        story_node, group_name, published_at=published_at, save_root=save_root,
                        max_images=max_images_per_post, download_images=download_images,
                        text_filter=text_filter, fetch_extra_images=fetch_extra_images,
                    )
                except Exception as e:
                    print(f"  ⚠️ Failed to extract post {temp_post_id}, skipping: {e}")
                    continue
                if post_data:
                    batch_posts.append(post_data)
                    all_posts.append(post_data)
                    posts_found += 1
                    print(f"  - Found post: {post_data['post_id']}")
                elif text_filter:
                    print(f"  ⏭️  Skipping post {temp_post_id} — rejected by text_filter")
                    
                    # Check if we should process this batch
                    if batch_size > 0 and len(batch_posts) >= batch_size and on_batch_complete:
                        print(f"\n📦 Batch complete: {len(batch_posts)} posts. Total: {len(all_posts)}/{limit}")
                        on_batch_complete(batch_posts, len(all_posts), limit)
                        batch_posts = []  # Reset batch
                    
                    if len(all_posts) >= limit:
                        break
            
            # Break outer loop if limit reached
            if len(all_posts) >= limit:
                break
            
            # Look for pagination info
            if 'page_info' in item:
                page_info = item['page_info'] or {}
                if page_info.get('has_next_page'):
                    next_cursor = page_info.get('end_cursor')
        
        print(f"Found {posts_found} posts on this page")

        # Stop pagination once we've clearly walked past the start of the date window
        if start_date and consecutive_too_old >= 5:
            print(f"⏹  {consecutive_too_old} consecutive posts older than start date — stopping pagination")
            break

        # Check if we should continue
        if not next_cursor or len(all_posts) >= limit:
            print("No more pages or reached limit. Stopping.")
            break
        
        cursor = next_cursor
        page_num += 1
        time.sleep(2)  # Be nice to the server
    
    # Process any remaining posts in the final batch
    if batch_posts and on_batch_complete:
        print(f"\n📦 Final batch: {len(batch_posts)} posts. Total: {len(all_posts)}/{limit}")
        on_batch_complete(batch_posts, len(all_posts), limit)
    
    return all_posts


if __name__ == "__main__":
    count = int(input("How many posts to fetch? "))
    
    print(f"\nFetching {count} posts from group {GROUP_ID}...")
    posts = fetch_posts(count)
    
    # Save posts to file
    with open("group_posts.json", "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
    
    print(f"\n✓ Saved {len(posts)} posts to group_posts.json")
    
    # Print summary
    print("\nSummary:")
    for i, post in enumerate(posts, 1):
        photos = len(post['photos'])
        videos = len(post['videos'])
        print(f"{i}. Post ID: {post['post_id']}")
        if photos:
            print(f"   📷 {photos} photo(s)")
        if videos:
            print(f"   🎥 {videos} video(s)")
        if post['message']:
            preview = post['message'][:100] + '...' if len(post['message']) > 100 else post['message']
            print(f"   {preview}")

#!/usr/bin/env python3
"""
Image processing for the Facebook product-analysis pipeline.

Every image downloaded from a post gets a processed sibling: grayscale,
JPEG-compressed down to <= MAX_PROCESSED_BYTES. Originals are never modified.
"""

import os
from PIL import Image

MAX_PROCESSED_BYTES = 200 * 1024  # 200 KB
MIN_QUALITY = 20
MIN_SCALE = 0.25


def process_image(original_path: str, processed_path: str, max_bytes: int = MAX_PROCESSED_BYTES) -> str:
    """Convert an image to grayscale and compress it under `max_bytes`.

    Saves the result to `processed_path` (JPEG) and returns that path.
    Raises on unreadable/corrupt input — caller should treat that as a
    per-image failure, not a whole-post failure.
    """
    os.makedirs(os.path.dirname(processed_path), exist_ok=True)

    with Image.open(original_path) as im:
        gray = im.convert("L")

        quality = 85
        scale = 1.0
        working = gray

        while True:
            working.save(processed_path, "JPEG", quality=quality, optimize=True)
            size = os.path.getsize(processed_path)

            if size <= max_bytes:
                break
            if quality > MIN_QUALITY:
                quality -= 15
                continue
            if scale > MIN_SCALE:
                scale -= 0.15
                w, h = gray.size
                new_size = (max(50, int(w * scale)), max(50, int(h * scale)))
                working = gray.resize(new_size, Image.LANCZOS)
                continue
            # Already at minimum quality/scale — accept whatever we have.
            break

    return processed_path


def process_post_images(original_paths: list[str], processed_dir: str) -> dict[str, str | None]:
    """Process a batch of images. Returns {original_path: processed_path or None on failure}."""
    results = {}
    for original_path in original_paths:
        filename = os.path.splitext(os.path.basename(original_path))[0] + "_processed.jpg"
        processed_path = os.path.join(processed_dir, filename)
        try:
            process_image(original_path, processed_path)
            results[original_path] = processed_path
        except Exception as e:
            print(f"  ⚠️  Failed to process image {original_path}: {e}")
            results[original_path] = None
    return results

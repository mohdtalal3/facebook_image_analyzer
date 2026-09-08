#!/usr/bin/env python3
"""
KIE AI product-image extraction (GPT-5.6 Luna via the /codex/v1/responses endpoint).

Uploads a local (processed) image to get a public URL — reusing KIE's own
file-stream-upload endpoint, the same one this project already used for
image generation — then sends that URL to the Luna model for analysis.
"""

import json
import os
import re
import threading
import time
import requests
from dotenv import load_dotenv

load_dotenv()

KIE_API_KEY = os.getenv("KIE_API_KEY", "")

UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
RESPONSES_URL = "https://api.kie.ai/codex/v1/responses"

HEADERS_AUTH = {"Authorization": f"Bearer {KIE_API_KEY}"}

# ── Rate limiter ──
# KIE allows up to 20 new generation/analysis requests per 10s per account.
# Stay a bit under that so concurrent callers (run_facebook.py's thread pool)
# never trip a 429, regardless of how many threads are calling in at once.
MAX_PER_WINDOW = 18
WINDOW_SECONDS = 10


class RateLimiter:
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.lock = threading.Lock()
        self.timestamps: list[float] = []

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.timestamps = [t for t in self.timestamps if now - t < self.period]
                if len(self.timestamps) < self.max_calls:
                    self.timestamps.append(now)
                    return
            time.sleep(0.2)


rate_limiter = RateLimiter(MAX_PER_WINDOW, WINDOW_SECONDS)

PRODUCT_EXTRACTION_PROMPT = """Product Image Extraction Agent

Analyze the provided product image and extract the following information:

Brand name
Product name
Food or non-food classification

Carefully inspect the entire image, including packaging, logos, labels, visible text, and the product itself.

Rules
Identify the brand name separately from the product name.
Extract the most specific product name visible in the image, including flavor, variant, type, or version when clearly shown.
Classify the product as either:
food
non_food
Be accurate and do not guess or hallucinate information.
If the brand cannot be confidently identified, return null.
If the product name cannot be confidently identified, return null.
If the food/non-food classification cannot be confidently determined, return null.
Ignore prices, discounts, promotional offers, store names, and advertising slogans.
Normalize capitalization and formatting where appropriate.
Always identify only the primary product shown in the image.
Return JSON only. Do not include explanations, markdown, comments, or any additional text.

Food Classification

Use food for products intended for human consumption, including:

Food
Snacks
Beverages
Dairy
Meat
Produce
Bakery
Frozen food
Canned food
Cooking ingredients

Use non_food for products such as:

Cleaning products
Household products
Personal care
Cosmetics
Pet products
Paper products
Electronics
Clothing
Toys
Other non-edible products

Required JSON Format
{
  "brand": "string or null",
  "product_name": "string or null",
  "category": "food or non_food or null"
}

Example:

{
  "brand": "Coca-Cola",
  "product_name": "Coca-Cola Zero Sugar",
  "category": "food"
}

Accuracy is more important than completeness. If the image does not provide enough evidence, return null rather than guessing."""


class KieAnalysisError(Exception):
    pass


def upload_image(file_path: str) -> str:
    """Upload a local image and return its public URL."""
    with open(file_path, "rb") as f:
        response = requests.post(
            UPLOAD_URL,
            headers=HEADERS_AUTH,
            files={"file": (os.path.basename(file_path), f, "image/jpeg")},
            data={"uploadPath": "fb_product_images"},
            timeout=60,
        )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise KieAnalysisError(f"Upload failed: {data}")
    file_data = data["data"]
    url = file_data.get("fileUrl") or file_data.get("url") or file_data.get("downloadUrl")
    if not url:
        raise KieAnalysisError(f"Could not find URL in upload response: {file_data}")
    return url


def _extract_response_text(data: dict) -> str | None:
    """Best-effort extraction of the model's text reply — the exact response
    shape of this endpoint isn't publicly documented, so this tries the
    common 'responses API' shape first, then falls back to a recursive scan."""
    try:
        for item in data.get("output", []) or []:
            if item.get("type") == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") in ("output_text", "text") and c.get("text"):
                        return c["text"]
    except Exception:
        pass

    if data.get("output_text"):
        return data["output_text"]

    # Fallback: scan every string value for something that looks like our JSON
    def _scan(node):
        if isinstance(node, str):
            if '"brand"' in node or "'brand'" in node:
                return node
            return None
        if isinstance(node, dict):
            for v in node.values():
                found = _scan(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = _scan(v)
                if found:
                    return found
        return None

    return _scan(data)


def _parse_json_block(text: str) -> dict:
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0)
    return json.loads(text)


def analyze_product_image(image_url: str, timeout: int = 120) -> dict:
    """Call KIE GPT-5.6 Luna to extract brand/product/category from an image.

    Returns {"brand": ..., "product_name": ..., "category": ...}.
    Raises KieAnalysisError on any failure — caller is responsible for
    recording a failed-analysis placeholder instead of losing the post.
    """
    payload = {
        "model": "gpt-5-6-luna",
        "stream": False,  # default is true (SSE) — we want one JSON response back
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": PRODUCT_EXTRACTION_PROMPT},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        "reasoning": {"effort": "low"},
    }

    rate_limiter.acquire()
    response = requests.post(
        RESPONSES_URL,
        headers={**HEADERS_AUTH, "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    credits_consumed = data.get("credits_consumed")
    print(f"  🔢 Tokens — input: {usage.get('input_tokens')}, output: {usage.get('output_tokens')}, "
          f"total: {usage.get('total_tokens')} | credits consumed: {credits_consumed}")

    text = _extract_response_text(data)
    if not text:
        raise KieAnalysisError(f"Could not find text content in KIE response: {data}")

    parsed = _parse_json_block(text)

    return {
        "brand": parsed.get("brand"),
        "product_name": parsed.get("product_name"),
        "category": parsed.get("category"),
        "tokens_used": usage.get("total_tokens"),
        "credits_consumed": credits_consumed,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 kie_vision.py <path_to_image>")
        raise SystemExit(1)

    image_path = sys.argv[1]

    print(f"Uploading {image_path}...")
    public_url = upload_image(image_path)
    print(f"Public URL: {public_url}")

    print("Analyzing...")
    result = analyze_product_image(public_url)
    print(json.dumps(result, indent=2, ensure_ascii=False))

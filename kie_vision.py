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
from pathlib import Path

import requests
from dotenv import load_dotenv

from constants import KIE_MAX_REQUESTS_PER_WINDOW, KIE_RATE_WINDOW_SECONDS

load_dotenv()

KIE_API_KEY = os.getenv("KIE_API_KEY", "")
UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
RESPONSES_URL = "https://api.kie.ai/codex/v1/responses"

HEADERS_AUTH = {"Authorization": f"Bearer {KIE_API_KEY}"}

# ── Rate limiter ──
# KIE allows up to 20 new generation/analysis requests per 10s PER ACCOUNT.
# Brand jobs run as separate subprocesses in parallel, so the limiter must be
# shared across processes — an in-memory per-process limiter would let N
# concurrent brand jobs collectively exceed the account cap (and a finished
# job's unused capacity would be wasted). SharedFileRateLimiter keeps the
# recent request timestamps in a small JSON file under data/ guarded by an
# flock, so every process draws from the same account-wide window.


class RateLimiter:
    """Single-process rate limiter (kept as fallback when fcntl is
    unavailable). Thread-safe within one process."""

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


class SharedFileRateLimiter:
    """Cross-process rate limiter: all KIE-calling processes (the parallel
    brand jobs, the discovery job, the CLI) share one window of timestamps
    persisted in `state_path`, locked with flock for atomic read-modify-write.
    When a process exits, its unused capacity is simply not extended — the
    survivors automatically get the full account budget back. Prunes entries
    older than the window on every attempt, so a crashed process's stale
    timestamps disappear within one period."""

    def __init__(self, max_calls: int, period: float, state_path):
        self.max_calls = max_calls
        self.period = period
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def acquire(self):
        while True:
            if self._try_acquire():
                return
            time.sleep(0.2)

    def _try_acquire(self) -> bool:
        import fcntl
        now = time.time()
        fd = os.open(self.state_path, os.O_RDWR | os.O_CREAT, 0o644)
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                raw = f.read().strip()
                try:
                    timestamps = json.loads(raw) if raw else []
                except Exception:
                    timestamps = []
                timestamps = [t for t in timestamps if now - t < self.period]
                if len(timestamps) >= self.max_calls:
                    return False
                timestamps.append(now)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(timestamps))
                return True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


try:
    import fcntl  # noqa: F401 — POSIX only; falls back to in-process limiter on Windows
    rate_limiter = SharedFileRateLimiter(
        KIE_MAX_REQUESTS_PER_WINDOW, KIE_RATE_WINDOW_SECONDS,
        Path(__file__).resolve().parent / "data" / "kie_rate_window.json",
    )
except ImportError:
    rate_limiter = RateLimiter(KIE_MAX_REQUESTS_PER_WINDOW, KIE_RATE_WINDOW_SECONDS)

PRODUCT_EXTRACTION_PROMPT = """Analyze the provided product image and return ONLY valid JSON with these fields:

{
  "brand": "string or null",
  "product_name": "string or null",
  "category": "food or non_food or null",
  "subcategory": "string or null"
}

IMPORTANT — BRAND IDENTIFICATION:
Identify the actual brand FIRST, before identifying the product.

Carefully inspect the entire package for:
- Brand logo
- Brand name
- Manufacturer/brand markings
- Clearly visible branding text

The "brand" field must contain ONLY the actual brand/manufacturer name.

NEVER put the product type, flavor, variant, description, slogan, size, or generic product name in the brand field.

Examples:
- "OREO Chocolate Sandwich Cookies" → brand: "Oreo", product_name: "Chocolate Sandwich Cookies"
- "Coca-Cola Zero Sugar" → brand: "Coca-Cola", product_name: "Zero Sugar"
- "Lay's Classic Potato Chips" → brand: "Lay's", product_name: "Classic Potato Chips"

If the brand is not clearly visible or cannot be confidently separated from the product name:
"brand": null

DO NOT guess the brand based on familiarity, packaging style, colors, or product type.

IMPORTANT — PRODUCT IDENTIFICATION:
Identify the actual primary product separately from the brand.

Use the most specific product name clearly visible, including flavor, variant, type, or version when applicable.

Do NOT include the brand name in product_name.

Do NOT use the brand name as product_name unless the product itself is genuinely named that way.

Ignore:
- Prices
- Discounts
- Store names
- Slogans
- Promotional text
- Marketing claims
- Package size, unless it is part of the actual product name

If the product name cannot be confidently identified:
"product_name": null

BRAND/PRODUCT VALIDATION:
Before returning the answer, verify all of the following:

1. What is the actual brand?
2. What is the actual product?
3. Are they separate pieces of information?
4. Is there visible evidence supporting the brand?
5. Is there visible evidence supporting the product name?

Never force an answer.

For example, if the package shows:
"Brand X"
"Strawberry Yogurt"

Return:
{
  "brand": "Brand X",
  "product_name": "Strawberry Yogurt"
}

If only "Strawberry Yogurt" is visible and no reliable brand is shown, return:
{
  "brand": null,
  "product_name": "Strawberry Yogurt"
}

If you are uncertain whether a visible word is the brand or the product name, do NOT guess. Use null for the uncertain field.

PRIMARY PRODUCT:
Identify ONLY the main/primary product shown.

Ignore background products, accessories, decorative objects, ingredients shown in serving suggestions, and unrelated objects.

FOOD CLASSIFICATION:

"food" = a product intended for human consumption.

"non_food" = household products, cleaning products, personal care, cosmetics, pet products, paper products, electronics, clothing, toys, etc.

If the classification cannot be confidently determined:
"category": null

FOOD SUBCATEGORY:
If category is "food", choose EXACTLY ONE of:

- "Bakery & Deli"
- "Dairy & Eggs"
- "Meat & Seafood"
- "Frozen Foods & Breakfast"
- "Snacks & Pantry Staples"
- "Beverages & Energy"

Choose the subcategory based on the actual product, NOT the brand.

If category is "non_food":
"subcategory": null

If the food subcategory cannot be confidently determined:
"subcategory": null

ACCURACY RULES:
- Inspect the entire image carefully before answering.
- Read logos and visible text carefully.
- Prioritize actual package branding over assumptions.
- Do not infer a brand from the product category.
- Do not infer a brand from packaging colors or design.
- Do not confuse retailer/store names with product brands.
- Do not confuse slogans with brand names.
- Do not confuse product descriptions with brand names.
- Do not put the same information into both brand and product_name.
- Do not hallucinate missing information.
- Accuracy is more important than completeness.
- When uncertain, return null.
- Normalize capitalization and formatting.
- Return ONLY valid JSON.
- Do not include explanations, markdown, comments, or additional text."""


class KieAnalysisError(Exception):
    pass


def upload_image(file_path: str, upload_path: str = "fb_product_images", mime: str = "image/jpeg",
                 max_retries: int = 3) -> str:
    """Upload a local image to KIE's file-stream-upload endpoint and return
    its public URL. Single shared implementation for both the analysis path
    (this module) and the AI-generation path (generate.py delegates here).

    Paced through the shared account-wide rate limiter and retried up to
    max_retries times with backoff — a transient upload failure no longer
    fails the image outright."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            rate_limiter.acquire()
            with open(file_path, "rb") as f:
                response = requests.post(
                    UPLOAD_URL,
                    headers=HEADERS_AUTH,
                    files={"file": (os.path.basename(file_path), f, mime)},
                    data={"uploadPath": upload_path},
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
        except Exception as e:
            last_error = e
            print(f"  ⚠️ Upload attempt {attempt}/{max_retries} failed for "
                  f"{os.path.basename(file_path)}: {e}")
            if attempt < max_retries:
                time.sleep(2 * attempt)
    raise last_error


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

    Returns {"brand": ..., "product_name": ..., "category": ...,
    "subcategory": ...}.
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

    # 429-aware retry: KIE rejects (does not queue) requests over the
    # account's 20-per-10s cap — e.g. when several brand jobs' limiters
    # collectively overshoot. Back off a full window per attempt instead of
    # failing the image outright.
    response = None
    for attempt in range(1, 4):
        rate_limiter.acquire()
        response = requests.post(
            RESPONSES_URL,
            headers={**HEADERS_AUTH, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if response.status_code != 429:
            break
        wait = KIE_RATE_WINDOW_SECONDS * attempt
        print(f"  ⚠️ KIE rate limit hit (429), attempt {attempt}/3 — waiting {wait}s")
        if attempt < 3:
            time.sleep(wait)
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
        "subcategory": parsed.get("subcategory"),
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

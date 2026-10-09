#!/usr/bin/env python3
"""
Product-image extraction via OpenAI's official API (GPT-6 Luna through the
/v1/responses endpoint).

Sends the local (processed) image straight to GPT-6 Luna as a base64 data
URL — no upload step needed — and extracts brand, product name, and
food/non-food classification (replaced the earlier KIE-hosted extraction).

KIE is only still used by the AI image-generation path (generate.py, with
its uploads paced through kie_ratelimit.py).
"""

import base64
import json
import os
import re
import time

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = "gpt-6-luna"

# GPT-6 Luna standard-tier pricing, USD per 1M tokens (developers.openai.com
# /api/docs/models/gpt-6-luna): input $0.10, cached input $0.01, cache
# writes $0.125, output $0.50. The Responses API usage object reports
# cached reads but not cache writes, so the write rate is unused for now.
OPENAI_PRICE_PER_MTOK = {
    "input": 0.10,
    "cached_input": 0.01,
    "cache_write": 0.125,
    "output": 0.50,
}


def _openai_cost_usd(usage) -> float:
    """Estimated request cost from a Responses-API usage object, using the
    GPT-6 Luna rates above. Cached input tokens are billed at 10% of the
    uncached input rate."""
    cached = (usage.input_tokens_details.cached_tokens
              if usage.input_tokens_details else 0) or 0
    uncached_input = usage.input_tokens - cached
    return (
        uncached_input * OPENAI_PRICE_PER_MTOK["input"]
        + cached * OPENAI_PRICE_PER_MTOK["cached_input"]
        + usage.output_tokens * OPENAI_PRICE_PER_MTOK["output"]
    ) / 1_000_000

_openai_client: OpenAI | None = None


def _get_openai_client() -> OpenAI:
    """Lazily build the shared OpenAI client (SDK reads OPENAI_API_KEY;
    our own retry loop below handles transient failures, so the SDK's
    built-in retries are disabled)."""
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            raise AnalysisError("OPENAI_API_KEY is not set — add it to .env")
        _openai_client = OpenAI(api_key=OPENAI_API_KEY, max_retries=0)
    return _openai_client

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

The product_name MUST be the FULL descriptive product name — always include the flavor/variety AND the product type when they are visible.

Examples of the required level of detail:
- "Apple Harvest" alone is NOT enough → "Apple Harvest Soft & Chewy Candy"
- "Hummus" alone is NOT enough → "Brownie Batter Dessert Hummus"
- "Coffee Capsules" alone is NOT enough → "Pumpkin Spice Flavored Coffee Capsules"
- "Heat Puffs" alone is NOT enough → "The Flavor of Fear Sweet Phantom Heat Puffs"

Include in product_name (when visible on the package):
- Flavor / variety (e.g. Pumpkin Spice, Apple Harvest, Brownie Batter)
- Product type / form (e.g. Candy, Hummus, Coffee Capsules, Yogurt, Cookie Dough)
- Descriptive qualifiers that are part of the product's name (e.g. Soft & Chewy, Dessert, Flavored)

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


class AnalysisError(Exception):
    pass


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


def _image_content_part(image_source: str) -> dict:
    """Build an input_image content part from either a public URL or a
    local file path (local files are inlined as base64 data URLs)."""
    if image_source.startswith(("http://", "https://", "data:")):
        return {"type": "input_image", "image_url": image_source}
    with open(image_source, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}


def analyze_product_image(image_source: str, timeout: int = 600) -> dict:
    """Call OpenAI GPT-6 Luna to extract brand/product/category from an image.

    `image_source` is a local file path or a public image URL — local files
    are inlined as base64 data URLs, so no upload step is needed.

    Returns {"brand": ..., "product_name": ..., "category": ...,
    "subcategory": ..., "tokens_used": ..., "tokens": {...}, "cost_usd": ...}.
    Raises AnalysisError on any failure — caller is responsible for
    recording a failed-analysis placeholder instead of losing the post.
    """
    if not OPENAI_API_KEY:
        raise AnalysisError("OPENAI_API_KEY is not set — add it to .env")
    client = _get_openai_client()
    request_input = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": PRODUCT_EXTRACTION_PROMPT},
                _image_content_part(image_source),
            ],
        }
    ]

    # Retry loop: 429 (rate limit), 5xx (server errors — 500/501/502/503/
    # 524 etc.), connection failures, and completions with EMPTY text (the
    # model occasionally returns zero output tokens) all get up to 3
    # attempts. 429 backs off progressively per attempt; the rest back off
    # briefly (server-side hiccups usually clear in seconds).
    response = None
    for attempt in range(1, 4):
        try:
            response = client.responses.create(
                model=OPENAI_MODEL,
                input=request_input,
                reasoning={"effort": "low"},
                timeout=timeout,
            )
        except RateLimitError:
            wait = 15 * attempt
            print(f"  ⚠️ OpenAI rate limit hit (429), attempt {attempt}/3 — waiting {wait}s")
        except APIStatusError as e:
            if (e.status_code or 0) < 500:
                raise
            wait = 5 * attempt
            print(f"  ⚠️ OpenAI server error ({e.status_code}), attempt {attempt}/3 — waiting {wait}s")
        except APIConnectionError:
            wait = 5 * attempt
            print(f"  ⚠️ OpenAI connection failed, attempt {attempt}/3 — waiting {wait}s")
        else:
            if response.output_text:
                break
            wait = 5 * attempt
            print(f"  ⚠️ OpenAI returned an empty reply (0 output tokens), attempt {attempt}/3 — waiting {wait}s")
        response = None
        if attempt < 3:
            time.sleep(wait)
    if response is None:
        raise AnalysisError("OpenAI analysis failed after 3 attempts")

    usage = response.usage
    cached_tokens = (usage.input_tokens_details.cached_tokens
                     if usage.input_tokens_details else 0) or 0
    reasoning_tokens = (usage.output_tokens_details.reasoning_tokens
                        if usage.output_tokens_details else 0) or 0
    cost_usd = _openai_cost_usd(usage)
    print(f"  🔢 Tokens — input: {usage.input_tokens} (cached: {cached_tokens}), "
          f"output: {usage.output_tokens} (reasoning: {reasoning_tokens}), "
          f"total: {usage.total_tokens} | est. cost: ${cost_usd:.4f}")

    parsed = _parse_json_block(response.output_text)

    return {
        "brand": parsed.get("brand"),
        "product_name": parsed.get("product_name"),
        "category": parsed.get("category"),
        "subcategory": parsed.get("subcategory"),
        "tokens_used": usage.total_tokens,
        "tokens": {
            "input": usage.input_tokens,
            "cached_input": cached_tokens,
            "output": usage.output_tokens,
            "reasoning": reasoning_tokens,
        },
        "cost_usd": round(cost_usd, 4),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 analysis.py <path_to_image>")
        raise SystemExit(1)

    image_path = sys.argv[1]

    print(f"Analyzing {image_path} with {OPENAI_MODEL}...")
    result = analyze_product_image(image_path)
    print(json.dumps(result, indent=2, ensure_ascii=False))

#!/usr/bin/env python3
"""
AI-based product categorization using xAI's Grok models.

Given a list of product names from ONE site, ask the model to assign each
product a category from a fixed set of categories. Using a fixed/closed set
ensures categories line up consistently across different scraper sites so
products can be aggregated into the same category carousel on the final
WordPress page.
"""

import json
import os
from typing import Optional

from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

XAI_API_KEY = os.getenv("XAI_API_KEY")

# Fixed category set — keep this closed so the same category name is reused
# across every scraper site, letting the aggregate page group them together.
CATEGORIES = [
  "Bakery & Deli",
  "Dairy & Eggs",
  "Meat & Seafood",
  "Frozen Foods & Breakfast",
  "Snacks & Pantry Staples",
  "Beverages & Energy",
    "Other"
]


class CategoryGroup(BaseModel):
    category: str = Field(description=f"Must be exactly one of: {', '.join(CATEGORIES)}")
    indexes: list[int] = Field(description="0-based indexes (from the input list) of every product that belongs to this category")


class CategorizedGroupList(BaseModel):
    groups: list[CategoryGroup] = Field(description="One entry per category actually used — do not repeat a category, list all its product indexes together")


def _build_prompt(names: list[str]) -> str:
    numbered = "\n".join(f"{i}: {name}" for i, name in enumerate(names))
    return (
        "Group the products below into the single best-fitting category from this fixed list:\n"
        f"{', '.join(CATEGORIES)}\n\n"
        "Use 'Other' only if truly nothing else fits.\n"
        "Return ONE group per category used, each listing ALL matching product indexes together "
        "(do not repeat the same category in multiple groups, and every index must appear exactly once overall).\n\n"
        f"Products:\n{numbered}"
    )


def categorize_products(products: list[dict], site_name: str = "") -> list[dict]:
    """Attach a 'category' field (from the fixed CATEGORIES list) to each product dict.

    Sends only product names to the AI in a single batched call per site.
    Mutates and returns the same list. On any failure, falls back to
    category='Other' for all products so the pipeline never breaks.
    """
    if not products:
        return products

    if not XAI_API_KEY:
        print("  ⚠️  XAI_API_KEY not set — skipping AI categorization, defaulting to 'Other'")
        for p in products:
            p.setdefault("category", "Other")
        return products

    names = [p.get("name") or f"Item {i + 1}" for i, p in enumerate(products)]

    try:
        from openai import OpenAI
        import httpx

        client = OpenAI(
            api_key=XAI_API_KEY,
            base_url="https://api.x.ai/v1",
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

        print(f"  🤖 Categorizing {len(names)} products from {site_name or 'site'} via AI...")

        request_kwargs = dict(
            model="grok-4.5",
            max_output_tokens=500000,
            reasoning={
                "effort": "low"
            },
            store=False,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "CategorizedGroupList",
                    "schema": CategorizedGroupList.model_json_schema(),
                    "strict": True,
                }
            },
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a product categorization assistant. You always return "
                        "valid JSON matching the given schema — one group per category, "
                        "each group listing all of its product indexes together. "
                        "Do not explain your reasoning or add any commentary — output only the JSON."
                    ),
                },
                {"role": "user", "content": _build_prompt(names)},
            ],
        )

        response_text = ""
        with client.responses.stream(**request_kwargs) as stream:
            for event in stream:
                delta = getattr(event, "delta", None)
                if delta:
                    print(delta, end="", flush=True)
            print()
            final = stream.get_final_response()
            response_text = final.output_text

        data = json.loads(response_text)
        result = CategorizedGroupList(**data)
        valid_categories = set(CATEGORIES)
        for group in result.groups:
            category = group.category if group.category in valid_categories else "Other"
            for idx in group.indexes:
                if 0 <= idx < len(products):
                    products[idx]["category"] = category

        for p in products:
            p.setdefault("category", "Other")

        print(f"  ✅ Categorized {len(products)} products")

    except Exception as e:
        print(f"  ⚠️  AI categorization failed ({e}) — defaulting to 'Other'")
        for p in products:
            p.setdefault("category", "Other")

    return products

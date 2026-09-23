import json
import re
import time
from urllib.parse import quote
from bs4 import BeautifulSoup
from curl_cffi import requests


SEARCH_URL = "https://www.traderjoes.com/api/graphql"

# The exact persisted query the site's own search sends.
SEARCH_QUERY = """query SearchProducts($search: String, $pageSize: Int, $currentPage: Int, $storeCode: String = "TJ", $availability: String = "1", $published: String = "1") {
  products(
    search: $search
    filter: {store_code: {eq: $storeCode}, published: {eq: $published}, availability: {match: $availability}}
    pageSize: $pageSize
    currentPage: $currentPage
  ) {
    items {
      item_story_marketing
      primary_image
      primary_image_meta {
        url
        metadata
        __typename
      }
      published
      sku
      url_key
      name
      item_description
      item_title
      sales_size
      sales_uom_code
      sales_uom_description
      country_of_origin
      availability
      new_product
      promotion
      price_range {
        minimum_price {
          final_price {
            currency
            value
            __typename
          }
          __typename
        }
        __typename
      }
      retail_price
      ingredients {
        display_sequence
        ingredient
        __typename
      }
      __typename
    }
    total_count
    page_info {
      current_page
      page_size
      total_pages
      __typename
    }
    __typename
  }
}
"""


class TraderJoesSearcher:
    """Trader Joe's product search via Trader Joe's own GraphQL API
    (traderjoes.com/api/graphql) — NOT Instacart like the other scrapers.
    Returns the same result shape so it's a drop-in in the pipeline."""

    def __init__(self, proxy=None):
        self.proxy = proxy
        self.session = requests.Session()

        self.proxies = (
            {
                "http": proxy,
                "https": proxy,
            }
            if proxy
            else None
        )

    def warmup(self, max_retries: int = 3):
        """Warm up the Trader Joe's session — retried with backoff."""
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                r = self.session.get(
                    "https://www.traderjoes.com/home",
                    impersonate="chrome131",
                    proxies=self.proxies,
                    timeout=30,
                )
                r.raise_for_status()
                print("✓ Session warmed")
                return
            except Exception as e:
                last_error = e
                print(f"  ⚠️ Warmup attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    time.sleep(2 * attempt)
        raise last_error

    def search(self, query):
        last_exc = None
        for attempt in range(1, 4):
            try:
                return self._search_once(query)
            except Exception as e:
                last_exc = e
                if attempt < 3:
                    wait = 3 * attempt
                    print(f"    ⚠️  Search attempt {attempt}/3 failed ({e.__class__.__name__}: {e}) — retrying in {wait}s...")
                    time.sleep(wait)
                    try:
                        self.warmup()
                    except Exception:
                        pass
        raise last_exc

    def _search_once(self, query):

        payload = {
            "operationName": "SearchProducts",
            "variables": {
                "storeCode": "TJ",
                "availability": "1",
                "published": "1",
                "search": query,
                "currentPage": 1,
                "pageSize": 15,
            },
            "query": SEARCH_QUERY,
        }

        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.traderjoes.com",
            "referer": f"https://www.traderjoes.com/home/search?q={quote(query)}&global=yes",
        }

        r = self.session.post(
            "https://www.traderjoes.com/api/graphql",
            json=payload,
            headers=headers,
            impersonate="chrome131",
            proxies=self.proxies,
            timeout=30,
        )

        r.raise_for_status()

        data = r.json()

        # with open(Path(__file__).parent / "trader_joes_last_response.json", "w", encoding="utf-8") as f:
        #     json.dump(data, f, indent=2, ensure_ascii=False)
        # print("✓ Raw response saved to trader_joes_last_response.json")

        if data.get("errors"):
            raise Exception(data["errors"])

        items = (data.get("data", {}).get("products", {}) or {}).get("items") or []
        if not items:
            return None

        # Keyword match, same rule as the ALDI scraper: only the FIRST result
        # is considered, and its name must share at least 2 words with the
        # query (1 for single-word queries) — never fall back to the second
        # or third item. Generic filler words don't count toward the match
        # ("Butter with X" shouldn't match "Butter with Y" off "butter with"
        # alone).
        STOP_WORDS = {"with", "and", "the", "a", "an", "of", "for", "or",
                      "in", "to", "by", "style", "fresh"}

        def words(text):
            return set(re.findall(r"[a-z0-9]+", (text or "").lower())) - STOP_WORDS

        query_words = words(query)
        min_matches = min(2, len(query_words))

        product = None
        first = items[0]
        name = first.get("item_title") or first.get("name") or ""
        if len(query_words & words(name)) >= min_matches:
            product = first

        if not product:
            return None

        sku = product.get("sku") or ""

        price_value = (
            product.get("price_range", {})
            .get("minimum_price", {})
            .get("final_price", {})
            .get("value")
        )
        if price_value is not None:
            price = f"${float(price_value):.2f}"
        else:
            price = None

        sales_size = product.get("sales_size")
        sales_uom = product.get("sales_uom_description")
        size = f"{sales_size} {sales_uom}".strip() if (
            sales_size := product.get("sales_size")
        ) is not None and (sales_uom := product.get("sales_uom_description")) else None

        # Marketing story is the richest description; item_description is
        # usually null on TJ listings.
        description = None
        story = product.get("item_story_marketing")
        if story:
            description = re.sub(r"<[^>]+>", " ", story)
            description = " ".join(description.split())
        elif product.get("item_description"):
            description = product["item_description"]

        image_path = product.get("primary_image") or ""
        image_url = f"https://www.traderjoes.com{image_path}" if image_path.startswith("/") else image_path

        return {
            "name": product.get("item_title") or product.get("name"),
            "brand": "Trader Joe's",
            "price": price,
            "size": size,
            "image_url": image_url,
            "product_id": product.get("sku"),
            "product_url": f"https://www.traderjoes.com/home/products/{product.get('sku')}",
            "description": description,
            "available": product.get("availability") == "1",
            "raw_json": product,
        }

    def get_description(self, product_id):
        # The search response already carries the full item story — no
        # separate product-page fetch needed.
        return None


if __name__ == "__main__":

    proxy = None

    tj = TraderJoesSearcher(proxy)

    #tj.warmup()

    product = tj.search("Spreadable Butter with Olive Oil Salted")

    if product:

        print("=" * 80)
        print("Name        :", product["name"])
        print("Brand       :", product["brand"])
        print("Price       :", product["price"])
        print("Size        :", product["size"])
        print("Product ID  :", product["product_id"])
        print("Available   :", product["available"])
        print("Product URL :", product["product_url"])
        print("Image URL   :", product["image_url"])
        print()
        print("Description:")
        print(product["description"])
        print("=" * 80)

    else:
        print("No products found.")

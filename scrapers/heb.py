import json
import re
import time
from urllib.parse import quote
from bs4 import BeautifulSoup
from curl_cffi import requests


def _adjust_price(price_string):
    """Instacart marks up H-E-B's shelf prices — multiply by 0.85 to match
    the in-store price. '$14.92' -> '$12.68'. Unparseable strings pass
    through unchanged."""
    if not price_string:
        return price_string
    match = re.search(r"([\d,]+\.?\d*)", price_string)
    if not match:
        return price_string
    try:
        value = float(match.group(1).replace(",", "")) * 0.854
    except ValueError:
        return price_string
    return f"${value:,.2f}"


class HEBSearcher:
    """H-E-B product search via Instacart's GraphQL search endpoint —
    the same API the other storefront scrapers use (same response shape),
    just on instacart.com with H-E-B's shop/zone ids. Prices are scaled by
    0.85 to match H-E-B's in-store pricing."""

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
        """Warm up the Instacart session — retried with backoff, since a cold
        or failed session makes the first search likely to be blocked."""
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                r = self.session.get(
                    "https://www.instacart.com/store/h-e-b",
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

        variables = {
            "action": None,
            "query": query,
            "pageViewId": "5e7c4399-2b44-55a3-a702-811fef08f00e",
            "elevatedProductId": None,
            "searchSource": "search",
            "filters": [],
            "disableReformulation": False,
            "disableLlm": False,
            "forceInspiration": False,
            "orderBy": "bestMatch",
            "clusterId": None,
            "includeDebugInfo": False,
            "clusteringStrategy": None,
            "contentManagementSearchParams": {
                "itemGridColumnCount": 3
            },
            "shopId": "27436",
            "postalCode": "90012",
            "zoneId": "983",
            "first": 4,
        }

        extensions = {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "7bd0f8d9588792702f7f73f752d0b4f5d12f071c529c9f482104f8f13447cb3b",
            }
        }

        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.instacart.com",
            "referer": f"https://www.instacart.com/store/h-e-b/s?k={quote(query)}",
            "x-client-identifier": "web",
            "x-client-user-id": "21192267958514052",
            "x-ic-view-layer": "true",
            "x-page-view-id": "5e7c4399-2b44-55a3-a702-811fef08f00e",
        }

        params = {
            "operationName": "SearchResultsPlacements",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(extensions, separators=(",", ":")),
        }

        r = self.session.get(
            "https://www.instacart.com/graphql",
            params=params,
            headers=headers,
            impersonate="chrome131",
            proxies=self.proxies,
            timeout=30,
        )

        r.raise_for_status()

        data = r.json()

        # with open(Path(__file__).parent / "heb_last_response.json", "w", encoding="utf-8") as f:
        #     json.dump(data, f, indent=2, ensure_ascii=False)
        # print("✓ Raw response saved to heb_last_response.json")

        if data.get("errors"):
            raise Exception(data["errors"])

        product = None

        placements = data["data"]["searchResultsPlacements"]["placements"]

        def grid_items(placement):
            content = placement.get("content", {})
            if content.get("__typename") != "SearchContentManagementSearchItemGrid":
                return None
            return content.get("items", []) or []

        # "No results for ..." header — the response still contains item grids
        # after this (suggested/related products), but they are NOT matches.
        for placement in placements:
            content = placement.get("content", {})
            if content.get("__typename") != "SearchContentManagementSearchItemGridHeader":
                continue
            header_text = json.dumps(content, ensure_ascii=False)
            if "No results for" in header_text:
                return None
            break  # only the first header matters — it states the result status

        # Main feed only: the first non-empty item grid (main results come
        # first in placement order, before any related/ad grids). No fallback
        # to related results — if the item isn't in the main feed, no match.
        for placement in placements:
            items = grid_items(placement)
            if items:
                product = items[0]
                break

        if not product:
            return None

        product_id = product["productId"]
        evergreen_url = product.get("evergreenUrl") or product_id

        description = self.get_description(product_id)

        return {
            "name": product.get("name"),
            "brand": product.get("brandName"),
            "price": _adjust_price(
                product.get("price", {})
                .get("viewSection", {})
                .get("priceString")
            ),
            "size": product.get("size"),
            "image_url": (
                product.get("viewSection", {})
                .get("itemImage", {})
                .get("url")
            ),
            "product_id": product_id,
            "product_url": f"https://www.instacart.com/store/h-e-b/products/{evergreen_url}",
            "description": description,
            "available": (
                product.get("availability", {})
                .get("available")
            ),
            "raw_json": product,
        }

    def get_description(self, product_id):
        last_exc = None
        for attempt in range(1, 4):
            try:
                return self._get_description_once(product_id)
            except Exception as e:
                last_exc = e
                if attempt < 3:
                    wait = 3 * attempt
                    print(f"    ⚠️  Description attempt {attempt}/3 failed ({e.__class__.__name__}: {e}) — retrying in {wait}s...")
                    time.sleep(wait)
        raise last_exc

    def _get_description_once(self, product_id):
        from urllib.parse import unquote

        url = f"https://www.instacart.com/store/h-e-b/products/{product_id}"

        r = self.session.get(
            url,
            impersonate="chrome131",
            proxies=self.proxies,
            timeout=30,
        )

        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        script_tag = soup.find("script", id="node-apollo-state")

        if script_tag and script_tag.string:
            try:
                apollo_data = json.loads(unquote(script_tag.string.strip()))

                def find_detail_sections(obj):
                    if isinstance(obj, dict):
                        sections = obj.get("detailSections")
                        if isinstance(sections, list) and sections:
                            texts = [s["bodyString"] for s in sections if s.get("bodyString")]
                            if texts:
                                return " ".join(texts)
                        for v in obj.values():
                            result = find_detail_sections(v)
                            if result:
                                return result
                    elif isinstance(obj, list):
                        for item in obj:
                            result = find_detail_sections(item)
                            if result:
                                return result
                    return None

                return find_detail_sections(apollo_data)

            except (json.JSONDecodeError, Exception):
                pass

        return None


if __name__ == "__main__":

    proxy = None

    heb = HEBSearcher(proxy)

    heb.warmup()

    product = heb.search("H-E-B French Bread")

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

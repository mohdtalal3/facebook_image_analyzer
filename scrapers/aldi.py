import json
import re
import time
from urllib.parse import quote
from bs4 import BeautifulSoup
from curl_cffi import requests


class AldiSearcher:

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
        """Warm up the Aldi session — retried with backoff, since a cold or
        failed session makes the first search likely to be blocked."""
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                r = self.session.get(
                    "https://www.aldi.us/",
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
            "pageViewId": "cb814f70-6fb4-52c7-9fc6-cf2474b81340",
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
                "itemGridColumnCount": 6
            },
            "shopId": "29998",
            "postalCode": "60174",
            "zoneId": "384",
            "first": 4,
        }

        extensions = {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "406e5b9dfc9dc9b209b2c72012622de595fb4040d17f68efa4d4e104657273ee",
            }
        }

        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.aldi.us",
            "referer": f"https://www.aldi.us/store/aldi/s?k={quote(query)}",
            "x-client-identifier": "web",
            "x-client-user-id": "21123509038450780",
            "x-ic-view-layer": "true",
            #"x-ic-qp": "a48ad652-f0d1-5988-996c-0182cf2361aa",
            "x-page-view-id": "cb814f70-6fb4-52c7-9fc6-cf2474b81340",
        }

        params = {
            "operationName": "SearchResultsPlacements",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(extensions, separators=(",", ":")),
        }

        r = self.session.get(
            "https://www.aldi.us/graphql",
            params=params,
            headers=headers,
            impersonate="chrome131",
            proxies=self.proxies,
            timeout=30,
        )

        r.raise_for_status()

        data = r.json()

        # with open("aldi_first_response.json", "w", encoding="utf-8") as f:
        #     json.dump(data, f, indent=2, ensure_ascii=False)
        # print("✓ Raw first response saved to aldi_first_response.json")

        if data.get("errors"):
            raise Exception(data["errors"])

        product = None

        placements = data["data"]["searchResultsPlacements"]["placements"]

        def grid_items(placement):
            content = placement.get("content", {})
            if content.get("__typename") != "SearchContentManagementSearchItemGrid":
                return None
            return content.get("items", []) or []

        # 1) Main results first
        for placement in placements:
            items = grid_items(placement)

            section_type = (
                placement.get("content", {})
                .get("viewSection", {})
                .get("trackingProperties", {})
                .get("section_details", {})
                .get("section_type")
            )

            if section_type == "related_results":
                continue

            if items:
                product = items[0]
                break

        # 2) Fallback: related results, only if a query word matches the name
        if not product:
            def words(text):
                return set(re.findall(r"[a-z0-9]+", text.lower()))

            query_words = words(query)
            min_matches = min(2, len(query_words))

            for placement in placements:
                items = grid_items(placement)

                section_type = (
                    placement.get("content", {})
                    .get("viewSection", {})
                    .get("trackingProperties", {})
                    .get("section_details", {})
                    .get("section_type")
                )

                if section_type != "related_results":
                    continue

                if not items:
                    continue

                item = items[0]
                name = item.get("name") or ""
                if len(query_words & words(name)) >= min_matches:
                    product = item
                    break

        if not product:
            return None

        product_id = product["productId"]

        description = self.get_description(product_id)

        return {
            "name": product.get("name"),
            "brand": product.get("brandName"),
            "price": (
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
            "product_url": f"https://www.aldi.us/store/aldi/products/{product_id}",
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

        url = f"https://www.aldi.us/store/aldi/products/{product_id}"

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


    aldi = AldiSearcher(proxy)

    aldi.warmup()

    product = aldi.search("Reversible Porch Sign")

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

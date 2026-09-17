import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests
from dotenv import load_dotenv

load_dotenv()


_API_URL = "https://www.kroger.com/atlas/v1/product/v2/products"
_SEARCH_URL = "https://www.kroger.com/atlas/v1/search/v1/products-search"

_PROJECTIONS = (
    "items.full,offers.compact,nutrition.label,"
    "inventory.projected,variantGroupings.compact"
)

_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "referer": "https://www.kroger.com/",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="131", "Chromium";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "priority": "u=1, i",
    "sec-ch-device-memory": "16",
    "sec-ch-viewport-width": "1401",
    "kroger-visitor-id": "0088f2e2-911c-1c10-dafe-1d00a6098db8",
    "x-ab-test": '[{"testID":"0b2aa3","testOrigin":"d3","testVersion":"A"},{"testID":"378f49","testOrigin":"9e","testVersion":"B"}]',
    "x-dtreferer": "https://www.kroger.com/",
    "x-kroger-channel": "WEB",
    "x-modality": '{"type":"PICKUP","locationId":"02100537"}',
    "x-modality-type": "PICKUP",
    "x-facility-id": "02100537",
    "x-laf-object": (
        '[{"modality":{"type":"PICKUP",'
        '"handoffLocation":{"storeId":"02100537","facilityId":"4000"},'
        '"handoffAddress":{"address":{"addressLines":["605 W 4Th St"],'
        '"cityTown":"Rolla","name":"Rolla","postalCode":"65401",'
        '"stateProvince":"MO","residential":false,"countryCode":"US"},'
        '"location":{"lat":37.9467154,"lng":-91.7762367}}},'
        '"sources":[{"storeId":"02100537","facilityId":"4000"}],'
        '"assortmentKeys":["5b3c218b-e8ae-490b-8fd5-9dd2d7bcae6f"],'
        '"listingKeys":["02100537"]}]'
    ),
}

_MAX_RETRIES = 3


def _get_image(images: list) -> str | None:
    for perspective in ("front", "right"):
        for size in ("xlarge", "large", "medium", "small", "thumbnail"):
            for image in images:
                if image.get("perspective") == perspective and image.get("size") == size:
                    return image.get("url")
    return images[0].get("url") if images else None


class KrogerSearcher:
    """Fetch Kroger product data and images by GTIN13.

    Interface mirrors other searchers so it can be used as a drop-in source.
    NOTE: the atlas endpoint filters by GTIN13s, not keywords.
    """

    def __init__(self, proxy: str | None = None):
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def warmup(self):
        pass

    def _search_upcs(self, product_name: str) -> list[str]:
        """Step 1: search by keyword, return list of UPCs (GTIN13s)."""
        print(f"  [kroger] Searching: {product_name}")
        params = {
            "option.groupBy": "PRODUCT_VARIANT",
            "option.quickFacets": "true",
            "filter.locationId": "02100537",
            "filter.query": product_name,
            "filter.fulfillmentMethods": ["IN_STORE", "PICKUP", "DELIVERY"],
            "page.offset": "0",
            "page.size": "24",
            "option.personalization": "PURCHASE_HISTORY",
        }

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    _SEARCH_URL,
                    params=params,
                    headers=_HEADERS,
                    proxies=self.proxies,
                    timeout=30,
                    impersonate="chrome131",
                )
                resp.raise_for_status()
                data = resp.json()

                hits = data.get("data", {}).get("productsSearch", [])
                hits = [h for h in hits if h.get("searchEngineRank", 0) > 0]
                hits.sort(key=lambda h: h.get("searchEngineRank", 9999))
                upcs = [h["upc"] for h in hits if h.get("upc")]
                if not upcs:
                    print(f"  [kroger] No products found for: {product_name}")
                else:
                    print(f"  [kroger] Found {len(upcs)} UPCs, top: {upcs[0]}")
                return upcs

            except Exception as e:
                print(f"  [kroger] Search attempt {attempt}/{_MAX_RETRIES} failed: {e}")
                if attempt == _MAX_RETRIES:
                    return []
        return []

    def search(self, product_name: str) -> dict | None:
        """Search by product name; returns the first product normalised.

        Returned keys: name, price, image_url, product_url, description, brand, size
        """
        results = self.search_many([product_name])
        return results.get(product_name)

    def search_many(self, product_names: list[str]) -> dict[str, dict | None]:
        """Search multiple keywords efficiently.

        Sends one search request per keyword to collect rank-1 UPCs,
        then a SINGLE products request for all UPCs combined.

        Returns {keyword: normalised product dict or None}.
        """
        keyword_upc: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(self._search_upcs, name): name for name in product_names}
            for fut in as_completed(futures):
                name = futures[fut]
                upcs = fut.result()
                if upcs:
                    keyword_upc[name] = upcs[0]

        if not keyword_upc:
            return {name: None for name in product_names}

        all_upcs = list(dict.fromkeys(keyword_upc.values()))
        params = [("filter.gtin13s", u) for u in all_upcs]
        params.append(("filter.verified", "true"))
        params.append(("projections", _PROJECTIONS))

        products = self._fetch_products(params)

        by_upc = {
            p.get("item", {}).get("upc"): p.get("item", {})
            for p in products
        }

        results = {}
        for name in product_names:
            upc = keyword_upc.get(name)
            item = by_upc.get(upc) if upc else None
            if not item:
                results[name] = None
                continue

            results[name] = {
                "name":        item.get("description", ""),
                "price":       "",
                "image_url":   _get_image(item.get("images", [])) or "",
                "product_url": f"https://www.kroger.com/p/x/{item.get('upc', '')}" if item.get("upc") else "",
                "description": "",
                "brand":       (item.get("brand") or {}).get("name", ""),
                "size":        item.get("customerFacingSize", ""),
            }
        return results

    def _fetch_products(self, params: list) -> list:
        """Single products API call; returns raw products list."""
        upc_count = sum(1 for k, _ in params if k == "filter.gtin13s")
        print(f"  [kroger] Fetching {upc_count} products (single request)")
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    _API_URL,
                    params=params,
                    headers=_HEADERS,
                    proxies=self.proxies,
                    timeout=30,
                    impersonate="chrome131",
                )
                resp.raise_for_status()
                data = resp.json()
                products = data.get("data", {}).get("products", [])
                print(f"  [kroger] Got {len(products)} products")
                return products

            except Exception as e:
                print(f"  [kroger] Attempt {attempt}/{_MAX_RETRIES} failed: {e}")
                if attempt == _MAX_RETRIES:
                    return []
        return []


if __name__ == "__main__":
    terms = sys.argv[1:] or ["Olipop","Hostess Snack Cakes"]
    proxy = os.environ.get("STATIC_PROXY")
    searcher = KrogerSearcher(proxy=proxy)
    results = searcher.search_many(terms)

    for term, result in results.items():
        if result is None:
            print(f"\n✗ Not found: {term}")
        else:
            print(f"\n── {term} ────────────────────────────────")
            print(f"Name : {result['name']}")
            print(f"Image: {result['image_url']}")
            print(f"URL  : {result['product_url']}")
    print("────────────────────────────────────────────")

    with open("kroger_response.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print("Saved to kroger_response.json")
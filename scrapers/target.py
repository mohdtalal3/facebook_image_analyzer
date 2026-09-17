import re
import time
from urllib.parse import quote_plus

from curl_cffi import requests


BASE_URL = "https://cdui-orchestrations.target.com/cdui_orchestrations/v1/pages/slp"


class TargetSearcher:

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
        raise last_exc

    def _search_once(self, query):

        params = {
            "key": "9f36aeafbe60771e321a7cc95a78140772ab3e96",
            "platform": "WEB",
            "privacy_do_not_sell": "false",
            "targeted_advertising_opt_out": "false",
            "device_type": "DESKTOP",
            "sapphire_channel": "WEB",
            "sapphire_page": f"/s/{query}",
            "channel": "WEB",
            "page": f"/s/{query}",
            "visitor_id": "01A0AB43D82C02008F6EEB732DEE0301",
            "purchasable_store_ids": "2488,3254,1545,2483,1447",
            "state": "PB",
            "store_id": "2488",
            # "zip": "47002",
            # "country": "PK",
            "has_pending_inputs": "false",
            "count": "24",
            "default_purchasability_filter": "true",
            "new_search": "true",
            "offset": "0",
            "spellcheck": "true",
            "store_ids": "2488,3254,1545,2483,1447",
            "keyword": query,
            "is_seo_bot": "false",
            "include_data_source_modules": "true",
            "query_string": f"searchTerm={query}",
        }

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "sec-ch-ua-platform": '"macOS"',
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
            "sec-ch-ua-mobile": "?0",
            "origin": "https://www.target.com",
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": f"https://www.target.com/s?searchTerm={quote_plus(query)}",
            "accept-language": "en-US,en;q=0.9",
            "priority": "u=1, i",
        }

        r = self.session.get(
            BASE_URL,
            params=params,
            headers=headers,
            impersonate="chrome",
            proxies=self.proxies,
            timeout=30,
        )

        r.raise_for_status()

        data = r.json()

        products = extract_products(data)
        if not products:
            return None

        # No keyword checking — just take the first product
        return products[0]


def extract_products(data):
    """Pull name, price, url, image and description out of the raw API response."""
    products = []

    # Products can live in any data_source_module — collect them all
    for module in data.get("data_source_modules", []) or []:
        prods = (
            module.get("module_data", {})
            .get("search_response", {})
            .get("products", [])
        )
        for p in prods or []:
            item = p.get("item") or {}
            desc = item.get("product_description") or {}
            price = p.get("price") or {}

            bullets = [
                re.sub(r"<[^>]+>", "", b).strip()
                for b in (desc.get("bullet_descriptions") or [])
                if b
            ]

            products.append({
                "name": desc.get("title", ""),
                "price": price.get("formatted_current_price", ""),
                "price_value": price.get("current_retail"),
                "product_url": item.get("enrichment", {}).get("buy_url", ""),
                "image_url": (
                    (item.get("enrichment", {}).get("image_info", {}) or {})
                    .get("primary_image", {}) or {}
                ).get("url", ""),
                "description": "\n".join(bullets),
            })

    return products


if __name__ == "__main__":
    proxy = None

    target = TargetSearcher(proxy)

    product = target.search("Breaded Chicken Bites")

    if product:
        print("=" * 80)
        print("Name        :", product["name"])
        print("Price       :", product["price"])
        print("Product URL :", product["product_url"])
        print("Image URL   :", product["image_url"])
        print()
        print("Description:")
        print(product["description"])
        print("=" * 80)
    else:
        print("No products found.")

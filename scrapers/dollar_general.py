import json
import re
import time
import uuid
from urllib.parse import quote
from curl_cffi import requests


SEARCH_URL = "https://dggo.dollargeneral.com/omni/api/v5/search/shoppinglist/product/Provider"
STORE_NBR = 19052

# Static app-level tokens embedded in Dollar General's own web app — these do
# NOT expire (verified: the search endpoint returns 200 with NO Bearer token;
# only these two app tokens are required). If searches ever start failing,
# re-capture the curl from dollargeneral.com's search network tab and refresh
# these two values.
DG_APP_TOKEN = "6dinqus4908fkssw9h7aa8ldcgkimn3p"
DG_PARTNER_API_TOKEN = "11619A82-8E80-4A6F-8AD2-A14F4A8FFD74"

# Session/device identifiers captured from the site's own web app — the API
# silently returns an EMPTY 200 response for unknown ids, so these captured
# values are required (they are not JWTs — no expiry).
APP_SESSION_TOKEN = "BU3FEQDRSYGLQH4KM0LQ4M949D6FSQFQ"
CUSTOMER_GUID = "000000000000000000000000002803970966"
DEVICE_UNIQUE_ID = "0f47b2e6-798c-4296-8b4d-cbecd8697a97"


class DollarGeneralSearcher:
    """Dollar General product search via DG's own omni search API
    (dggo.dollargeneral.com) — NOT Instacart like most of the other
    scrapers. No login/Bearer token needed: only the site's static app
    tokens (DG_APP_TOKEN / DG_PARTNER_API_TOKEN) plus the captured
    session/device identifiers below are required.
    Returns the same result shape as the other searchers."""

    def __init__(self, proxy=None):
        self.proxy = proxy
        self.session = requests.Session()
        # Session/device identifiers captured from the site's own web app —
        # the API silently returns an EMPTY response for unknown ids, so
        # these captured values are required (they are not JWTs — no expiry).
        self.session_token = APP_SESSION_TOKEN
        self.device_unique_id = DEVICE_UNIQUE_ID
        self.customer_guid = CUSTOMER_GUID

        self.proxies = (
            {
                "http": proxy,
                "https": proxy,
            }
            if proxy
            else None
        )

    def warmup(self, max_retries: int = 3):
        """Warm up the Dollar General session — retried with backoff."""
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                r = self.session.get(
                    "https://www.dollargeneral.com/",
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
            "StoreNbr": 19052,
            "SearchTerm": query,
            "PageSize": 24,
            "PageStartRecordIndex": 0,
            "Filters": {
                "category": [],
                "brand": [],
                "dgDelivery": False,
                "dgPickUp": False,
                "dgShipTohome": False,
                "soldAtStore": True,
                "inStock": True,
            },
            "IncludeSponsored": True,
            "IncludeShipToHome": True,
            "IncludeDeals": True,
            "offerSourceType": 0,
            "SearchType": 0,
            #"bloomreachCookieId": "uid=4585777865169:v=12.0:ts=1790114755594:hc=2",
        }

        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://www.dollargeneral.com",
            "referer": "https://www.dollargeneral.com/",
            "x-dg-appsessiontoken": self.session_token,
            "x-dg-apptoken": DG_APP_TOKEN,
            "x-dg-cloud-service": "true",
            "x-dg-customerguid": self.customer_guid,
            "x-dg-deviceuniqueid": self.device_unique_id,
            "x-dg-partnerapitoken": DG_PARTNER_API_TOKEN,
        }

        r = self.session.post(
            "https://dggo.dollargeneral.com/omni/api/v5/search/shoppinglist/product/Provider",
            json=payload,
            headers=headers,
            impersonate="chrome131",
            proxies=self.proxies,
            timeout=30,
        )

        r.raise_for_status()

        data = r.json()

        items = data.get("Items") or []
        if not items:
            return None

        # Keyword match, same rule as the other scrapers: only the FIRST
        # result is considered, and its name must share at least 2 words with
        # the query (1 for single-word queries) — never fall back to the
        # second or third item. Generic filler words don't count.
        STOP_WORDS = {"with", "and", "the", "a", "an", "of", "for", "or",
                      "in", "to", "by", "style", "fresh"}

        def words(text):
            return set(re.findall(r"[a-z0-9]+", (text or "").lower())) - STOP_WORDS

        query_words = words(query)
        min_matches = min(2, len(query_words))

        product = None
        first = items[0]
        name = first.get("Description") or ""
        if len(query_words & words(name)) >= min_matches:
            product = first

        if not product:
            return None

        upc = product.get("UPC")
        price_value = product.get("Price")

        # Product page (the UPC is the only part that matters in the URL) —
        # carries brand, unit size and the long description the search
        # response doesn't have. get_description() stashes the full details
        # dict on the instance for brand/size.
        description = self.get_description(upc)
        details = getattr(self, "_last_details", None) or {}

        name = product.get("Description") or ""
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "product"

        return {
            "name": product.get("Description"),
            "brand": details.get("brand"),
            "price": f"${price_value:.2f}" if isinstance(price_value, (int, float)) else None,
            "size": details.get("unitSize"),
            "image_url": product.get("Image"),
            "product_id": str(upc) if upc is not None else None,
            "product_url": f"https://www.dollargeneral.com/p/{quote(slug)}/{upc}" if upc is not None else None,
            "description": description,
            "available": bool(product.get("IsSellable")) and (product.get("AvailableQty") or 0) > 0,
            "raw_json": product,
        }

    def get_description(self, product_id):
        """Fetch the product page (URL shape: /p/<any-slug>/<UPC>) and parse
        its `product-detail-json-response` div — an HTML-escaped JSON blob
        carrying longDescription, brand and unitSize. Returns the
        longDescription string, or None on any failure (the pipeline handles
        a missing description fine)."""
        if not product_id:
            return None
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
        print(f"    ⚠️  Description failed for {product_id}: {last_exc}")
        return None

    def _get_description_once(self, product_id):
        import html as html_mod

        url = f"https://www.dollargeneral.com/p/x/{product_id}"

        r = self.session.get(
            url,
            impersonate="chrome131",
            proxies=self.proxies,
            timeout=30,
        )

        r.raise_for_status()

        match = re.search(r'data-product-detail-json-response="([^"]+)"', r.text)
        if not match:
            return None

        try:
            details = json.loads(html_mod.unescape(match.group(1))).get("productDetails") or {}
        except (json.JSONDecodeError, ValueError):
            return None

        # Stash brand/size on the instance so _search_once can pick them up.
        self._last_details = details
        return details.get("longDescription")


if __name__ == "__main__":

    proxy = None

    dg = DollarGeneralSearcher(proxy)

   # dg.warmup()

    product = dg.search("Cheerios Honey Nut Cereal")

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

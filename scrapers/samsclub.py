import json
import re
import time
import os
import html
import uuid
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from curl_cffi import requests


try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass


SEARCH_API_URL = (
    "https://www.samsclub.com/orchestra/snb/graphql/Search/"
    "3830cd87ebf5ad15a5e8d7dc7912687edc9316cd01a3a1efdb9fbebc50561a8b/search"
)

PDP_API_URL = (
    "https://www.samsclub.com/orchestra/pdp/graphql/ItemById/"
    "dd3d6e9c0df88450b413b724a0996b5a7b48210918c0f5f59d130b22912a95ce/ip/{item_id}"
)


class SamsClubSearcher:

    def __init__(self, proxy=None):
        self.proxy = proxy or os.getenv("STATIC_PROXY")
        self.session = requests.Session()
        self.proxies = (
            {"http": self.proxy, "https": self.proxy}
            if self.proxy
            else None
        )

    def _get(self, url, **kwargs):
        return self.session.get(
            url,
            impersonate="chrome",
            proxies=self.proxies,
            timeout=30,
            **kwargs,
        )

    # --------------------------------------------------
    # SEARCH
    # --------------------------------------------------
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

        print(f"→ Sending search request for: '{query}'...")

        variables = {
            "id": "",
            "dealsId": "",
            "query": query,
            "nudgeContext": "",
            "page": 1,
            "prg": "desktop",
            "catId": "",
            "facet": "",
            "sort": "best_match",
            "rawFacet": "",
            "seoPath": "",
            "ps": 45,
            "limit": 45,
            "ptss": "",
            "trsp": "",
            "beShelfId": "",
            "recall_set": "",
            "module_search": "",
            "min_price": "",
            "max_price": "",
            "storeSlotBooked": "",
            "additionalQueryParams": {
                "minPrice": "",
                "maxPrice": "",
                "hidden_facet": None,
                "translation": None,
                "isMoreOptionsTileEnabled": True,
                "isGenAiEnabled": True,
                "rootDimension": "",
                "altQuery": "",
                "selectedFilter": "",
                "neuralSearchSeeAll": False,
                "isModuleArrayReq": False,
                "enableGenericItemTileOptions": False,
                "isLMPBrowsePage": False,
            },
            "searchArgs": {
                "query": query,
                "cat_id": "",
                "prg": "desktop",
                "facet": "",
            },
            "enableDesktopHighlights": False,
            "enableVolumePricing": False,
            "enableCopyBlock": True,
            "enableVariantCount": False,
            "enableSlaBadgeV2": False,
            "enableUnifiedProductFragment": False,
            "enableESSCarousel": False,
            "enableSearchBenefitsBanner": False,
            "enableSparkyPLPModule": False,
            "fitmentFieldParams": {
                "powerSportEnabled": True,
                "dynamicFitmentEnabled": True,
                "extendedAttributesEnabled": True,
                "extendedAttributesV2Enabled": False,
                "fuelTypeEnabled": False,
            },
            "fitmentSearchParams": {
                "id": "",
                "dealsId": "",
                "query": query,
                "nudgeContext": "",
                "page": 1,
                "prg": "desktop",
                "catId": "",
                "facet": "",
                "sort": "best_match",
                "rawFacet": "",
                "seoPath": "",
                "ps": 45,
                "limit": 45,
                "ptss": "",
                "trsp": "",
                "beShelfId": "",
                "recall_set": "",
                "module_search": "",
                "min_price": "",
                "max_price": "",
                "storeSlotBooked": "",
                "cat_id": "",
                "_be_shelf_id": "",
            },
            "searchParams": {
                "id": "",
                "dealsId": "",
                "query": query,
                "nudgeContext": "",
                "page": 1,
                "prg": "desktop",
                "catId": "",
                "facet": "",
                "sort": "best_match",
                "rawFacet": "",
                "seoPath": "",
                "ps": 45,
                "limit": 45,
                "ptss": "",
                "trsp": "",
                "beShelfId": "",
                "recall_set": "",
                "module_search": "",
                "min_price": "",
                "max_price": "",
                "storeSlotBooked": "",
                "cat_id": "",
                "_be_shelf_id": "",
            },
            "fetchBadSplit": True,
            "enableFashionTopNav": False,
            "enableUnifiedSchema": True,
            "postProcessingVersion": 2,
            "version": "v2",
            "enableRelatedSearches": True,
            "enablePortableFacets": True,
            "enableFacetCount": True,
            "fetchMarquee": True,
            "fetchSkyline": True,
            "fetchGallery": False,
            "fetchSbaTop": True,
            "fetchDataV1": False,
            "fetchDataV2": False,
            "fungibilityEnabled": False,
            "enableAdsPromoData": False,
            "fetchDac": False,
            "tenant": "SAMS_GLASS",
            "enableMultiSave": False,
            "enableInStoreShelfMessage": False,
            "enableSellerType": False,
            "enableItemRank": False,
            "enableOptimisticWeightUpdate": False,
            "enableAdditionalSearchDepartmentAnalytics": False,
            "enableFulfillmentTagsEnhacements": False,
            "enableRxDrugScheduleModal": False,
            "enablePromoData": False,
            "enableSignInToSeePrice": True,
            "enablePromotionMessages": True,
            "enableDebugAnalyticsTags": True,
            "enableItemLimits": True,
            "enableCanAddToList": True,
            "enableIsFreeWarranty": True,
            "enableShopSimilarBottomSheet": False,
            "adsParams": {"fungibilityEnabled": False},
            "pageType": "SearchPage",
            "enableAdsUnifiedProductTile": False,
        }

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "x-o-mart": "B2C",
            "x-o-gql-query": "query Search",
            "sec-ch-ua-platform": '"macOS"',
            "x-o-segment": "oaoh",
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
            "x-enable-server-timing": "1",
            "sec-ch-ua-mobile": "?0",
            "x-latency-trace": "1",
            "wm_mp": "true",
            "content-type": "application/json",
            "x-apollo-operation-name": "Search",
            "tenant-id": "gj9b60",
            "downlink": "1.35",
            "x-o-platform": "rweb",
            "x-o-platform-version": "samsus-w-1.2.0-084d7af52c0286cd78797b5b7ed5e7caf28a1714-0909",
            "accept-language": "en-US",
            "x-o-ccm": "server",
            "x-o-bu": "SAMS-US",
            "dpr": "2",
            "wm_page_url": f"https://www.samsclub.com/search?q={quote_plus(query)}",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": f"https://www.samsclub.com/search?q={quote_plus(query)}",
            "priority": "u=1, i",
        }

        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
        }

        r = self._get(
            SEARCH_API_URL,
            params=params,
            headers=headers,
        )

        print(f"← Search response: {r.status_code}")

        data = r.json()

        if data.get("errors"):
            raise Exception(data["errors"])

        search_result = (
            data.get("data", {})
            .get("search", {})
            .get("searchResult", {})
        )

        item_stacks = search_result.get("itemStacks", []) or []
        if not item_stacks:
            print("✗ No item stacks in response")
            return None

        items = item_stacks[0].get("itemsV2", []) or []
        if not items:
            print("✗ No items in search results")
            return None

        print(f"✓ Search returned {len(items)} items")

        # Match: product name must share at least 2 words with the query.
        # Check the first product; if it doesn't match, try the second only.
        def words(text):
            return set(re.findall(r"[a-z0-9]+", text.lower()))

        query_words = words(query)
        min_matches = min(2, len(query_words))

        product = None
        for pos, item in enumerate(items[:2], 1):
            name = item.get("name") or ""
            matched = query_words & words(name)
            print(f"  Checking product {pos}: '{name[:60]}' — matched words: {sorted(matched) if matched else 'none'}")
            if len(matched) >= min_matches:
                product = item
                break

        if not product:
            print("✗ No product matched at least 2 query words")
            return None

        print(f"✓ Matched: {product.get('name', '')[:60]}")

        item_id = product.get("usItemId", "")
        canonical_url = product.get("canonicalUrl", "") or ""

        description = self.get_description(item_id, canonical_url)

        return {
            "name": product.get("name"),
            "price": (
                product.get("priceInfo", {})
                .get("currentPrice", {})
                .get("priceString")
            ),
            "image_url": (
                (product.get("imageInfo") or {}).get("thumbnailUrl")
            ),
            "item_id": item_id,
            "product_url": (
                canonical_url
                if canonical_url.startswith("http")
                else f"https://www.samsclub.com{canonical_url}"
            ),
            "description": description,
        }

    # --------------------------------------------------
    # DESCRIPTION (PDP)
    # --------------------------------------------------
    def get_description(self, item_id, canonical_url=""):
        if not item_id:
            return ""
        last_exc = None
        for attempt in range(1, 4):
            try:
                return self._get_description_once(item_id, canonical_url)
            except Exception as e:
                last_exc = e
                if attempt < 3:
                    wait = 3 * attempt
                    print(f"    ⚠️  Description attempt {attempt}/3 failed ({e.__class__.__name__}: {e}) — retrying in {wait}s...")
                    time.sleep(wait)
        print(f"    ⚠️  Description failed for {item_id}: {last_exc}")
        return ""

    def _get_description_once(self, item_id, canonical_url=""):

        print(f"→ Sending description request (PDP) for item {item_id}...")

        url = PDP_API_URL.format(item_id=item_id)

        page_url_path = canonical_url if canonical_url else f"/ip/{item_id}"
        if page_url_path.startswith("http"):
            page_url_path = urlparse(page_url_path).path

        product_page_url = f"https://www.samsclub.com{page_url_path}"

        variables = {
            "isMobile": False,
            "channel": "WWW",
            "version": "v1",
            "postProcessingVersion": 1,
            "pageType": "ItemPageGlobalDesktop",
            "tenant": "SAMS_GLASS",
            "tempo": {
                "targeting": "%7B%22userState%22%3A%22loggedIn%22%7D",
                "params": [
                    {"key": "expoVars", "value": "expoVariationValue"},
                    {"key": "expoVars", "value": "expoVariationValue2"},
                ],
            },
            "p13nCls": {
                "pageId": item_id,
                "skipPtcFetch": True,
                "p13NCallType": "ATF",
                "userClientInfo": {"isZipLocated": True, "callType": "CLIENT"},
                "userReqInfo": {
                    "refererContext": {
                        "source": "itempage",
                        "query": "",
                        "sourceId": None,
                        "wmlspartner": None,
                        "variantSwitch": True,
                        "itemSwitchContext": {
                            "refererItem": item_id,
                            "sizeReferer": None,
                            "sizeReferers": None,
                        },
                    },
                    "enableSlaBadgeV2": False,
                    "isMoreOptionsTileEnabled": True,
                },
            },
            "iId": item_id,
            "layout": ["itemDesktop2"],
            "p13N": {
                "userClientInfo": {
                    "isZipLocated": True,
                    "deviceType": "desktop",
                    "callType": "CLIENT",
                },
                "userReqInfo": {
                    "refererContext": {
                        "source": "itempage",
                        "sourceId": None,
                        "wmlspartner": None,
                    },
                    "pageUrl": page_url_path,
                },
            },
            "cSId": "",
            "sSId": None,
            "fBBAd": True,
            "eLLBBAds": False,
            "adV1Enabled": False,
            "fMq": True,
            "fGalAd": False,
            "fSCar": True,
            "fDac": False,
            "fBB": True,
            "enableAdsTemplateBadging": False,
            "enableAdsUnifiedProductTile": False,
            "fSL": True,
            "fIdml": True,
            "sIdml": False,
            "fMrkDscrp": False,
            "fRev": True,
            "fFit": True,
            "fSeo": True,
            "fP13": True,
            "fAff": True,
            "spVid": False,
            "spSBA": False,
            "eItIb": True,
            "fIlc": True,
            "bbe": False,
            "fSId": False,
            "eSb": True,
            "eCc": False,
            "eSsm": False,
            "enableRelatedSearch": False,
            "enableTopReasonsToBuy": False,
            "enableDetailedBeacon": False,
            "enableImageClassification": False,
            "enableMultiSave": False,
            "enableBnplMessage": False,
            "enableAOSModuleAttribute": False,
            "enableSizePredictor": False,
            "fRem": False,
            "enablePromoData": False,
            "enablePromotionMessages": True,
            "enableFlowerDelivery": True,
            "enableVariantMigration": False,
            "enableChannelLevelPricing": True,
            "enableSignInToSeePrice": True,
            "eTwc": True,
            "enableSecondaryOffers": False,
            "enableSWC": False,
            "enableReimagineSnapshot": False,
            "isSubscriptionFrequencyListEnabled": False,
            "enableWplusFulfillmentModalOnItemPage": False,
            "enableNutritionFacts": True,
            "enableProSellerHighlight": False,
            "enableProductAttributeEnrichment": False,
            "enableContactLensPurchase": False,
            "isSubscriptionEligible": False,
            "vTOP": {
                "personaId": 0,
                "personaManId": 0,
                "isByomActive": False,
                "isCYOMManActive": True,
                "isCYOMImageReductionEnabled": False,
                "isFollowMeActive": False,
            },
            "sV": False,
            "sVC": False,
            "vFId": None,
            "pAdd": None,
            "sFId": None,
            "sizePredictorInput": None,
            "enableTrueFitSizeChart": False,
            "conditionGroupCode": None,
            "conditionCodes": [],
            "selectedOfferId": None,
            "conditionType": "NEW",
            "enableRxDrugScheduleModal": False,
            "isGEPEnable": False,
            "enableUpstreamErrorCode": True,
            "filterCriteria": {"rating": [], "reviewAttributes": [], "aspectId": None},
            "reviewSummaryAspectsLimit": 6,
            "eA2S": False,
            "attributesCacheKey": "",
            "count": 2,
            "startAt": 1,
            "enableB2BItemConditionPricing": False,
            "enableCarouselStrategy": True,
            "enableOptimisticWeightUpdate": False,
            "enableStreamLinedBadging": False,
            "enableSparky": False,
            "enableItemPageFaq": False,
            "includeVideo": False,
        }

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "x-o-gql-query": "query ItemById",
            "ip-session-traffic-type": "",
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            "traffic-type": "Internal",
            "sec-ch-ua-mobile": "?0",
            "content-type": "application/json",
            "cyomv2enabled": "true",
            "x-apollo-operation-name": "ItemById",
            "downlink": "1.45",
            "accept-language": "en-US",
            "x-o-item-id": item_id,
            "dpr": "1",
            "is-variant-fetch": "true",
            "x-o-mart": "B2C",
            "sec-ch-ua-platform": '"macOS"',
            "x-o-segment": "oaoh",
            "x-enable-server-timing": "1",
            "baggage": "trafficType=customer,deviceType=desktop,renderScope=CSR,webRequestSource=Browser,pageName=itemPage",
            "x-latency-trace": "1",
            "ip-referer": product_page_url,
            "wm_mp": "true",
            "tenant-id": "gj9b60",
            "x-o-platform": "rweb",
            "x-o-platform-version": "samsus-w-1.2.0-a2c3b093a782dd00caa3ae82ef7c9d46744a253a-0805",
            "x-o-ccm": "server",
            "x-o-bu": "SAMS-US",
            "calltype": "CLIENT",
            "wm_page_url": product_page_url,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": product_page_url,
            "priority": "u=1, i",
        }

        # Fresh request (new connection) — reusing the search session gets
        # blocked by PerimeterX with 412
        r = requests.get(
            url,
            params={"variables": json.dumps(variables, separators=(",", ":"))},
            headers=headers,
            impersonate="chrome",
            proxies=self.proxies,
            timeout=30,
        )

        r.raise_for_status()

        print(f"✓ Description response received (status {r.status_code})")

        data = r.json()

        product = data.get("data", {}).get("product", {})
        if not product:
            print(f"✗ No product data in PDP response for {item_id}")
            return ""

        description = product.get("description", "")
        if isinstance(description, dict):
            description = description.get("value", "")
        if not description:
            description = product.get("shortDescription", "") or ""

        if not description:
            print(f"✗ Description field empty for {item_id}")
            return ""

        print(f"✓ Description fetched ({len(description)} chars)")

        description = re.sub(r"<[^>]+>", "", str(description))
        return html.unescape(description).strip()


if __name__ == "__main__":

    proxy = None

    sams = SamsClubSearcher(proxy)

    product = sams.search("Southern Style Chicken Sandwich")

    if product:
        print("=" * 80)
        print("Name        :", product["name"])
        print("Price       :", product["price"])
        print("Item ID     :", product["item_id"])
        print("Product URL :", product["product_url"])
        print("Image URL   :", product["image_url"])
        print()
        print("Description:")
        print(product["description"])
        print("=" * 80)
    else:
        print("No matching product found.")

"""
dealerly/ebay.py
================
eBay Browse API client plus title-based vehicle parsing utilities.

v0.9.3 changes:
  - ebay_search() accepts buyer_postcode + search_radius_miles
  - Sends X-EBAY-C-ENDUSERCTX header for location-biased results
  - Adds itemLocationPostalCode filter for proximity ranking

Responsibilities:
  - OAuth2 app token acquisition
  - Environment resolution (production vs sandbox)
  - Item search and single-item fetch
  - Listing normalisation (eBay payload -> Listing dataclass)
  - Title-based make/model/year/mileage extraction
  - Vehicle key and comps query construction
  - Whole-car filter (rejects parts listings, golf equipment, etc.)

Depends on:
  - dealerly.models   (Listing, VehicleGuess)
  - dealerly.vrm      (is_ulez_compliant, looks_plausible_uk_vrm)

I/O: HTTP requests to api.ebay.com (or api.sandbox.ebay.com).
No DB access.

Resilience: ``ebay_search`` retries on timeout/connection errors (default 3 attempts,
exponential backoff). Env: ``DEALERLY_EBAY_HTTP_TIMEOUT`` (read timeout seconds, default 45),
``DEALERLY_EBAY_SEARCH_RETRIES`` (1-6, default 3). Pipeline ``fetch_ebay_comps`` catches
remaining failures so Phase 4.x scoring continues with cached / fallback comps.
"""
from __future__ import annotations

import base64
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, Timeout

from dealerly.models import Listing, VehicleGuess
from dealerly.vrm import is_ulez_compliant, detect_ulez_from_text


def _ebay_read_timeout_s() -> float:
    """Per-request read timeout (connect uses min(15, this)). Override: DEALERLY_EBAY_HTTP_TIMEOUT."""
    raw = os.environ.get("DEALERLY_EBAY_HTTP_TIMEOUT", "").strip()
    if raw:
        try:
            return max(10.0, min(120.0, float(raw)))
        except ValueError:
            pass
    return 45.0


def _ebay_search_attempts() -> int:
    raw = os.environ.get("DEALERLY_EBAY_SEARCH_RETRIES", "").strip()
    if raw.isdigit():
        return max(1, min(6, int(raw)))
    return 3


def _ebay_timeout_tuple() -> Tuple[float, float]:
    """(connect, read) for requests — shared by search, item, OAuth."""
    read_s = _ebay_read_timeout_s()
    return (min(15.0, read_s), read_s)


def _ebay_request_get(
    url: str,
    *,
    headers: Dict[str, str],
    params: Optional[Dict[str, str]] = None,
) -> requests.Response:
    """GET with retries on timeout / connection errors."""
    timeout = _ebay_timeout_tuple()
    attempts = _ebay_search_attempts()
    for attempt in range(attempts):
        try:
            return requests.get(url, headers=headers, params=params, timeout=timeout)
        except (Timeout, RequestsConnectionError):
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.75 * (2**attempt))
    raise RuntimeError("_ebay_request_get: unreachable")


def _ebay_request_post(url: str, *, headers: Dict[str, str], data: Dict[str, str]) -> requests.Response:
    """POST with retries on timeout / connection errors."""
    timeout = _ebay_timeout_tuple()
    attempts = _ebay_search_attempts()
    for attempt in range(attempts):
        try:
            return requests.post(url, headers=headers, data=data, timeout=timeout)
        except (Timeout, RequestsConnectionError):
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.75 * (2**attempt))
    raise RuntimeError("_ebay_request_post: unreachable")


# ---------------------------------------------------------------------------
# eBay API endpoints
# ---------------------------------------------------------------------------

def _ebay_endpoints(env: str) -> Tuple[str, str, str]:
    """Return (token_url, search_url, item_base_url) for the given env."""
    if env.lower() == "sandbox":
        return (
            "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
            "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search",
            "https://api.sandbox.ebay.com/buy/browse/v1/item/",
        )
    return (
        "https://api.ebay.com/identity/v1/oauth2/token",
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        "https://api.ebay.com/buy/browse/v1/item/",
    )


def _looks_like_sandbox(client_id: str) -> bool:
    cid = client_id.strip().upper()
    return cid.startswith("SBX-") or "-SBX-" in cid or "SBX" in cid


def resolve_ebay_env(env: str, client_id: str) -> str:
    """Auto-detect sandbox from client_id prefix; otherwise honour env string."""
    return "sandbox" if _looks_like_sandbox(client_id) else env.strip().lower()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def ebay_app_token(*, env: str, client_id: str, client_secret: str) -> str:
    """
    Fetch a client-credentials OAuth2 token from eBay.
    Raises requests.HTTPError on failure.
    """
    token_url, _, _ = _ebay_endpoints(env)
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    r = _ebay_request_post(
        token_url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def ebay_search(
    *,
    env: str,
    token: str,
    price_min: int,
    price_max: int,
    limit: int,
    offset: int,
    keywords: str,
    category_ids: Optional[str],
    buying_filter: str,
    sort: str,
    buyer_postcode: str = "",
    search_radius_miles: int = 0,
) -> Dict[str, Any]:
    """
    Call the eBay Browse API item_summary/search endpoint.
    Returns the raw JSON response dict.
    Raises requests.HTTPError on non-2xx responses.

    v0.9.3: If buyer_postcode is set, sends X-EBAY-C-ENDUSERCTX header
    to bias search results toward the buyer's location. The eBay Browse API
    uses this for relevance ranking — items closer to the postcode rank higher.
    search_radius_miles is informational for logging; eBay's Browse API does
    not support hard distance filtering, so this biases rather than hard-filters.
    """
    _, browse_url, _ = _ebay_endpoints(env)
    filters = [
        f"price:[{price_min}..{price_max}]",
        "priceCurrency:GBP",
        "itemLocationCountry:GB",
    ]
    if buying_filter:
        filters.append(buying_filter)

    params: Dict[str, str] = {
        "q": keywords,
        "limit": str(limit),
        "offset": str(offset),
        "filter": ",".join(filters),
        "sort": sort,
    }
    if category_ids:
        params["category_ids"] = str(category_ids)

    headers: Dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        "Accept": "application/json",
    }

    # v0.9.3: Location bias via contextual location header
    if buyer_postcode:
        headers["X-EBAY-C-ENDUSERCTX"] = (
            f"contextualLocation=country=GB,zip={buyer_postcode}"
        )

    r = _ebay_request_get(browse_url, headers=headers, params=params)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Single item fetch
# ---------------------------------------------------------------------------

def ebay_get_item(
    *, env: str, token: str, item_id: str
) -> Optional[Dict[str, Any]]:
    """
    Fetch full item details from the eBay Browse API.
    Tries with DESCRIPTION fieldgroup first, falls back to bare item endpoint.
    Returns None for 404 (item not found or ended).
    """
    _, _, base_url = _ebay_endpoints(env)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        "Accept": "application/json",
    }
    for url in (
        f"{base_url}{item_id}?fieldgroups=DESCRIPTION",
        f"{base_url}{item_id}",
    ):
        try:
            r = _ebay_request_get(url, headers=headers)
        except (Timeout, RequestsConnectionError):
            continue
        if r.status_code == 404:
            return None
        if r.status_code in (400, 403):
            continue
        try:
            r.raise_for_status()
        except HTTPError:
            continue
        return r.json()
    return None


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

def _price_gbp(item: Dict[str, Any]) -> Optional[float]:
    """Extract GBP price from an eBay item summary dict."""
    cand = item.get("currentBidPrice") or item.get("price") or {}
    if (cand.get("currency") or "GBP").upper() not in ("GBP", ""):
        return None
    try:
        return float(cand.get("value"))
    except (TypeError, ValueError):
        return None


def _fuel_from_item(item: Dict[str, Any]) -> str:
    """
    Extract fuel type from item specifics (preferred) or title keyword fallback.
    Returns a lowercase string e.g. "diesel", "petrol", or "" if unknown.
    """
    for spec in item.get("itemSpecifics") or item.get("localizedAspects") or []:
        if not isinstance(spec, dict):
            continue
        if "fuel" not in str(spec.get("name") or "").lower():
            continue
        vals = spec.get("values") or spec.get("value") or []
        if isinstance(vals, str):
            vals = [vals]
        if vals:
            return str(vals[0]).lower()

    # Fallback: title keyword scan
    title = str(item.get("title", "")).lower()
    for fuel in ("diesel", "petrol", "hybrid", "electric", "phev"):
        if fuel in title:
            return fuel
    return ""


def mileage_from_item(item: Dict[str, Any]) -> Optional[int]:
    """
    Extract mileage from item specifics (preferred) or title fallback.

    Checks common eBay field names: "Mileage", "Odometer Reading",
    "Miles", "Vehicle Mileage". Handles values like "80,000",
    "80000 miles", "80k".

    Returns None if not found or outside plausible range.
    """
    for spec in item.get("itemSpecifics") or item.get("localizedAspects") or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or spec.get("localizedName") or "").lower()
        if not any(kw in name for kw in ("mileage", "miles", "odometer")):
            continue
        vals = spec.get("values") or spec.get("value") or []
        if isinstance(vals, str):
            vals = [vals]
        for val in vals:
            v = str(val).lower().replace(",", "").replace(" ", "")
            # Handle "80000miles", "80000", "80k"
            m = re.search(r"(\d{2,6})", v)
            if m:
                miles = int(m.group(1))
                # Handle "80" meaning "80k"
                if "k" in v and miles < 1000:
                    miles *= 1000
                if 1_000 <= miles <= 400_000:
                    return miles

    # Fallback: title
    return parse_mileage_from_title(str(item.get("title", "")))


def year_from_item(item: Dict[str, Any]) -> Optional[int]:
    """
    Extract year from item specifics (preferred) or title fallback.

    Checks: "Year", "Year of Manufacture", "Registration Year".
    """
    for spec in item.get("itemSpecifics") or item.get("localizedAspects") or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or spec.get("localizedName") or "").lower()
        if "year" not in name:
            continue
        vals = spec.get("values") or spec.get("value") or []
        if isinstance(vals, str):
            vals = [vals]
        for val in vals:
            m = re.search(r"(19|20)\d{2}", str(val))
            if m:
                y = int(m.group(0))
                from datetime import datetime
                if 1985 <= y <= datetime.now().year:
                    return y

    # Fallback: title
    return parse_year_from_title(str(item.get("title", "")))


def collect_item_specific_text(item: Dict[str, Any]) -> str:
    """
    Flatten eBay itemSpecifics / localizedAspects into a single pipe-delimited
    string for use in prompts or text scanning.
    Truncated at 12,000 characters.
    """
    bits: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (str, int, float)):
                    bits.append(f"{k}: {v}")
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(item.get("itemSpecifics") or item.get("localizedAspects") or [])
    return " | ".join(bits)[:12_000]


def upgrade_ebay_image_url(url: str) -> str:
    """
    Prefer eBay CDN large picture size (s-l1600) for report thumbnails and ANPR.
    Search results often return s-l225/s-l300; upgrading improves clarity.
    """
    if not url or "ebayimg" not in url.lower() or "s-l" not in url.lower():
        return url
    try:
        return re.sub(
            r"/s-l\d+(\.(?:jpg|jpeg|png|webp))(?=\?|$)",
            r"/s-l1600\1",
            url,
            count=1,
            flags=re.IGNORECASE,
        )
    except Exception:
        return url


def collect_image_urls(
    item: Dict[str, Any],
    limit: int = 4,
    rank_fn=None,
) -> List[str]:
    """
    Return up to limit unique image URLs from an eBay item payload.

    If rank_fn is provided (e.g. rank_images_for_anpr from vision.py), all
    collected URLs are passed through it before the limit is applied, so the
    most plate-likely images are returned first.
    """
    urls: List[str] = []

    def _add(u: Any) -> None:
        if isinstance(u, str) and u.startswith("http") and u not in urls:
            urls.append(u)

    _add((item.get("image") or {}).get("imageUrl"))
    for img in item.get("additionalImages") or []:
        _add(img.get("imageUrl") if isinstance(img, dict) else img)
    for key in ("thumbnailImages", "pictures", "images"):
        for img in item.get(key) or []:
            _add(img.get("imageUrl") or img.get("url") if isinstance(img, dict) else img)

    urls = [upgrade_ebay_image_url(u) for u in urls]

    if rank_fn is not None:
        urls = rank_fn(urls)

    return urls[:limit]


_DISPLAY_DEPRIORITISE: tuple = (
    "interior", "dashboard", "engine", "wheel", "seat", "boot", "cabin",
    "document", "servicebook", "manual", "receipt", "invoice", "odometer", "speedo",
)
_DISPLAY_PRIORITISE: tuple = (
    "front", "angle", "hero", "main", "exterior", "outside", "side", "rear",
)


def rank_images_for_display(urls: List[str]) -> List[str]:
    """
    Reorder image URLs for better report thumbnails.

    Prefers clear exterior shots (front/angle/side/rear) and de-prioritises
    interior/docs/engine closeups.
    """
    if not urls:
        return urls

    def _score(url: str, idx: int) -> float:
        score = 0.5
        u = (url or "").lower()
        if idx == 0:
            score += 0.35
        if any(k in u for k in _DISPLAY_PRIORITISE):
            score += 0.3
        if any(k in u for k in _DISPLAY_DEPRIORITISE):
            score -= 0.7
        if any(k in u for k in ("1200", "1600", "1920", "2048", "original", "s-l1600", "s-l960")):
            score += 0.18
        return score

    scored = [(u, _score(u, i)) for i, u in enumerate(urls)]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [u for u, _s in scored]


# ---------------------------------------------------------------------------
# Listing normalisation
# ---------------------------------------------------------------------------

def normalise_ebay_items(payload: Dict[str, Any]) -> List[Listing]:
    """
    Convert an eBay search response payload into a list of Listing objects.
    Skips items with no parseable GBP price.
    """
    out: List[Listing] = []
    for item in payload.get("itemSummaries") or []:
        price = _price_gbp(item)
        if price is None:
            continue
        fuel  = _fuel_from_item(item)
        title = str(item.get("title", ""))
        guess = guess_make_model(title)
        # Capture hero image from search result so ALL listings get a thumbnail,
        # not just Phase-3-enriched ones. eBay search returns image.imageUrl or
        # thumbnailImages[0].imageUrl on each itemSummary.
        image_obj = item.get("image") or {}
        thumb_list = item.get("thumbnailImages") or []
        first_img = upgrade_ebay_image_url(
            str(image_obj.get("imageUrl", ""))
            or (str(thumb_list[0].get("imageUrl", "")) if thumb_list else "")
        )
        # Sprint 15: detect auction format from eBay buyingOptions
        buying_opts = item.get("buyingOptions") or []
        is_auction = "AUCTION" in buying_opts and "FIXED_PRICE" not in buying_opts
        # Sprint 15: year+fuel ULEZ, overridden by title text if seller states it
        _ulez = is_ulez_compliant(guess.year, fuel)
        _text_ulez = detect_ulez_from_text(title)
        if _text_ulez is not None:
            _ulez = _text_ulez
        out.append(Listing(
            platform="ebay",
            item_id=str(item.get("itemId", "")),
            title=title,
            price_gbp=float(price),
            url=str(item.get("itemWebUrl", "")),
            location=str((item.get("itemLocation") or {}).get("postalCode", "")),
            condition=str(item.get("condition") or ""),
            vrm="",
            raw=item,
            fuel_type=fuel,
            year=guess.year,
            mileage=mileage_from_item(item) or guess.mileage,
            ulez_compliant=_ulez,
            first_image_url=first_img,
            is_auction=is_auction,
        ))
    return out


def hard_price_filter(
    listings: List[Listing], lo: float, hi: float
) -> List[Listing]:
    """Remove listings whose price falls outside [lo, hi]."""
    return [
        l for l in listings
        if l.price_gbp is not None and lo <= float(l.price_gbp) <= hi
    ]


def merge_dedupe(lists: List[List[Listing]]) -> List[Listing]:
    """Merge multiple listing lists, deduplicating by item_id."""
    seen: Dict[str, Listing] = {}
    for lst in lists:
        for listing in lst:
            if listing.item_id and listing.item_id not in seen:
                seen[listing.item_id] = listing
    return list(seen.values())


# ---------------------------------------------------------------------------
# Vehicle title parsing
# ---------------------------------------------------------------------------

MAKE_ALIASES: Dict[str, List[str]] = {
    "vauxhall":   ["vauxhall", "vx"],
    "volkswagen": ["volkswagen", "vw"],
    "ford":       ["ford"],
    "honda":      ["honda"],
    "toyota":     ["toyota"],
    "skoda":      ["skoda", "scoda"],
    "seat":       ["seat"],
    "audi":       ["audi"],
    "bmw":        ["bmw"],
    "mercedes":   ["mercedes", "merc", "mercedes-benz"],
    "hyundai":    ["hyundai"],
    "kia":        ["kia"],
    "nissan":     ["nissan"],
    "mazda":      ["mazda"],
    "renault":    ["renault"],
    "peugeot":    ["peugeot"],
    "citroen":    ["citroen", "citroën"],
    "fiat":       ["fiat"],
    "mini":       ["mini"],
    "dacia":      ["dacia"],
    "mitsubishi": ["mitsubishi"],
    "subaru":     ["subaru"],
    "suzuki":     ["suzuki"],
    "volvo":      ["volvo"],
    "lexus":      ["lexus"],
    "jeep":       ["jeep"],
    "land rover": ["land rover", "landrover", "freelander", "discovery"],
    "jaguar":     ["jaguar"],
    "alfa romeo": ["alfa romeo", "alfa"],
}

MODEL_TOKENS: List[str] = [
    "jazz", "civic", "crv", "cr-v", "hrv", "hr-v", "accord",
    "yaris", "auris", "corolla", "aygo", "iq", "prius",
    "fabia", "octavia", "superb", "ibiza", "leon", "arona",
    "fiesta", "focus", "mondeo", "kuga", "ecosport", "puma", "galaxy", "c-max",
    "corsa", "astra", "insignia", "mokka", "zafira", "meriva", "crossland",
    "golf", "polo", "passat", "tiguan", "up",
    "i10", "i20", "i30", "tucson", "ioniq",
    "picanto", "ceed", "sportage", "stonic",
    "micra", "juke", "qashqai", "leaf",
    "mx5", "mazda3", "mazda6",
    "clio", "megane", "zoe", "208", "308", "3008", "c3", "c4",
    "500", "punto", "sandero", "duster",
    "swift", "baleno", "ignis",
]

# Strings in titles that indicate the listing is NOT a car
GOLF_SPORT_EXCLUDES: List[str] = [
    "taylormade", "titleist", "motocaddy", "putter", "iron set", "iron", "pw",
    "golf trolley", "gps remote", "cart bag", "right handed", "stiff steel",
]

PARTS_EXCLUDES: List[str] = [
    "breaking", "for breaking", "for parts", "engine only", "gearbox only",
    "ecu", "mechatronics", "turbo only", "injector", "alternator", "starter motor",
    "clutch kit", "catalytic", "particulate filter", "head unit",
    "wheels only", "alloys only", "tyres only", "wiring loom", "swap", "engine swap",
    "stripping", "stripped",
]


def parse_year_from_title(title: str) -> Optional[int]:
    """Extract a 4-digit year from a listing title. Returns None if not found."""
    m = re.search(r"\b(19|20)\d{2}\b", title or "")
    if m:
        y = int(m.group(0))
        if 1985 <= y <= datetime.now().year:
            return y
    return None


def parse_mileage_from_title(title: str) -> Optional[int]:
    """
    Extract mileage from a listing title.
    Handles "80000 miles", "80,000 miles", and "80k" shorthand.
    Returns None if not found or outside a plausible range.
    """
    t = (title or "").lower().replace(",", "")
    m = re.search(r"\b(\d{2,6})\s*miles\b", t)
    if m:
        miles = int(m.group(1))
        if 1_000 <= miles <= 400_000:
            return miles
    m2 = re.search(r"\b(\d{2,3})\s*k\b", t)
    if m2:
        miles = int(m2.group(1)) * 1_000
        if 10_000 <= miles <= 400_000:
            return miles
    return None


def guess_make_model(title: str) -> VehicleGuess:
    """
    Best-effort make/model/year/mileage/fuel extraction from a listing title.
    Returns VehicleGuess with make="unknown" / model="unknown" on failure.
    """
    t = (title or "").lower()

    make = "unknown"
    for mk, aliases in MAKE_ALIASES.items():
        if any(re.search(rf"\b{re.escape(a)}\b", t) for a in aliases):
            make = mk
            break

    model = "unknown"
    for token in sorted(MODEL_TOKENS, key=len, reverse=True):
        if token in t:
            model = token.replace(" ", "")
            break

    fuel = next(
        (f for f in ("diesel", "petrol", "hybrid", "electric", "phev") if f in t),
        "",
    )
    return VehicleGuess(
        make=make,
        model=model,
        year=parse_year_from_title(title),
        mileage=parse_mileage_from_title(title),
        fuel_type=fuel,
    )


def has_car_signal_words(title: str) -> bool:
    """True if the title contains words strongly associated with whole-car listings."""
    t = (title or "").lower()
    return any(s in t for s in (
        "mot", "service", "history", "ulez", "hatchback", "saloon", "estate",
        "petrol", "diesel", "manual", "automatic", "drives", "starts", "v5",
        "logbook", "owners", "mileage", "miles", "registered",
    )) or bool(re.search(r"\b(19|20)\d{2}\b", t))


def is_strict_whole_car(title: str) -> bool:
    """
    Return True only if the title looks like a whole-car listing.
    Rejects: golf equipment, parts listings, and ambiguous VW Golf/Polo tokens
    without a brand qualifier.
    """
    t = (title or "").lower()
    if any(x in t for x in GOLF_SPORT_EXCLUDES):
        return False
    if any(x in t for x in PARTS_EXCLUDES):
        return False
    g = guess_make_model(title)
    if g.model in ("golf", "polo") and not re.search(r"\b(vw|volkswagen)\b", t):
        return False
    if g.make == "unknown" and not has_car_signal_words(title):
        return False
    return True


# ---------------------------------------------------------------------------
# Vehicle key / comps query construction
# ---------------------------------------------------------------------------

def year_band(year: Optional[int]) -> str:
    """
    Map a year to a 3-year band string e.g. 2018 -> "2018-2020".
    Used as part of the vehicle_key for comps lookups.
    """
    if not year:
        return "y?"
    base = int(year // 3) * 3
    return f"{base}-{base + 2}"


def vehicle_key_from_title(title: str) -> str:
    """Build a "make|model|yband|fuel" key from a listing title."""
    g = guess_make_model(title)
    fuel = g.fuel_type or "unknown"
    return f"{g.make}|{g.model}|{year_band(g.year)}|{fuel}"


def comps_query_from_key(key: str) -> Optional[str]:
    """
    Build an eBay search query string from a vehicle key.
    Returns None if make or model is unknown.
    Includes fuel type in the query for more targeted comps.
    """
    parts = key.split("|")
    if len(parts) < 3:
        return None
    make, model = parts[0], parts[1]
    yband = parts[2]
    fuel = parts[3] if len(parts) > 3 else ""

    if make == "unknown" or model == "unknown":
        return None

    year_hint = ""
    if "-" in yband and yband[:4].isdigit():
        year_hint = str(int(yband[:4]) + 1)

    # Include fuel type in query for better targeting
    fuel_hint = ""
    if fuel and fuel != "unknown":
        fuel_hint = fuel

    if model in ("golf", "polo") and make == "volkswagen":
        return f"vw {model} {fuel_hint} {year_hint}".strip()
    return f"{make} {model} {fuel_hint} {year_hint}".strip()

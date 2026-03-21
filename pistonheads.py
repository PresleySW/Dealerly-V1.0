"""
dealerly/pistonheads.py
=======================
Sprint 15: PistonHeads UK classifieds ingestion adapter.

PistonHeads (pistonheads.com) is a major UK car sales platform with a large
classifieds section used by both private sellers and dealers. Particularly
strong in performance/enthusiast vehicles but covers the full used-car market.

Strategy (three-tier, matching Motors adapter pattern):
  1. __NEXT_DATA__ JSON embedded in page (preferred — structured data, fast)
  2. JSON-LD / application/json script blocks
  3. BeautifulSoup HTML parse of listing cards (fallback)

Search URL:
  https://www.pistonheads.com/classifieds?type=used-cars
      &search={make}+{model}&priceFrom={min}&priceTo={max}&page={page}

Auction detection:
  PistonHeads has both regular "advertised price" listings and timed auction
  lots. Listings with `listingType == "auction"` or a `saleType` containing
  "auction" are flagged as `is_auction=True`.

Rate limiting:
  1.5 s sleep between page requests (polite crawl; no captcha or WAF).

Depends on:
  - dealerly.ingestion   (BaseIngestionAdapter)
  - dealerly.models      (Listing)
  - dealerly.ebay        (guess_make_model, merge_dedupe)
  - dealerly.vrm         (is_ulez_compliant, detect_ulez_from_text,
                          SAFE_VRM_PATTERNS, _scan_patterns,
                          looks_plausible_uk_vrm, normalise_vrm)
  - dealerly.config      (USER_AGENT)
  - requests, beautifulsoup4 (optional)
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

from dealerly.ingestion import BaseIngestionAdapter
from dealerly.models import Listing
from dealerly.ebay import guess_make_model, merge_dedupe
from dealerly.vrm import (
    SAFE_VRM_PATTERNS,
    _scan_patterns,
    is_ulez_compliant,
    detect_ulez_from_text,
    looks_plausible_uk_vrm,
    normalise_vrm,
)
from dealerly.config import USER_AGENT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PH_BASE = "https://www.pistonheads.com"
_PH_SEARCH = _PH_BASE + "/classifieds"
_SLEEP_S: float = 1.5
_MAX_PAGES: int = 3
_MAX_LOW_SIGNAL_PAGES: int = 2   # stop early if repeated empty pages

def _random_headers() -> dict:
    """Browser-like headers with a realistic User-Agent to reduce Cloudflare blocks."""
    import random as _rnd
    _UAS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ]
    return {
        "User-Agent": _rnd.choice(_UAS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.pistonheads.com/classifieds",
        "DNT": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }

# Keep static reference for backward compat (unused internally now)
_HEADERS = _random_headers()


# ---------------------------------------------------------------------------
# Main adapter class
# ---------------------------------------------------------------------------

class PistonHeadsAdapter(BaseIngestionAdapter):
    """
    Ingestion adapter for PistonHeads UK classifieds.

    Inherits BaseIngestionAdapter so it fits seamlessly into the Phase 1
    adapter chain alongside EbayIngestionAdapter, MotorsAdapter, etc.
    """

    def platform_name(self) -> str:
        return "pistonheads"

    @property
    def is_available(self) -> bool:
        return True  # public site, no auth required

    def fetch_listings(
        self,
        queries: list[str],
        price_min: int,
        price_max: int,
        pages: int,
        **kwargs,
    ) -> List[Listing]:
        """
        Fetch PistonHeads listings for each query term.

        Returns deduplicated List[Listing].
        """
        all_batches: List[List[Listing]] = []
        for term in queries:
            try:
                batch = _fetch_query(
                    term,
                    price_min=price_min,
                    price_max=price_max,
                    max_pages=min(pages, _MAX_PAGES),
                )
                all_batches.append(batch)
            except Exception as exc:
                print(f"  [pistonheads] Query '{term}' error: {exc}")
                all_batches.append([])

        return merge_dedupe(all_batches)


# ---------------------------------------------------------------------------
# Fetch + parse helpers
# ---------------------------------------------------------------------------

def _fetch_query(
    query: str,
    *,
    price_min: int,
    price_max: int,
    max_pages: int,
) -> List[Listing]:
    """Fetch all result pages for one search query."""
    parts = query.strip().split(None, 1)
    make  = parts[0] if parts else query
    model = parts[1] if len(parts) > 1 else ""

    results: List[Listing] = []
    low_signal_count = 0

    for page in range(1, max_pages + 1):
        params: Dict[str, Any] = {
            "type":      "used-cars",
            "priceFrom": str(price_min),
            "priceTo":   str(price_max),
            "page":      str(page),
        }
        if make:
            params["make"] = make.capitalize()
        if model:
            params["model"] = model.capitalize()
        # If no make/model parsed, fall back to free-text search
        if not make and not model:
            params["search"] = query

        try:
            resp = requests.get(
                _PH_SEARCH,
                params=params,
                headers=_random_headers(),
                timeout=20,
                allow_redirects=True,
            )
        except Exception as exc:
            print(f"  [pistonheads] HTTP error for '{query}' page {page}: {exc}")
            break

        if resp.status_code != 200:
            if resp.status_code in (403, 429, 503):
                print(
                    f"  [pistonheads] Blocked ({resp.status_code}) for '{query}'. "
                    "Cloudflare or rate-limited — try again later or use Playwright fallback."
                )
                break
            print(f"  [pistonheads] HTTP {resp.status_code} for '{query}' page {page}")
            break

        # Detect Cloudflare challenge page (200 but JS challenge body)
        _body_low = resp.text[:2000].lower()
        if "challenge-platform" in _body_low or "cf-browser-verification" in _body_low:
            print(
                f"  [pistonheads] Cloudflare JS challenge detected for '{query}'. "
                "Headless browser needed — will return 0 listings for this query."
            )
            break

        page_listings = _parse_page(resp.text)
        if not page_listings:
            low_signal_count += 1
            if low_signal_count >= _MAX_LOW_SIGNAL_PAGES:
                break
        else:
            low_signal_count = 0
            results.extend(page_listings)

        if page < max_pages:
            import random as _rnd
            time.sleep(_SLEEP_S + _rnd.uniform(0.5, 1.5))

    return results


def _parse_page(html: str) -> List[Listing]:
    """
    Try to extract listings from a PistonHeads search result page.

    Tier 1: __NEXT_DATA__ JSON (structured, preferred)
    Tier 2: application/json script blocks (alternative SSR pattern)
    Tier 3: BeautifulSoup HTML parse (fallback)
    """
    listings = _parse_next_data(html)
    if listings:
        return listings

    listings = _parse_json_scripts(html)
    if listings:
        return listings

    if _BS4_AVAILABLE:
        return _parse_bs4(html)

    return []


def _parse_next_data(html: str) -> List[Listing]:
    """Extract listings from __NEXT_DATA__ JSON embedded in page."""
    m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []

    # PistonHeads pages: props.pageProps.listings or props.pageProps.classifieds
    page_props = (
        data.get("props", {})
            .get("pageProps", {})
    )
    candidates: List[Dict[str, Any]] = []
    for key in ("listings", "classifieds", "results", "data", "vehicles"):
        val = page_props.get(key)
        if isinstance(val, list) and val:
            candidates = val
            break
        # sometimes nested under initialData or dehydratedState
        if isinstance(val, dict):
            for k2 in ("listings", "classifieds", "results"):
                v2 = val.get(k2)
                if isinstance(v2, list) and v2:
                    candidates = v2
                    break

    if not candidates:
        # Try dehydratedState -> queries -> state -> data -> pages -> flatMap
        try:
            queries = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("dehydratedState", {})
                    .get("queries", [])
            )
            for q in queries:
                pages = q.get("state", {}).get("data", {}).get("pages", [])
                for p in pages:
                    items = p.get("listings") or p.get("data") or p.get("results") or []
                    if isinstance(items, list):
                        candidates.extend(items)
        except Exception:
            pass

    return [_item_to_listing(item) for item in candidates if isinstance(item, dict)]


def _parse_json_scripts(html: str) -> List[Listing]:
    """Try any application/json script blocks for listing arrays."""
    results: List[Listing] = []
    for m in re.finditer(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and _looks_like_listing(item):
                    results.append(_item_to_listing(item))
        elif isinstance(data, dict):
            for key in ("listings", "classifieds", "results", "vehicles", "data"):
                val = data.get(key)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict) and _looks_like_listing(item):
                            results.append(_item_to_listing(item))
                    break
    return results


def _looks_like_listing(item: Dict[str, Any]) -> bool:
    """Heuristic: does this dict look like a car listing?"""
    keys = set(item.keys())
    return bool(
        keys & {"price", "make", "model", "year", "title", "url", "id", "listingId"}
    )


def _parse_bs4(html: str) -> List[Listing]:
    """BeautifulSoup fallback — parse listing card elements from HTML."""
    if not _BS4_AVAILABLE:
        return []
    soup = BeautifulSoup(html, "html.parser")
    listings: List[Listing] = []

    # PistonHeads uses article or li elements with data attributes or class patterns
    cards = (
        soup.find_all("article", class_=re.compile(r"classified|listing|vehicle", re.I))
        or soup.find_all("li", class_=re.compile(r"classified|listing|vehicle", re.I))
        or soup.find_all("div", class_=re.compile(r"classified-item|listing-item|car-item", re.I))
    )

    for card in cards:
        try:
            listing = _card_to_listing(card)
            if listing:
                listings.append(listing)
        except Exception:
            continue

    return listings


def _card_to_listing(card) -> Optional[Listing]:
    """Parse a single BeautifulSoup card element into a Listing."""
    # Title
    title_el = (
        card.find("h2")
        or card.find("h3")
        or card.find(class_=re.compile(r"title|heading", re.I))
    )
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None

    # Price
    price_el = card.find(class_=re.compile(r"price", re.I))
    price_text = price_el.get_text(strip=True) if price_el else ""
    price = _parse_price(price_text)
    if not price:
        return None

    # URL
    link_el = card.find("a", href=True)
    url = ""
    if link_el:
        href = link_el["href"]
        url = href if href.startswith("http") else _PH_BASE + href

    # Image
    img_el = card.find("img")
    img_url = ""
    if img_el:
        img_url = (
            img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src") or ""
        )

    # Mileage from card text
    card_text = card.get_text(" ", strip=True)
    guess = guess_make_model(title)

    item_id = f"ph_{abs(hash(url or title)):x}"
    ulez = is_ulez_compliant(guess.year, guess.fuel_type)
    text_ulez = detect_ulez_from_text(card_text)
    if text_ulez is not None:
        ulez = text_ulez

    return Listing(
        platform="pistonheads",
        item_id=item_id,
        title=title,
        price_gbp=price,
        url=url,
        location="",
        condition="Used",
        vrm="",
        raw={"raw_html_text": card_text[:500]},
        fuel_type=guess.fuel_type,
        year=guess.year,
        mileage=guess.mileage,
        ulez_compliant=ulez,
        first_image_url=img_url,
    )


def _item_to_listing(item: Dict[str, Any]) -> Listing:
    """Convert a JSON listing dict to a Listing object."""
    title = (
        item.get("title")
        or f"{item.get('year', '')} {item.get('make', '')} {item.get('model', '')}".strip()
        or "Unknown"
    )
    price = (
        _parse_price(str(item.get("price") or item.get("advertPrice") or item.get("priceGbp") or ""))
        or 0.0
    )

    url = item.get("url") or item.get("listingUrl") or item.get("link") or ""
    if url and not url.startswith("http"):
        url = _PH_BASE + url

    slug_id = (
        item.get("id") or item.get("listingId") or item.get("adId")
        or abs(hash(url or title))
    )
    item_id = f"ph_{slug_id}"

    img_url = (
        item.get("imageUrl") or item.get("image") or item.get("thumbnailUrl")
        or item.get("mainImage") or ""
    )
    if isinstance(img_url, dict):
        img_url = img_url.get("src") or img_url.get("url") or ""

    location = str(item.get("location") or item.get("town") or item.get("county") or "")
    fuel = str(item.get("fuel") or item.get("fuelType") or "").strip()

    year_raw = item.get("year") or item.get("modelYear") or item.get("vehicleYear")
    try:
        year = int(str(year_raw).strip()[:4]) if year_raw else None
    except (ValueError, TypeError):
        year = None

    mileage_raw = item.get("mileage") or item.get("odometer") or item.get("miles")
    try:
        mileage = int(re.sub(r"[^\d]", "", str(mileage_raw))) if mileage_raw else None
    except (ValueError, TypeError):
        mileage = None

    guess = guess_make_model(title)
    if not year:
        year = guess.year
    if not mileage:
        mileage = guess.mileage
    if not fuel:
        fuel = guess.fuel_type

    ulez = is_ulez_compliant(year, fuel)
    text_blob = title + " " + str(item.get("description") or "")
    text_ulez = detect_ulez_from_text(text_blob)
    if text_ulez is not None:
        ulez = text_ulez

    # Auction detection
    sale_type = str(
        item.get("saleType") or item.get("listingType") or item.get("type") or ""
    ).lower()
    is_auction = "auction" in sale_type

    return Listing(
        platform="pistonheads",
        item_id=item_id,
        title=title,
        price_gbp=float(price),
        url=url,
        location=location,
        condition=str(item.get("condition") or "Used"),
        vrm="",
        raw=item,
        fuel_type=fuel,
        year=year,
        mileage=mileage,
        ulez_compliant=ulez,
        first_image_url=str(img_url),
        is_auction=is_auction,
    )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

_PRICE_PATTERN = re.compile(r"[\d,]+")


def _parse_price(text: str) -> Optional[float]:
    """Extract a numeric price from a string like '£4,995' or '4995'."""
    text = re.sub(r"[^\d,.]", "", str(text or ""))
    m = _PRICE_PATTERN.search(text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None

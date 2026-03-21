"""
dealerly/motors.py
==================
Sprint 3: Motors.co.uk scraping adapter.

Uses requests + BeautifulSoup / JSON extraction (no Playwright required —
Motors.co.uk is a public site that does not require authentication).

Strategy:
  1. Try __NEXT_DATA__ JSON embedded in the page for structured listing data.
     This is the preferred path as it gives clean, structured records.
  2. Fall back to regex / BeautifulSoup HTML parsing if __NEXT_DATA__ is
     absent or the expected keys are missing.

Search URL template:
    https://www.motors.co.uk/search/?make={make}&model={model}
        &price-from={min}&price-to={max}&page={page}

Query parsing:
    Splits a query string by first whitespace token as make, remainder as
    model.  E.g. "honda jazz" -> make=honda, model=jazz.

Rate limiting:
    2 s sleep between page requests.

Output:
    List[Listing] with platform="motors", identical in format to eBay
    pipeline output so downstream phases are platform-agnostic.

Depends on:
  - dealerly.ingestion   (BaseIngestionAdapter)
  - dealerly.models      (Listing)
  - dealerly.ebay        (guess_make_model, merge_dedupe)
  - dealerly.vrm         (SAFE_VRM_PATTERNS, _scan_patterns,
                          is_ulez_compliant, looks_plausible_uk_vrm,
                          normalise_vrm)
  - dealerly.config      (USER_AGENT)
  - requests (stdlib-adjacent, assumed present)
  - beautifulsoup4 (optional, falls back to regex if absent)
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests

# Optional Playwright import — graceful degradation (same pattern as facebook.py)
try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

# Optional BeautifulSoup import
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
    looks_plausible_uk_vrm,
    normalise_vrm,
)
from dealerly.config import USER_AGENT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MOTORS_SEARCH_URL = (
    "https://www.motors.co.uk/search/car/"
    "?make={make}&model={model}&price_from={price_min}&price_to={price_max}&page={page}"
)

# Fallback URL format tried when the primary URL returns empty/blocked results.
# Motors.co.uk has used both /search/ and /car-search/ at various points.
_MOTORS_SEARCH_URL_ALT = (
    "https://www.motors.co.uk/search/"
    "?make={make}&model={model}&price-from={price_min}&price-to={price_max}&page={page}"
)

_MOTORS_SEARCH_URL_ALT2 = (
    "https://www.motors.co.uk/car-search/"
    "?make={make}&model={model}&price-from={price_min}&price-to={price_max}&page={page}"
)
_MOTORS_SEARCH_URL_ALT3 = (
    "https://www.motors.co.uk/used-cars/"
    "?make={make}&model={model}&price-from={price_min}&price-to={price_max}&page={page}"
)
_MOTORS_SEARCH_URL_ALT4 = (
    "https://www.motors.co.uk/{make}/{model}/used-cars/"
    "?price-from={price_min}&price-to={price_max}&page={page}"
)
_MOTORS_SEARCH_URL_ALT5 = (
    "https://www.motors.co.uk/{make}/{model}/used-cars/"
    "?price_from={price_min}&price_to={price_max}&page={page}"
)

# Seconds to sleep between page requests
_PAGE_SLEEP_S: float = 0.6

# Request timeout
_REQUEST_TIMEOUT_S: int = 20

# Per-query guardrails to keep runtime predictable.
_QUERY_TIME_BUDGET_S: float = 35.0
# Sprint 12: raised from 2 → 3 so one empty page doesn't abort pagination early.
_MAX_LOW_SIGNAL_PAGES: int = 3
_PLAYWRIGHT_QUERY_BUDGET_S: float = 28.0

# HTTP headers that avoid most bot-detection on Motors.co.uk.
# Use realistic browser UAs — the generic Dealerly UA triggers Cloudflare.
# Pool of recent Chrome UAs across OS/version combinations; one is picked
# randomly per session to avoid repeated-UA fingerprinting.
_UA_POOL: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.129 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

_WIN_EVENT_LOOP_POLICY_SET = False


def _parse_mileage_from_text(text: str) -> Optional[int]:
    """
    Extract mileage from free text (Sprint 8 — mileage reliability).

    Handles:
      "45,000 miles" / "45000 miles" / "45000mi"    (standard)
      "45k miles"   / "45.5k miles"                 (k-shorthand → ×1000)

    Returns integer miles in range [100, 400000], or None.
    """
    # k-shorthand first (e.g. "45k miles", "45.5k miles")
    m_k = re.search(r"(\d+(?:\.\d+)?)\s*k\s*(?:miles?|mi\b)", text, re.I)
    if m_k:
        try:
            val = int(float(m_k.group(1)) * 1_000)
            if 100 <= val <= 400_000:
                return val
        except (ValueError, TypeError):
            pass
    # Standard "miles" / "mi" pattern
    m = re.search(r"([\d,]+)\s*(?:miles?|mi\b)", text, re.I)
    if m:
        try:
            val = int(m.group(1).replace(",", ""))
            if 100 <= val <= 400_000:
                return val
        except (ValueError, TypeError):
            pass
    return None


def _random_headers() -> Dict[str, str]:
    """Return a headers dict with a randomly-chosen Chrome UA."""
    return {
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


def _is_generic_motors_landing(html: str) -> bool:
    """Detect the low-signal generic Motors landing page."""
    t = (html or "").lower()
    return (
        "motors makes searching for a used car simple" in t
        and "__next_data__" not in t
        and t.count("/used-cars/local/") >= 10
    )


# ---------------------------------------------------------------------------
# MotorsAdapter
# ---------------------------------------------------------------------------

class MotorsAdapter(BaseIngestionAdapter):
    """
    Scrapes Motors.co.uk using requests + HTML / JSON parsing.

    No authentication is required.  Always available as long as the
    ``requests`` library is installed (included in Dealerly requirements).
    """

    def platform_name(self) -> str:
        return "motors"

    @property
    def is_available(self) -> bool:
        """Always True — only requires the requests library."""
        return True

    def fetch_listings(
        self,
        queries: list[str],
        price_min: int,
        price_max: int,
        pages: int,
        **kwargs,
    ) -> List[Listing]:
        """
        Search Motors.co.uk for each query term and return normalised listings.

        Args:
            queries:    List of search terms, e.g. ["honda jazz", "ford fiesta"].
                        Each term is parsed into make + model for the URL.
            price_min:  Minimum asking price filter (GBP).
            price_max:  Maximum asking price filter (GBP).
            pages:      Maximum result pages to fetch per query.
            **kwargs:   Unused (accepted for interface compatibility).

        Returns:
            Deduplicated List[Listing] with platform="motors".
        """
        session = requests.Session()
        session.headers.update(_random_headers())

        # Warmup: load homepage + a sample search page to acquire session cookies
        # and build session trust. Many classifieds sites use cookie-based bot
        # scoring — a cold session with no cookies triggers Cloudflare/JS challenges.
        _warmup_session(session)
        time.sleep(0.5)
        # Second warmup: hit a generic search page to look like a real user
        # navigating from homepage → search (builds Referer chain).
        _warmup_search(session)

        all_batches: List[List[Listing]] = []

        for query in queries:
            try:
                batch = _fetch_query(
                    session=session,
                    query=query,
                    price_min=price_min,
                    price_max=price_max,
                    pages=pages,
                )
                all_batches.append(batch)
                print(f"  [motors] '{query}' -> {len(batch)} listings")
            except Exception as exc:
                print(f"  [motors] Query '{query}' failed: {exc}")
                all_batches.append([])

        return merge_dedupe(all_batches) if all_batches else []


# ---------------------------------------------------------------------------
# Internal fetch helpers
# ---------------------------------------------------------------------------

def _parse_make_model(query: str):
    """
    Split a query string into (make, model) for the Motors.co.uk URL.

    Rules:
      - First whitespace-delimited token -> make
      - Everything after -> model (URL-encoded separately)
      - Single-word query -> make only, model empty

    Examples:
      "honda jazz"        -> ("honda", "jazz")
      "volkswagen polo"   -> ("volkswagen", "polo")
      "used car"          -> ("used", "car")
      "fiesta"            -> ("fiesta", "")

    Returns:
        Tuple[str, str] of (make, model), both lowercased.
    """
    parts = query.strip().lower().split(None, 1)
    make  = parts[0] if parts else ""
    model = parts[1] if len(parts) > 1 else ""
    return make, model


def _build_search_url(
    make: str, model: str, price_min: int, price_max: int, page: int,
    alt: int = 0,
) -> str:
    """
    Construct the Motors.co.uk search URL for the given parameters.

    Args:
        make:       Vehicle make (e.g. "honda").
        model:      Vehicle model (e.g. "jazz").  Empty string is fine.
        price_min:  Min price GBP.
        price_max:  Max price GBP.
        page:       1-based page number.
        alt:        URL mode index (0..5, including path-based used-cars routes).

    Returns:
        Fully-formed URL string.
    """
    if alt == 1:
        tmpl = _MOTORS_SEARCH_URL_ALT
    elif alt == 2:
        tmpl = _MOTORS_SEARCH_URL_ALT2
    elif alt == 3:
        tmpl = _MOTORS_SEARCH_URL_ALT3
    elif alt == 4:
        tmpl = _MOTORS_SEARCH_URL_ALT4
    elif alt == 5:
        tmpl = _MOTORS_SEARCH_URL_ALT5
    else:
        tmpl = _MOTORS_SEARCH_URL
    return tmpl.format(
        make=quote_plus(make),
        model=quote_plus(model),
        price_min=price_min,
        price_max=price_max,
        page=page,
    )


def _build_search_url_candidates(
    make: str, model: str, price_min: int, price_max: int, page: int,
) -> List[str]:
    """Candidate search URLs across current + legacy Motors route formats."""
    return [
        # Prioritise stable /used-cars and /search routes.
        # Path-based variants frequently bounce to non-results/cazoo URLs.
        _build_search_url(make, model, price_min, price_max, page, alt=3),
        _build_search_url(make, model, price_min, price_max, page, alt=1),
        _build_search_url(make, model, price_min, price_max, page, alt=0),
    ]


def _warmup_session(session: requests.Session) -> bool:
    """
    Perform a warmup GET on the Motors.co.uk homepage to acquire session
    cookies and avoid bot-detection on subsequent search requests.

    Returns True on success, False if the warmup request fails.
    """
    try:
        r = session.get(
            "https://www.motors.co.uk/",
            timeout=_REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
        # Update Referer for all subsequent requests
        session.headers.update({"Referer": "https://www.motors.co.uk/"})
        print(f"  [motors] Warmup -> HTTP {r.status_code} "
              f"(cookies: {list(session.cookies.keys())}, "
              f"body: {len(r.text):,} bytes)")
        return r.status_code == 200
    except Exception as exc:
        print(f"  [motors] Warmup failed: {exc}")
        return False


def _warmup_search(session: requests.Session) -> bool:
    """
    Second warmup: hit a generic used-cars page to build a realistic
    navigation path (homepage → browse → search). This sets additional
    cookies and a Referer chain that helps avoid bot detection.
    """
    try:
        r = session.get(
            "https://www.motors.co.uk/used-cars/",
            timeout=_REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
        session.headers.update({"Referer": "https://www.motors.co.uk/used-cars/"})
        print(f"  [motors] Warmup search -> HTTP {r.status_code}")
        time.sleep(0.35)
        return r.status_code == 200
    except Exception as exc:
        print(f"  [motors] Warmup search failed: {exc}")
        return False


def _fetch_query(
    session: requests.Session,
    query: str,
    price_min: int,
    price_max: int,
    pages: int,
) -> List[Listing]:
    """
    Fetch all result pages for a single query and return normalised listings.

    Args:
        session:    requests.Session with headers already set.
        query:      Search term string.
        price_min:  Min price GBP.
        price_max:  Max price GBP.
        pages:      Maximum pages to fetch.

    Returns:
        Deduplicated List[Listing] across all pages for this query.
    """
    make, model = _parse_make_model(query)
    listings: List[Listing] = []
    seen_ids: set[str] = set()
    query_started = time.time()
    low_signal_pages = 0
    # Sprint 8: start with /used-cars/ (alt=3) — the most stable Motors route.
    # alt=0 (/search/car/) is tried as a fallback on 404 or parse failure.
    _url_mode = 3

    for page_num in range(1, pages + 1):
        if (time.time() - query_started) >= _QUERY_TIME_BUDGET_S:
            print(f"  [motors] Query budget hit ({_QUERY_TIME_BUDGET_S:.0f}s) — stopping early.")
            break
        url = _build_search_url(make, model, price_min, price_max, page_num, alt=_url_mode)

        try:
            resp = session.get(url, timeout=_REQUEST_TIMEOUT_S)
            print(f"  [motors] GET {url[:80]} -> HTTP {resp.status_code} ({len(resp.text):,} bytes)")

            lower_body = (resp.text or "").lower()
            challenge_markers = (
                "cloudflare",
                "cf-browser-verification",
                "verify you are human",
                "checking your browser",
                "cf-challenge",
            )
            challenge_detected = any(m in lower_body for m in challenge_markers)

            if resp.status_code == 403:
                if page_num == 1:
                    # First page 403 — retry once after a longer delay
                    print(f"  [motors] 403 Forbidden — retrying after 5s delay...")
                    time.sleep(5.0)
                    try:
                        resp = session.get(url, timeout=_REQUEST_TIMEOUT_S)
                        print(f"  [motors] Retry -> HTTP {resp.status_code}")
                    except requests.RequestException:
                        pass
                    if resp.status_code == 403:
                        print(f"  [motors] 403 persists — switching to Playwright fallback...")
                        return listings + _playwright_fetch_query(
                            make, model, price_min, price_max, pages, seen_ids
                        )
                else:
                    print(f"  [motors] 403 Forbidden on page {page_num}. Stopping query.")
                    break
            if resp.status_code == 429:
                print(f"  [motors] 429 Too Many Requests — waiting 10s before retry...")
                time.sleep(10.0)
                try:
                    resp = session.get(url, timeout=_REQUEST_TIMEOUT_S)
                    if resp.status_code == 429:
                        print(f"  [motors] Still rate limited. Stopping query.")
                        break
                except requests.RequestException:
                    break
            if resp.status_code in (503, 520, 521, 522, 523, 524) and page_num == 1:
                print(
                    f"  [motors] HTTP {resp.status_code} likely anti-bot edge response "
                    f"— switching to Playwright fallback..."
                )
                return listings + _playwright_fetch_query(
                    make, model, price_min, price_max, pages, seen_ids
                )
            if resp.status_code == 404:
                if _url_mode < 2 and page_num == 1:
                    # Primary URL 404 — try legacy URL formats.
                    _url_mode += 1
                    print(f"  [motors] 404 on URL mode {_url_mode - 1} — trying mode {_url_mode}")
                    url = _build_search_url(make, model, price_min, price_max, page_num, alt=_url_mode)
                    try:
                        resp = session.get(url, timeout=_REQUEST_TIMEOUT_S)
                        print(f"  [motors] Alt GET {url[:80]} -> HTTP {resp.status_code}")
                        if resp.status_code != 200:
                            print(f"  [motors] Alt URL also failed — stopping query.")
                            break
                    except requests.RequestException:
                        break
                else:
                    print(f"  [motors] 404 on page {page_num} — no more pages.")
                    break
            elif resp.status_code != 200:
                if challenge_detected and page_num == 1:
                    print(
                        f"  [motors] Challenge body detected with HTTP {resp.status_code} "
                        f"— switching to Playwright fallback..."
                    )
                    return listings + _playwright_fetch_query(
                        make, model, price_min, price_max, pages, seen_ids
                    )
                print(f"  [motors] HTTP {resp.status_code} for page {page_num} — body: {resp.text[:200]}")
                break

            page_listings = _parse_page(resp.text, price_min, price_max, seen_ids)
            if not page_listings and _is_generic_motors_landing(resp.text):
                page_listings = _parse_store_vehicles(
                    resp.text, price_min, price_max, seen_ids, make=make, model=model
                )
                if page_listings:
                    print(
                        f"  [motors] Page {page_num}: parsed {len(page_listings)} listings "
                        "via Store vehicles payload"
                    )

            if not page_listings:
                _lower = resp.text.lower()
                if challenge_detected:
                    print(
                        f"  [motors] Cloudflare JS challenge detected on page {page_num} "
                        f"— switching to Playwright fallback..."
                    )
                    return listings + _playwright_fetch_query(
                        make, model, price_min, price_max, pages, seen_ids
                    )
                if _is_generic_motors_landing(resp.text) and page_num == 1:
                    print(
                        "  [motors] Generic landing page detected (no listing payload) "
                        "— skipping Playwright fallback for this query."
                    )
                    break
                low_signal_pages += 1
                if low_signal_pages >= _MAX_LOW_SIGNAL_PAGES:
                    print(
                        f"  [motors] Low-signal pages threshold hit ({_MAX_LOW_SIGNAL_PAGES}) "
                        "— ending query early."
                    )
                    break
                _diagnose_empty_page(resp.text, page_num)
                if page_num == 1:
                    # Sprint 8: try remaining static URL modes before Playwright.
                    # We started at alt=3 (/used-cars/); try alt=1 and alt=0 first.
                    _static_fallback_modes = [m for m in (1, 0) if m != _url_mode]
                    _static_ok = False
                    for _fb_mode in _static_fallback_modes:
                        _fb_url = _build_search_url(
                            make, model, price_min, price_max, 1, alt=_fb_mode
                        )
                        print(
                            f"  [motors] Static fallback: trying URL mode {_fb_mode} "
                            f"({_fb_url[:80]})..."
                        )
                        try:
                            _fb_resp = session.get(_fb_url, timeout=_REQUEST_TIMEOUT_S)
                            _fb_pages = _parse_page(
                                _fb_resp.text, price_min, price_max, seen_ids
                            )
                            if _fb_pages:
                                print(
                                    f"  [motors] Static fallback mode {_fb_mode}: "
                                    f"{len(_fb_pages)} listings — using this route."
                                )
                                _url_mode = _fb_mode
                                listings.extend(_fb_pages)
                                _static_ok = True
                                break
                        except requests.RequestException:
                            pass
                    if _static_ok:
                        # Continue paginating from page 2 with the working mode
                        continue
                    # All static modes exhausted — hand off to Playwright
                    print(
                        f"  [motors] All static URL modes exhausted "
                        f"— switching to Playwright fallback..."
                    )
                    return listings + _playwright_fetch_query(
                        make, model, price_min, price_max, pages, seen_ids
                    )
                # Sprint 12: don't hard-stop on first empty page beyond page 1.
                # Let _MAX_LOW_SIGNAL_PAGES (now 3) govern consecutive-empty threshold.

            low_signal_pages = 0
            print(f"  [motors] Page {page_num}: parsed {len(page_listings)} listings")
            listings.extend(page_listings)

        except requests.RequestException as exc:
            print(f"  [motors] Request error (page {page_num}): {type(exc).__name__}: {exc}")
            break

        if page_num < pages:
            time.sleep(_PAGE_SLEEP_S)

    return listings


def _playwright_fetch_query(
    make: str,
    model: str,
    price_min: int,
    price_max: int,
    pages: int,
    seen_ids: Optional[set] = None,
) -> List[Listing]:
    """
    Fetch all result pages for a query using headless Playwright.

    Called as a fallback when requests-based fetching is blocked by a
    Cloudflare JS challenge (403 or empty 200 with challenge body).
    Mirrors _fetch_query but drives a real browser engine to bypass
    Cloudflare's JavaScript-based bot detection.

    Args:
        make:       Vehicle make (e.g. "honda").
        model:      Vehicle model (e.g. "jazz").
        price_min:  Min price GBP.
        price_max:  Max price GBP.
        pages:      Maximum pages to fetch.
        seen_ids:   Optional deduplication set shared with the calling
                    context — prevents duplicates when some pages were
                    already fetched via requests before the fallback.

    Returns:
        List[Listing] or [] if Playwright is unavailable.
    """
    try:
        loop = asyncio.get_running_loop()
        loop_running = loop.is_running()
    except Exception as exc:
        loop_running = False

    # Playwright sync API cannot run in a thread with an active asyncio loop.
    # If one is running (e.g. Spyder/Jupyter), execute fallback in a worker thread.
    if loop_running:
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="motors-pw") as ex:
                fut = ex.submit(
                    _playwright_fetch_query_sync,
                    make,
                    model,
                    price_min,
                    price_max,
                    pages,
                    seen_ids,
                )
                return fut.result(timeout=120)
        except Exception as exc:
            print(f"  [motors] Playwright threaded fallback error: {exc}")
            return _playwright_fetch_query_subprocess(
                make, model, price_min, price_max, pages, seen_ids
            )

    try:
        return _playwright_fetch_query_sync(make, model, price_min, price_max, pages, seen_ids)
    except NotImplementedError:
        print("  [motors] Playwright sync unsupported in current loop — using subprocess fallback...")
        return _playwright_fetch_query_subprocess(
            make, model, price_min, price_max, pages, seen_ids
        )


def _playwright_fetch_query_sync(
    make: str,
    model: str,
    price_min: int,
    price_max: int,
    pages: int,
    seen_ids: Optional[set] = None,
) -> List[Listing]:
    _ensure_playwright_compatible_event_loop_policy()

    if not _PLAYWRIGHT_AVAILABLE:
        print("  [motors] Playwright not installed — cannot bypass Cloudflare.")
        print("  Install with: pip install playwright && playwright install chromium")
        return []

    if seen_ids is None:
        seen_ids = set()

    listings: List[Listing] = []
    pw_started = time.time()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                ],
            )
            ctx = browser.new_context(
                user_agent=random.choice(_UA_POOL),
                viewport={"width": 1280, "height": 900},
                extra_http_headers={
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-GB', 'en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                window.chrome = {runtime: {}};
            """)
            page = ctx.new_page()
            response_payloads: List[Any] = []

            def _on_response(resp) -> None:
                """Capture JSON API payloads emitted during page navigation."""
                try:
                    ctype = (resp.headers or {}).get("content-type", "").lower()
                    url_l = (resp.url or "").lower()
                    is_json = "application/json" in ctype or url_l.endswith(".json")
                    looks_like_search_api = any(
                        token in url_l for token in ("/api/", "search", "vehicle", "listing", "stock")
                    )
                    if not (is_json or looks_like_search_api):
                        return
                    data = None
                    try:
                        data = resp.json()
                    except Exception:
                        data = None
                    if data is not None:
                        response_payloads.append(data)
                except Exception:
                    pass

            page.on("response", _on_response)

            print("  [motors] Playwright: warming up on homepage...")
            page.goto(
                "https://www.motors.co.uk/",
                wait_until="domcontentloaded",
                timeout=25_000,
            )
            time.sleep(3.5)
            preferred_candidate_idx: Optional[int] = None

            for page_num in range(1, pages + 1):
                if (time.time() - pw_started) >= _PLAYWRIGHT_QUERY_BUDGET_S:
                    print(
                        f"  [motors] Playwright budget hit ({_PLAYWRIGHT_QUERY_BUDGET_S:.0f}s) "
                        "— ending fallback early."
                    )
                    break
                response_payloads.clear()
                page_listings: List[Listing] = []
                html = ""
                try:
                    all_candidates = _build_search_url_candidates(
                        make, model, price_min, price_max, page_num
                    )
                    if preferred_candidate_idx is not None and 0 <= preferred_candidate_idx < len(all_candidates):
                        candidate_urls = [all_candidates[preferred_candidate_idx]]
                    else:
                        candidate_urls = list(all_candidates)

                    # If preferred route fails on later pages, we can fall back to
                    # trying all candidates once for resilience.
                    tried_all_candidates = (preferred_candidate_idx is None)
                    candidate_cursor = 0
                    while candidate_cursor < len(candidate_urls):
                        if (time.time() - pw_started) >= _PLAYWRIGHT_QUERY_BUDGET_S:
                            break
                        url = candidate_urls[candidate_cursor]
                        candidate_cursor += 1
                        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=8_000)
                        except Exception:
                            pass
                        try:
                            challenge = page.evaluate(
                                "() => /verify you are human|checking your browser|cf-challenge|cloudflare/i.test(document.body?.innerText || '')"
                            )
                        except Exception:
                            challenge = False
                        if challenge:
                            print("  [motors] Playwright: challenge/interstitial detected, waiting...")
                            time.sleep(8.0)
                            try:
                                page.reload(wait_until="domcontentloaded", timeout=25_000)
                                page.wait_for_timeout(1500)
                            except Exception:
                                pass
                        for scroll_y in (400, 800, 1200):
                            page.mouse.wheel(0, scroll_y)
                            time.sleep(0.5)
                        time.sleep(1.2)
                        html = page.content()
                        if _is_generic_motors_landing(html):
                            print(
                                f"  [motors] Playwright page {page_num}: generic landing "
                                "page detected, ending query early."
                            )
                            page_listings = []
                            break
                        if _looks_non_results_page(page.url, html):
                            print(
                                f"  [motors] Playwright page {page_num}: landing is non-results "
                                f"({page.url[:70]}), trying next URL mode..."
                            )
                            continue
                        print(
                            f"  [motors] Playwright page {page_num}: tried {url[:70]} "
                            f"-> landed {page.url[:70]} ({len(html):,} bytes)"
                        )

                        try:
                            nd_json = page.evaluate(
                                "() => window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__) : null"
                            )
                            if nd_json and nd_json != "null":
                                nd = json.loads(nd_json)
                                page_listings = _parse_next_data(nd, price_min, price_max, seen_ids)
                                if page_listings:
                                    print(
                                        f"  [motors] Playwright page {page_num}: "
                                        f"{len(page_listings)} listings via window.__NEXT_DATA__"
                                    )
                        except Exception:
                            pass

                        if not page_listings:
                            page_listings = _playwright_extract_dom_cards(
                                page, price_min, price_max, seen_ids,
                            )
                            if page_listings:
                                print(
                                    f"  [motors] Playwright page {page_num}: "
                                    f"{len(page_listings)} listings via DOM extraction"
                                )

                        if not page_listings:
                            page_listings = _parse_playwright_response_payloads(
                                response_payloads, price_min, price_max, seen_ids
                            )
                            if page_listings:
                                print(
                                    f"  [motors] Playwright page {page_num}: "
                                    f"{len(page_listings)} listings via API payloads"
                                )

                        if not page_listings:
                            page_listings = _parse_page(html, price_min, price_max, seen_ids)
                            if page_listings:
                                print(
                                    f"  [motors] Playwright page {page_num}: "
                                    f"{len(page_listings)} listings via HTML parse"
                                )

                        if page_listings:
                            if preferred_candidate_idx is None:
                                try:
                                    preferred_candidate_idx = all_candidates.index(url)
                                    print(
                                        f"  [motors] Playwright: locked route mode {preferred_candidate_idx} "
                                        f"for query '{make} {model}'."
                                    )
                                except ValueError:
                                    pass
                            break

                    if (
                        not page_listings
                        and preferred_candidate_idx is not None
                        and page_num > 1
                        and not tried_all_candidates
                    ):
                        # Preferred route produced no listings on this page; retry
                        # all modes once before giving up.
                        for url in all_candidates:
                            if url in candidate_urls:
                                continue
                            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                            try:
                                page.wait_for_load_state("networkidle", timeout=6_000)
                            except Exception:
                                pass
                            html = page.content()
                            page_listings = _parse_page(html, price_min, price_max, seen_ids)
                            if page_listings:
                                print(
                                    f"  [motors] Playwright page {page_num}: "
                                    f"{len(page_listings)} listings via retry route"
                                )
                                try:
                                    preferred_candidate_idx = all_candidates.index(url)
                                except ValueError:
                                    pass
                                break

                    if not page_listings:
                        _diagnose_empty_page(html, page_num)
                        break

                    listings.extend(page_listings)

                except Exception as page_exc:
                    print(f"  [motors] Playwright page {page_num} error: {page_exc}")
                    break

                if page_num < pages:
                    time.sleep(_PAGE_SLEEP_S)

            browser.close()
    except Exception as exc:
        print(f"  [motors] Playwright session error: {type(exc).__name__}: {exc!r}")
        if isinstance(exc, NotImplementedError):
            raise

    return listings


def _playwright_fetch_query_subprocess(
    make: str,
    model: str,
    price_min: int,
    price_max: int,
    pages: int,
    seen_ids: Optional[set] = None,
) -> List[Listing]:
    """
    Run Playwright fallback in a separate Python process.

    This isolates Playwright from IDE-managed asyncio loops that break subprocess
    transport (common with nest_asyncio integrations on Windows).
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return []

    payload = {
        "make": make,
        "model": model,
        "price_min": price_min,
        "price_max": price_max,
        "pages": pages,
        "seen_ids": sorted(seen_ids or []),
    }
    script = (
        "import io,json,sys,contextlib;"
        "from dataclasses import asdict;"
        "from dealerly.motors import _playwright_fetch_query_sync;"
        "p=json.loads(sys.argv[1]);"
        "s=set(p.get('seen_ids') or []);"
        "buf=io.StringIO();"
        "with contextlib.redirect_stdout(buf):"
        " lst=_playwright_fetch_query_sync("
        "  p['make'],p['model'],p['price_min'],p['price_max'],p['pages'],s"
        " );"
        "print(json.dumps([asdict(x) for x in lst], ensure_ascii=True))"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, json.dumps(payload, ensure_ascii=True)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            cwd=None,
        )
    except Exception as exc:
        print(f"  [motors] Playwright subprocess launch failed: {type(exc).__name__}: {exc!r}")
        return []

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        print(f"  [motors] Playwright subprocess failed (exit {proc.returncode}): {err[:300]}")
        return []

    raw_out = (proc.stdout or "").strip()
    if not raw_out:
        return []

    try:
        data = json.loads(raw_out)
        out: List[Listing] = []
        for row in data:
            if isinstance(row, dict):
                out.append(Listing(**row))
        return out
    except Exception as exc:
        print(f"  [motors] Playwright subprocess parse error: {type(exc).__name__}: {exc!r}")
        return []


def _ensure_playwright_compatible_event_loop_policy() -> None:
    """
    Ensure asyncio loop policy supports subprocesses for Playwright on Windows.

    Some IDEs (Spyder/Jupyter integrations) set SelectorEventLoopPolicy, which
    raises NotImplementedError for subprocess transport on modern Python builds.
    Playwright requires subprocess support to launch browser driver processes.
    """
    global _WIN_EVENT_LOOP_POLICY_SET
    if _WIN_EVENT_LOOP_POLICY_SET:
        return
    if not sys.platform.startswith("win"):
        _WIN_EVENT_LOOP_POLICY_SET = True
        return
    try:
        # Use Proactor policy on Windows so asyncio subprocess APIs work.
        policy_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
        if policy_cls is not None:
            asyncio.set_event_loop_policy(policy_cls())
    except Exception as exc:
        print(f"  [motors] Could not set Windows Proactor policy: {type(exc).__name__}: {exc!r}")
    finally:
        _WIN_EVENT_LOOP_POLICY_SET = True


def _parse_playwright_response_payloads(
    payloads: List[Any],
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """Parse listing candidates from JSON payloads captured by Playwright."""
    if not payloads:
        return []
    out: List[Listing] = []
    for payload in payloads:
        for vehicle_list in _find_vehicle_lists_in_payload(payload):
            for vehicle in vehicle_list:
                if not isinstance(vehicle, dict):
                    continue
                listing = _vehicle_dict_to_listing(vehicle, price_min, price_max, seen_ids)
                if listing is None:
                    listing = _payload_dict_to_listing_relaxed(
                        vehicle, price_min, price_max, seen_ids
                    )
                if listing is not None:
                    out.append(listing)
        # Deep fallback: walk arbitrary dict nodes and extract listing-like records
        for node in _iter_dict_nodes(payload):
            listing = _payload_node_to_listing_deep(node, price_min, price_max, seen_ids)
            if listing is not None:
                out.append(listing)
    return out


def _looks_non_results_page(url: str, html: str) -> bool:
    """Heuristic guard for Motors landing/error pages that are not listings."""
    u = (url or "").lower()
    t = (html or "").lower()
    if "/error" in u:
        return True
    if "/search/car/" in u and "/used-cars/" not in u and "/cars/" not in u:
        return True
    if "sorry an error has occurred" in t:
        return True
    if "motors makes searching for a used car simple" in t and "/used-cars/" not in u and "/cars/" not in u:
        return True
    return False


def _normalise_image_url(url: str) -> str:
    """Normalize Motors image URLs to absolute HTTPS URLs."""
    u = (url or "").strip()
    if not u:
        return ""
    # Some embedded JSON strings use escaped slashes.
    u = u.replace("\\/", "/")
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://www.motors.co.uk" + u
    return u


def _is_placeholder_image_url(url: str) -> bool:
    """True when URL points to a known generic placeholder image."""
    u = (url or "").strip().lower()
    if not u:
        return True
    return (
        "motors-cdn/images/noimage" in u
        or "noimage_lrg" in u
        or "/no-image" in u
        or "placeholder" in u
    )


_DETAIL_IMAGE_CACHE: Dict[str, str] = {}
_DETAIL_IMAGE_RESOLVE_LIMIT = 18
_DETAIL_IMAGE_RESOLVE_COUNT = 0


def _resolve_detail_image_url(detail_url: str) -> str:
    """
    Best-effort fetch of a Motors detail page to recover a real hero image when
    search payload only provides a generic placeholder image.
    """
    global _DETAIL_IMAGE_RESOLVE_COUNT
    u = _normalise_image_url(detail_url or "")
    if not u:
        return ""
    if u in _DETAIL_IMAGE_CACHE:
        return _DETAIL_IMAGE_CACHE[u]
    if "/car-" not in u:
        _DETAIL_IMAGE_CACHE[u] = ""
        return ""
    if _DETAIL_IMAGE_RESOLVE_COUNT >= _DETAIL_IMAGE_RESOLVE_LIMIT:
        _DETAIL_IMAGE_CACHE[u] = ""
        return ""
    _DETAIL_IMAGE_RESOLVE_COUNT += 1
    try:
        r = requests.get(
            u,
            timeout=max(6, int(_REQUEST_TIMEOUT_S)),
            headers=_random_headers(),
            allow_redirects=True,
        )
        html = r.text or ""
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<link[^>]+rel=["\']preload["\'][^>]+as=["\']image["\'][^>]+href=["\']([^"\']+)["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if not m:
                continue
            candidate = _normalise_image_url(m.group(1).strip())
            if candidate and not _is_placeholder_image_url(candidate):
                _DETAIL_IMAGE_CACHE[u] = candidate
                return candidate
    except Exception:
        pass
    _DETAIL_IMAGE_CACHE[u] = ""
    return ""


def _prefer_non_placeholder_image(image_url: str, detail_url: str) -> str:
    """Keep valid image; if placeholder, try resolving from detail page."""
    n = _normalise_image_url(image_url or "")
    if n and not _is_placeholder_image_url(n):
        return n
    resolved = _resolve_detail_image_url(detail_url)
    if resolved:
        return resolved
    return n


def _is_probable_vehicle_detail_url(url: str) -> bool:
    """Reject obvious category/filter pages that are not individual cars."""
    u = (url or "").lower()
    if not re.search(r"/(car-details|used-cars|cars)/", u):
        return False
    bad_tokens = (
        "/under-",
        "/for-sale",
        "/cars/",
        "/cars/honda/",
        "/cars/toyota/",
        "/cars/nissan/",
        "/cars/peugeot/",
        "/cars/ford/",
        "/cars/vauxhall/",
        "/cars/suzuki/",
        "/cars/dacia/",
    )
    # Keep canonical detail pages like /cars-for-sale/<id>/ and reject broad facets.
    if "/cars-for-sale/" in u:
        return True
    if any(tok in u for tok in bad_tokens):
        # allow make/model landing pages only if they carry an explicit numeric id
        return bool(re.search(r"/(\d{6,})/?($|[?#])", u))
    return True


def _is_junk_title(title: str) -> bool:
    t = (title or "").strip().lower()
    if not t:
        return True
    if t.startswith("under £") or t.startswith("under gbp"):
        return True
    if "used cars for sale" in t:
        return True
    return False


def _find_vehicle_lists_in_payload(payload: Any, max_depth: int = 6) -> List[List[Dict[str, Any]]]:
    """Recursively collect list-of-dict nodes that look like vehicle arrays."""
    matches: List[List[Dict[str, Any]]] = []

    def _walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, list) and node:
            sample = node[0]
            if isinstance(sample, dict):
                has_price = any(
                    k in sample for k in ("price", "askingPrice", "displayPrice", "priceGbp")
                )
                has_identity = any(
                    k in sample for k in ("id", "adId", "listingId", "vehicleId", "url", "title")
                )
                if has_price and has_identity:
                    matches.append(node)
            for child in node:
                _walk(child, depth + 1)
        elif isinstance(node, dict):
            for child in node.values():
                _walk(child, depth + 1)

    _walk(payload, 0)
    return matches


def _iter_dict_nodes(payload: Any, max_depth: int = 8) -> List[Dict[str, Any]]:
    """Return nested dict nodes from any payload structure."""
    out: List[Dict[str, Any]] = []

    def _walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, dict):
            out.append(node)
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                _walk(v, depth + 1)

    _walk(payload, 0)
    return out


def _payload_node_to_listing_deep(
    node: Dict[str, Any],
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> Optional[Listing]:
    """Very tolerant node parser for unknown API schema variants."""
    url = str(
        node.get("url")
        or node.get("adUrl")
        or node.get("listingUrl")
        or node.get("href")
        or ""
    ).strip()
    if not url:
        return None
    if not _is_probable_vehicle_detail_url(url):
        return None
    if url.startswith("/"):
        url = "https://www.motors.co.uk" + url
    if "motors.co.uk" not in url and not url.startswith("http"):
        return None
    # Prefer actual detail pages to avoid category links.
    if not _is_probable_vehicle_detail_url(url):
        return None

    title = str(
        node.get("title")
        or node.get("displayTitle")
        or node.get("name")
        or node.get("vehicleName")
        or ""
    ).strip()
    if _is_junk_title(title):
        return None
    if not title:
        title = "Used car listing"

    # Try direct fields first, then regex over compact node JSON.
    price = _coerce_price(
        node.get("price")
        or node.get("askingPrice")
        or node.get("displayPrice")
        or node.get("priceGbp")
        or 0
    )
    if price <= 0:
        compact = json.dumps(node, ensure_ascii=True)
        m = re.search(r"(?:£\\/?u00a3|£)\s?([\d,]{3,})", compact)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
            except Exception:
                price = 0.0
        if price <= 0:
            m2 = re.search(r'"(?:price|askingPrice|displayPrice|priceGbp)"\s*:\s*"?(\d{3,6})"?', compact)
            if m2:
                try:
                    price = float(m2.group(1))
                except Exception:
                    price = 0.0
    if price <= 0 or price < price_min or price > price_max:
        return None

    raw_id = str(
        node.get("id") or node.get("adId") or node.get("listingId") or node.get("vehicleId") or ""
    ).strip()
    if not raw_id:
        id_match = re.search(r"(\d{5,})", url)
        raw_id = id_match.group(1) if id_match else f"{abs(hash(url.split('?')[0])):x}"
    item_id = f"motors_{raw_id}"
    if item_id in seen_ids:
        return None
    seen_ids.add(item_id)

    guess = guess_make_model(title)
    location = str(node.get("location") or node.get("town") or node.get("dealerLocation") or "").strip()
    fuel_type = str(node.get("fuelType") or node.get("fuel") or guess.fuel_type or "").strip().lower()
    year_raw = node.get("year") or node.get("registrationYear") or node.get("modelYear")
    year: Optional[int] = None
    if year_raw:
        try:
            year = int(str(year_raw)[:4])
        except Exception:
            year = None
    mileage_raw = node.get("mileage") or node.get("odometerReading") or node.get("miles")
    mileage: Optional[int] = None
    if mileage_raw:
        try:
            mileage = int(re.sub(r"[^\d]", "", str(mileage_raw)))
        except Exception:
            mileage = None

    return Listing(
        platform="motors",
        item_id=item_id,
        title=title,
        price_gbp=price,
        url=url,
        location=location,
        condition="Used",
        vrm="",
        raw=node,
        fuel_type=fuel_type or guess.fuel_type,
        year=year or guess.year,
        mileage=mileage or guess.mileage,
        ulez_compliant=is_ulez_compliant(year or guess.year, fuel_type or guess.fuel_type),
    )


def _payload_dict_to_listing_relaxed(
    v: Dict[str, Any],
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> Optional[Listing]:
    """Best-effort payload→Listing conversion when canonical IDs are missing."""
    title = str(v.get("title") or v.get("displayTitle") or v.get("name") or "").strip()
    url = str(v.get("url") or v.get("adUrl") or v.get("listingUrl") or "").strip()
    if url.startswith("/"):
        url = "https://www.motors.co.uk" + url
    price_raw = (
        v.get("price") or v.get("askingPrice") or v.get("displayPrice") or v.get("priceGbp") or 0
    )
    price = _coerce_price(price_raw)
    if not title or not url or _is_junk_title(title) or not _is_probable_vehicle_detail_url(url):
        return None
    if price <= 0 or price < price_min or price > price_max:
        return None

    raw_id = str(v.get("id") or v.get("adId") or v.get("listingId") or v.get("vehicleId") or "").strip()
    if not raw_id:
        raw_id = re.sub(r"[^a-zA-Z0-9]", "", url.split("?")[0])[-18:] or f"{abs(hash(url)):x}"
    item_id = f"motors_{raw_id}"
    if item_id in seen_ids:
        return None
    seen_ids.add(item_id)

    year_raw = v.get("year") or v.get("registrationYear") or v.get("modelYear")
    year: Optional[int] = None
    if year_raw:
        try:
            year = int(str(year_raw)[:4])
        except Exception:
            year = None

    mileage_raw = v.get("mileage") or v.get("odometerReading") or v.get("miles")
    mileage: Optional[int] = None
    if mileage_raw:
        try:
            mileage = int(re.sub(r"[^\d]", "", str(mileage_raw)))
        except Exception:
            mileage = None

    fuel_type = str(v.get("fuelType") or v.get("fuel") or "").strip().lower()
    location = str(v.get("location") or v.get("town") or v.get("dealerLocation") or "").strip()
    image_url = _extract_best_image_url(v)
    guess = guess_make_model(title)
    if not year:
        year = guess.year
    if not mileage:
        mileage = guess.mileage
    if not fuel_type:
        fuel_type = guess.fuel_type

    return Listing(
        platform="motors",
        item_id=item_id,
        title=title,
        price_gbp=price,
        url=url,
        location=location,
        condition="Used",
        vrm="",
        raw=v,
        fuel_type=fuel_type,
        year=year,
        mileage=mileage,
        ulez_compliant=is_ulez_compliant(year, fuel_type),
        first_image_url=_prefer_non_placeholder_image(image_url, url),
    )


def _playwright_extract_dom_cards(
    page: Any,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """
    Extract listing data directly from the rendered DOM via JS evaluation.

    This is the most resilient Playwright parsing path — it runs inside the
    browser context after all client-side JS has hydrated, so it works even
    when __NEXT_DATA__ is absent and static HTML parsing fails.

    Modelled on facebook.py's _extract_cards approach: run a JS snippet that
    queries visible listing card elements and returns structured data.

    Args:
        page:       Active Playwright Page object.
        price_min:  Min price filter (GBP).
        price_max:  Max price filter (GBP).
        seen_ids:   Deduplication set (mutated).

    Returns:
        List[Listing] extracted from the live DOM.
    """
    raw_cards = page.evaluate(r"""
        () => {
            const results = [];
            // Motors.co.uk card selectors — ordered most to least specific
            const selectors = [
                'article[data-vehicle-id]',
                '[data-testid*="vehicle"]',
                '[data-testid*="listing"]',
                '[class*="VehicleCard"]',
                '[class*="vehicle-card"]',
                '[class*="listing-card"]',
                '[class*="SearchResult"]',
                '[class*="search-result"]',
                'a[href*="/car-details/"]',
                'a[href*="/used-cars/"]',
                'a[href*="/cars/"]',
            ];

            let cards = [];
            for (const sel of selectors) {
                cards = document.querySelectorAll(sel);
                if (cards.length > 0) break;
            }

            for (const card of cards) {
                try {
                    // Find the link
                    const anchor = card.tagName === 'A' ? card
                        : card.querySelector('a[href*="/car-details/"], a[href*="/used-cars/"], a[href*="/cars/"]')
                        || card.querySelector('a[href]');
                    if (!anchor) continue;

                    const href = anchor.href || '';

                    // Extract text spans for title/price/location
                    const allText = card.innerText || '';

                    // Image
                    const img = card.querySelector('img');
                    const imgSrc = img ? (img.src || img.dataset.src || '') : '';

                    // Data attributes
                    const vehicleId = card.dataset.vehicleId
                        || card.dataset.adId
                        || card.dataset.listingId
                        || '';

                    // Sprint 8: grab a dedicated mileage element when available
                    const mileageEl = card.querySelector(
                        '[class*="mileage"],[class*="odometer"],[data-mileage],' +
                        '[class*="Mileage"],[class*="miles"]'
                    );
                    const mileageText = mileageEl ? mileageEl.innerText.trim() : '';

                    results.push({
                        url: href,
                        text: allText,
                        image: imgSrc,
                        vehicleId: vehicleId,
                        mileageText: mileageText,
                    });
                } catch (e) {}
            }

            // Broad fallback:
            // scan anchors that look like detail-page links and have a price
            // nearby in text content.
            if (results.length === 0) {
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                for (const a of anchors) {
                    try {
                        const hrefRaw = a.getAttribute('href') || '';
                        const href = a.href || hrefRaw;
                        if (!href) continue;
                        if (!/\/(car-details|used-cars|cars|car-|vehicle|listing)/i.test(href)) continue;

                        const container = a.closest('article, li, div, section') || a.parentElement || a;
                        const text = (container.innerText || a.innerText || '').trim();
                        if (!text) continue;
                        if (!/£\s?\d[\d,]*/.test(text)) continue;

                        const img = container.querySelector('img') || a.querySelector('img');
                        const imgSrc = img ? (img.src || img.dataset.src || '') : '';
                        const vehicleId = container.dataset?.vehicleId
                            || container.dataset?.adId
                            || container.dataset?.listingId
                            || '';

                        results.push({
                            url: href,
                            text: text,
                            image: imgSrc,
                            vehicleId: vehicleId,
                            mileageText: '',
                        });
                    } catch (e) {}
                }
            }
            return results;
        }
    """)

    if not raw_cards:
        return []

    listings: List[Listing] = []

    for card in raw_cards:
        url = (card.get("url") or "").strip()
        if not url:
            continue
        if url.startswith("/"):
            url = "https://www.motors.co.uk" + url
        if not _is_probable_vehicle_detail_url(url):
            continue

        text = card.get("text") or ""
        image_url = card.get("image") or ""
        vehicle_id = card.get("vehicleId") or ""

        # Extract item_id from URL or data attribute
        raw_id = vehicle_id
        if not raw_id:
            id_match = (
                re.search(r"/car-details/(\d+)", url)
                or re.search(r"/used-cars/[^/]+/(\d+)", url)
                or re.search(r"/cars/[^/]+/(\d+)", url)
            )
            raw_id = id_match.group(1) if id_match else ""
        if not raw_id:
            raw_id = f"{abs(hash(url.split('?')[0])):x}"

        item_id = f"motors_{raw_id}"
        if item_id in seen_ids:
            continue

        # Parse price from card text
        price_match = re.search(r"£\s?([\d,]+)", text)
        price = 0.0
        if price_match:
            try:
                price = float(price_match.group(1).replace(",", ""))
            except ValueError:
                pass
        if price <= 0 or price < price_min or price > price_max:
            continue

        # Parse title — first line or heading-like text
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        title = ""
        for ln in lines:
            # Skip price-only or very short lines
            if re.match(r"^£", ln) or len(ln) < 8:
                continue
            title = ln[:200]
            break
        if not title or _is_junk_title(title):
            continue

        # Year
        year: Optional[int] = None
        year_match = re.search(r"\b(19[6-9]\d|20[0-3]\d)\b", text)
        if year_match:
            try:
                year = int(year_match.group(1))
            except ValueError:
                pass

        # Mileage (Sprint 8: prefer dedicated mileage element, fallback to card text)
        mileage_src = card.get("mileageText") or text
        mileage: Optional[int] = _parse_mileage_from_text(mileage_src)

        # Location — often last meaningful line
        location = ""
        for ln in reversed(lines):
            if not re.match(r"^[£\d]", ln) and len(ln) < 60 and ln != title:
                location = ln[:80]
                break

        seen_ids.add(item_id)
        guess = guess_make_model(title)
        fuel_type = guess.fuel_type

        vrm = ""
        vrm_result = _scan_patterns(title.upper(), SAFE_VRM_PATTERNS)
        if vrm_result:
            candidate, _, conf = vrm_result
            candidate = normalise_vrm(candidate)
            if candidate and looks_plausible_uk_vrm(candidate):
                vrm = candidate

        listing = Listing(
            platform="motors",
            item_id=item_id,
            title=title,
            price_gbp=price,
            url=url,
            location=location,
            condition="Used",
            vrm=vrm,
            raw={"dom_card": text[:300]},
            fuel_type=fuel_type,
            year=year or guess.year,
            mileage=mileage or guess.mileage,
            ulez_compliant=is_ulez_compliant(year or guess.year, fuel_type),
            first_image_url=_prefer_non_placeholder_image(image_url, url),
        )

        if vrm:
            listing.vrm_source = "regex_title_motors"
            listing.vrm_confidence = 0.85

        listings.append(listing)

    return listings


def _diagnose_empty_page(html: str, page_num: int) -> None:
    """
    Log diagnostic information when all parsing strategies return zero listings.
    Helps identify why Motors.co.uk is returning no results.
    """
    # Check for __NEXT_DATA__
    next_data = _extract_next_data(html)
    if next_data:
        page_props = next_data.get("props", {}).get("pageProps", {})
        pp_keys = list(page_props.keys())
        print(f"  [motors] Page {page_num}: __NEXT_DATA__ found, pageProps keys: {pp_keys}")
        # Try to show what's in the data
        for key in ("searchResults", "vehicles", "listings", "results", "initialProps", "data"):
            val = page_props.get(key)
            if val is not None:
                if isinstance(val, dict):
                    print(f"  [motors]   pageProps.{key} keys: {list(val.keys())[:8]}")
                elif isinstance(val, list):
                    print(f"  [motors]   pageProps.{key}: list of {len(val)} items")
                else:
                    print(f"  [motors]   pageProps.{key}: {str(val)[:80]}")
    else:
        print(f"  [motors] Page {page_num}: no __NEXT_DATA__ found")
        # Check for common block indicators
        lower = html.lower()
        if "cloudflare" in lower or "cf-browser-verification" in lower:
            print(f"  [motors] Cloudflare challenge detected")
        elif "access denied" in lower:
            print(f"  [motors] Access denied page")
        elif "no results" in lower or "no cars found" in lower:
            print(f"  [motors] Site reports no results for this search")
        else:
            # Show first 300 chars of body to aid debugging
            import re as _re
            body_text = _re.sub(r'<[^>]+>', ' ', html)[:300].strip()
            print(f"  [motors] Page text preview: {body_text[:200]}")


def _parse_page(
    html: str,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """
    Extract listings from a Motors.co.uk search results HTML page.

    Strategy:
      1. Try __NEXT_DATA__ JSON (Next.js embedded data) — clean & structured.
      2. Fallback: BeautifulSoup HTML parsing.
      3. Fallback: Pure regex extraction.

    Args:
        html:       Raw HTML response body.
        price_min:  Listings below this price are skipped.
        price_max:  Listings above this price are skipped.
        seen_ids:   Set of item_ids already collected (mutated in place).

    Returns:
        List[Listing] parsed from this page.
    """
    # --- Strategy 1: __NEXT_DATA__ ---
    next_data = _extract_next_data(html)
    if next_data:
        listings = _parse_next_data(next_data, price_min, price_max, seen_ids)
        if listings:
            return listings
        # __NEXT_DATA__ found but no vehicles extracted — log and try fallbacks
    else:
        pass  # __NEXT_DATA__ absent — try HTML parsing

    # --- Strategy 2: SearchResults hydrate payload ---
    # Motors current search pages often embed JSON in React hydrate call:
    # ReactDOM.hydrate(... {"initialResults":[...] ... } ...)
    initial_results = _parse_initial_results_payload(html, price_min, price_max, seen_ids)
    if initial_results:
        return initial_results

    # --- Strategy 3: BeautifulSoup ---
    if _BS4_AVAILABLE:
        listings = _parse_bs4(html, price_min, price_max, seen_ids)
        if listings:
            return listings

    # --- Strategy 4: JSON-LD ---
    jsonld_listings = _parse_json_ld(html, price_min, price_max, seen_ids)
    if jsonld_listings:
        return jsonld_listings

    # --- Strategy 5: Href+price regex ---
    href_listings = _parse_href_price(html, price_min, price_max, seen_ids)
    if href_listings:
        return href_listings

    # --- Strategy 6: Broad anchor+context fallback ---
    anchor_listings = _parse_anchor_context(html, price_min, price_max, seen_ids)
    if anchor_listings:
        return anchor_listings

    # --- Strategy 7: Regex ---
    regex_listings = _parse_regex(html, price_min, price_max, seen_ids)
    return regex_listings


def _parse_initial_results_payload(
    html: str,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """Parse `initialResults` array embedded in SearchResults hydrate payload."""
    marker = '"initialResults":'
    idx = html.find(marker)
    if idx < 0:
        return []
    arr_start = html.find("[", idx)
    arr_text = _slice_balanced_brackets(html, arr_start)
    if not arr_text:
        return []
    try:
        rows = json.loads(arr_text)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []

    listings: List[Listing] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("ObjectType", "")).lower() != "usedvehicleresult":
            continue
        listing = _search_result_to_listing(row, price_min, price_max, seen_ids)
        if listing is not None:
            listings.append(listing)
    return listings


def _search_result_to_listing(
    row: Dict[str, Any],
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> Optional[Listing]:
    """Convert SearchResults hydrate payload row to Listing."""
    title = _compose_motors_title(row)
    if not title:
        return None
    price = _coerce_price(row.get("GBPPrice") or row.get("GBPPriceIncVAT") or row.get("Price") or 0)
    if price <= 0 or price < price_min or price > price_max:
        return None

    detail = str(row.get("DetailsPageUrl") or row.get("Url") or "").strip()
    if not detail:
        return None
    url = detail if detail.startswith("http") else f"https://www.motors.co.uk{detail}"
    raw_id_match = re.search(r"(\d{5,})", url)
    raw_id = raw_id_match.group(1) if raw_id_match else f"{abs(hash(url)):x}"
    item_id = f"motors_{raw_id}"
    if item_id in seen_ids:
        return None
    seen_ids.add(item_id)

    mileage: Optional[int] = None
    mil_raw = str(row.get("Mileage") or "").strip()
    if mil_raw:
        try:
            mileage = int(re.sub(r"[^\d]", "", mil_raw))
        except Exception:
            mileage = None
    year: Optional[int] = None
    year_raw = str(row.get("RegistrationYear") or "").strip()
    if year_raw:
        try:
            year = int(year_raw[:4])
        except Exception:
            year = None

    dealer = row.get("Dealer") or {}
    location = ""
    if isinstance(dealer, dict):
        location = str(
            dealer.get("Town")
            or dealer.get("City")
            or dealer.get("Postcode")
            or dealer.get("Name")
            or ""
        ).strip()
    guess = guess_make_model(title)
    fuel_type = str(row.get("FuelType") or guess.fuel_type or "").lower()
    vrm = str(row.get("Registration") or "").strip()
    return Listing(
        platform="motors",
        item_id=item_id,
        title=title,
        price_gbp=price,
        url=url,
        location=location,
        condition="Used",
        vrm=vrm,
        raw={"search_result": row},
        fuel_type=fuel_type,
        year=year or guess.year,
        mileage=mileage or guess.mileage,
        ulez_compliant=is_ulez_compliant(year or guess.year, fuel_type),
        first_image_url=_prefer_non_placeholder_image(_extract_best_image_url(row), url),
    )


def _compose_motors_title(row: Dict[str, Any]) -> str:
    """Build a richer Motors title using make/model + key listing attributes."""
    base = str(row.get("Variant") or row.get("Title") or "").strip()
    make = str(row.get("Make") or row.get("Manufacturer") or "").strip()
    model = str(row.get("Model") or row.get("ModelName") or "").strip()
    name = " ".join([x for x in [make, model] if x]).strip()
    title = base
    if name and name.lower() not in base.lower():
        title = f"{name} - {base}" if base else name

    extra_bits: List[str] = []
    fuel = str(row.get("FuelType") or row.get("Fuel") or "").strip()
    gearbox = str(row.get("TransmissionType") or row.get("Transmission") or "").strip()
    body = str(row.get("BodyType") or row.get("BodyStyle") or "").strip()
    mileage = str(row.get("Mileage") or "").strip()
    for val in (fuel, gearbox, body):
        if val:
            extra_bits.append(val)
    if mileage and mileage not in title:
        extra_bits.append(f"{mileage} miles")

    if extra_bits:
        suffix = " | ".join(extra_bits[:3])
        if suffix.lower() not in title.lower():
            title = f"{title} | {suffix}" if title else suffix
    return title.strip()


def _extract_best_image_url(row: Dict[str, Any]) -> str:
    """Extract image URL from common Motors payload fields."""
    direct_candidates = [
        row.get("ImageUrl"),
        row.get("ImageURL"),
        row.get("imageUrl"),
        row.get("imageURL"),
        row.get("MainImageUrl"),
        row.get("mainImageUrl"),
        row.get("mainImage"),
        row.get("HeroImage"),
        row.get("heroImage"),
        row.get("PrimaryImageUrl"),
        row.get("primaryImageUrl"),
        row.get("ThumbnailUrl"),
        row.get("thumbnailUrl"),
        row.get("thumbnail"),
        row.get("Image"),
        row.get("image"),
        row.get("PosterImage"),
        row.get("posterImage"),
    ]
    for c in direct_candidates:
        if isinstance(c, str) and c.strip():
            n = _normalise_image_url(c.strip())
            if n and not _is_placeholder_image_url(n):
                return n

    imgs = row.get("Images") or row.get("images") or row.get("Photos") or row.get("photos") or []
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, str):
            n = _normalise_image_url(first)
            if n and not _is_placeholder_image_url(n):
                return n
        if isinstance(first, dict):
            for k in ("url", "src", "href", "large", "medium", "small"):
                v = first.get(k)
                if isinstance(v, str) and v.strip():
                    n = _normalise_image_url(v.strip())
                    if n and not _is_placeholder_image_url(n):
                        return n
    # Fallback: scan nested payload for any obvious image URL field.
    stack: list[Any] = [row]
    visited = 0
    while stack and visited < 600:
        node = stack.pop()
        visited += 1
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if isinstance(v, str):
                    sv = v.strip()
                    if not sv:
                        continue
                    if (
                        ("image" in lk or "photo" in lk or "thumb" in lk)
                        and ("http" in sv or sv.startswith("//") or sv.startswith("/"))
                    ):
                        n = _normalise_image_url(sv)
                        if n and not _is_placeholder_image_url(n):
                            return n
                    if (
                        ("cdn.images.autoexposure.co.uk" in sv)
                        or ("cdn.motors.co.uk" in sv and ("jpg" in sv.lower() or "webp" in sv.lower()))
                    ):
                        n = _normalise_image_url(sv)
                        if n and not _is_placeholder_image_url(n):
                            return n
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list)):
                    stack.append(v)
                elif isinstance(v, str):
                    sv = v.strip()
                    if not sv:
                        continue
                    if (
                        ("cdn.images.autoexposure.co.uk" in sv)
                        or ("cdn.motors.co.uk" in sv and ("jpg" in sv.lower() or "webp" in sv.lower()))
                    ):
                        n = _normalise_image_url(sv)
                        if n and not _is_placeholder_image_url(n):
                            return n
    return ""


def _slice_balanced_brackets(text: str, start_idx: int) -> Optional[str]:
    """Return bracket-balanced slice starting at '[' or None."""
    if start_idx < 0 or start_idx >= len(text) or text[start_idx] != "[":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]
    return None


def _parse_store_vehicles(
    html: str,
    price_min: int,
    price_max: int,
    seen_ids: set,
    *,
    make: str,
    model: str,
) -> List[Listing]:
    """
    Parse legacy Motors Store payload with `vehicles: [...]` inline in HTML.

    This path targets the currently-served generic landing pages where card DOM
    parsing yields no listings but the data payload is still present in scripts.
    """
    marker = "vehicles:"
    rows: List[Dict[str, Any]] = []
    search_from = 0
    while True:
        idx = html.find(marker, search_from)
        if idx < 0:
            break
        arr_start = html.find("[", idx)
        arr_text = _slice_balanced_brackets(html, arr_start)
        if arr_text:
            try:
                parsed = json.loads(arr_text)
                if isinstance(parsed, list):
                    for row in parsed:
                        if isinstance(row, dict):
                            rows.append(row)
            except Exception:
                pass
        search_from = idx + len(marker)
    if not rows:
        return []

    out: List[Listing] = []
    mk = (make or "").strip().lower()
    mdl = (model or "").strip().lower()
    for row in rows:
        title = str(
            row.get("Variant")
            or row.get("Title")
            or row.get("DisplayName")
            or row.get("Derivative")
            or ""
        ).strip()
        if not title:
            continue
        title_l = title.lower()
        make_hint = str(
            row.get("Make") or row.get("Manufacturer") or row.get("Brand") or ""
        ).strip().lower()
        model_hint = str(row.get("Model") or row.get("ModelName") or "").strip().lower()
        if mk and mk not in title_l and mk not in make_hint:
            continue
        if mdl and mdl not in title_l and mdl not in model_hint:
            continue
        price = _coerce_price(
            row.get("GBPPrice")
            or row.get("GBPPriceIncVAT")
            or row.get("GBPOriginalPrice")
            or row.get("Price")
            or 0
        )
        if price <= 0 or price < price_min or price > price_max:
            continue
        detail = str(
            row.get("DetailsPageUrl")
            or row.get("Url")
            or row.get("url")
            or ""
        ).strip()
        if not detail:
            continue
        url = detail if detail.startswith("http") else f"https://www.motors.co.uk{detail}"
        raw_id_match = re.search(r"(\d{5,})", url)
        raw_id = raw_id_match.group(1) if raw_id_match else f"{abs(hash(url)):x}"
        item_id = f"motors_{raw_id}"
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        mileage: Optional[int] = None
        mil_raw = str(row.get("Mileage") or "").strip()
        if mil_raw:
            try:
                mileage = int(re.sub(r"[^\d]", "", mil_raw))
            except Exception:
                mileage = None
        year: Optional[int] = None
        year_raw = str(row.get("RegistrationYear") or "").strip()
        if year_raw:
            try:
                year = int(year_raw[:4])
            except Exception:
                year = None

        dealer = row.get("Dealer") or {}
        location = ""
        if isinstance(dealer, dict):
            location = str(
                dealer.get("Town")
                or dealer.get("City")
                or dealer.get("Postcode")
                or dealer.get("Name")
                or ""
            ).strip()
        guess = guess_make_model(title)
        fuel_type = str(row.get("FuelType") or guess.fuel_type or "").lower()
        out.append(
            Listing(
                platform="motors",
                item_id=item_id,
                title=title,
                price_gbp=price,
                url=url,
                location=location,
                condition="Used",
                vrm=str(row.get("Registration") or "").strip(),
                raw={"store_vehicle": row},
                fuel_type=fuel_type,
                year=year or guess.year,
                mileage=mileage or guess.mileage,
                ulez_compliant=is_ulez_compliant(year or guess.year, fuel_type),
            )
        )
        if len(out) >= 80:
            break
    return out


def _parse_anchor_context(
    html: str,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """
    Broad fallback parser for Motors pages where card selectors drift.

    Extracts anchors containing /used-cars/ then infers title/price from nearby
    parent context text. This keeps static parsing useful and avoids expensive
    Playwright fallback on simple markup shifts.
    """
    if not _BS4_AVAILABLE:
        return []
    soup = BeautifulSoup(html, "html.parser")
    listings: List[Listing] = []

    anchors = soup.find_all("a", href=re.compile(r"/used-cars/", re.I))
    for a in anchors:
        href = str(a.get("href") or "").strip()
        if not href:
            continue
        url = href if href.startswith("http") else f"https://www.motors.co.uk{href}"
        # Require a numeric id-like token in URL to avoid category/filter links.
        if not re.search(r"\d{4,}", url):
            continue
        if not _is_probable_vehicle_detail_url(url):
            continue

        container = a.find_parent(["article", "li", "section", "div"]) or a
        context_text = container.get_text(" ", strip=True)[:900]
        price_match = re.search(r"£\s?([\d,]+)", context_text)
        if not price_match:
            continue
        try:
            price = float(price_match.group(1).replace(",", ""))
        except Exception:
            continue
        if price < price_min or price > price_max:
            continue

        title = a.get_text(" ", strip=True)[:180]
        if not title or len(title) < 8 or _is_junk_title(title):
            h = container.find(["h1", "h2", "h3", "h4"])
            title = h.get_text(" ", strip=True)[:180] if h else ""
        if not title or len(title) < 8 or _is_junk_title(title):
            continue

        id_match = re.search(r"(\d{4,})", url)
        raw_id = id_match.group(1) if id_match else f"{abs(hash(url)):x}"
        item_id = f"motors_{raw_id}"
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        guess = guess_make_model(title)
        listing = Listing(
            platform="motors",
            item_id=item_id,
            title=title,
            price_gbp=price,
            url=url,
            location="",
            condition="Used",
            vrm="",
            raw={"anchor_context": context_text[:260]},
            fuel_type=guess.fuel_type,
            year=guess.year,
            mileage=guess.mileage,
            ulez_compliant=is_ulez_compliant(guess.year, guess.fuel_type),
        )
        listings.append(listing)
        if len(listings) >= 80:
            break
    return listings


def _extract_next_data(html: str) -> Optional[Dict[str, Any]]:
    """
    Extract the __NEXT_DATA__ JSON blob embedded in Next.js pages.

    Motors.co.uk is a Next.js app; most pages embed their initial props
    as a JSON blob in a <script id="__NEXT_DATA__"> tag.

    Returns:
        Parsed dict or None if not found / invalid JSON.
    """
    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_next_data(
    data: Dict[str, Any],
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """
    Navigate the __NEXT_DATA__ structure to find vehicle listings.

    The structure varies across Motors.co.uk page types. We try multiple
    known paths and fall back to a recursive search for any list of 3+
    dicts that contain a price-like field.

    Args:
        data:       Parsed __NEXT_DATA__ dict.
        price_min:  Min price filter.
        price_max:  Max price filter.
        seen_ids:   Deduplication set (mutated).

    Returns:
        List[Listing] extracted from the JSON, or [] if structure not found.
    """
    vehicles = None
    try:
        page_props = data.get("props", {}).get("pageProps", {})

        # Try all known paths for vehicles array (Motors.co.uk Next.js structure
        # has changed several times — try all known variants).
        def _get_nested(d, *keys):
            """Safe nested dict traversal."""
            for k in keys:
                if not isinstance(d, dict):
                    return None
                d = d.get(k)
            return d

        sr = page_props.get("searchResults") or {}
        res = page_props.get("results") or {}
        dat = page_props.get("data") or {}
        init = page_props.get("initialProps") or {}
        # Also check dehydratedState (TanStack Query / React Query pattern)
        deh = _get_nested(page_props, "dehydratedState", "queries") or []
        deh_data = []
        for q in deh:
            qd = _get_nested(q, "state", "data")
            if isinstance(qd, dict):
                for k in ("vehicles", "listings", "adverts", "cars", "results"):
                    if isinstance(qd.get(k), list) and qd[k]:
                        deh_data = qd[k]
                        break
            if deh_data:
                break

        candidates = [
            sr.get("vehicles"),
            sr.get("listings"),
            sr.get("adverts"),
            sr.get("cars"),
            sr.get("results"),
            page_props.get("vehicles"),
            page_props.get("listings"),
            page_props.get("adverts"),
            page_props.get("cars"),
            res.get("vehicles"),
            res.get("listings"),
            dat.get("vehicles"),
            dat.get("listings"),
            init.get("vehicles"),
            init.get("listings"),
            deh_data or None,
        ]
        for c in candidates:
            if c and isinstance(c, list) and len(c) > 0:
                vehicles = c
                break

        # Fallback: recursively search for a list of dicts with price-like keys
        if not vehicles:
            vehicles = _find_vehicles_recursive(page_props, depth=0, max_depth=4)

    except (AttributeError, TypeError):
        return []

    if not vehicles or not isinstance(vehicles, list):
        return []

    listings: List[Listing] = []
    for vehicle in vehicles:
        if not isinstance(vehicle, dict):
            continue
        listing = _vehicle_dict_to_listing(vehicle, price_min, price_max, seen_ids)
        if listing is not None:
            listings.append(listing)

    return listings


def _find_vehicles_recursive(
    obj: Any,
    depth: int,
    max_depth: int,
) -> Optional[list]:
    """
    Recursively search a nested dict/list structure for a list of vehicle-like
    dicts (those containing price and title/make fields).

    Returns the first matching list found, or None.
    """
    if depth > max_depth:
        return None
    if isinstance(obj, list) and len(obj) >= 2:
        # Check if this looks like a vehicle list
        sample = obj[0] if obj else {}
        if isinstance(sample, dict):
            has_price = any(k in sample for k in ("price", "askingPrice", "displayPrice", "priceGbp"))
            has_title = any(k in sample for k in ("title", "displayTitle", "name", "make", "adTitle"))
            if has_price and has_title:
                return obj
    if isinstance(obj, dict):
        for v in obj.values():
            result = _find_vehicles_recursive(v, depth + 1, max_depth)
            if result is not None:
                return result
    return None


def _vehicle_dict_to_listing(
    v: Dict[str, Any],
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> Optional[Listing]:
    """
    Convert a Motors.co.uk vehicle dict (from __NEXT_DATA__) to a Listing.

    Common field names seen in Motors.co.uk JSON:
        id / adId / listingId
        title / displayTitle
        price / askingPrice / displayPrice
        mileage / odometerReading
        year / registrationYear / modelYear
        location / town / dealerLocation
        url / adUrl
        images[0].url / heroImage

    Args:
        v:          Raw vehicle dict from JSON.
        price_min:  Min price (GBP) — listing skipped if below.
        price_max:  Max price (GBP) — listing skipped if above.
        seen_ids:   Deduplication set.

    Returns:
        Listing or None if invalid/duplicate/out-of-range.
    """
    # --- ID ---
    raw_id = str(
        v.get("id") or v.get("adId") or v.get("listingId") or v.get("vehicleId") or ""
    ).strip()
    if not raw_id:
        return None
    item_id = f"motors_{raw_id}"
    if item_id in seen_ids:
        return None

    # --- Title ---
    title = str(
        v.get("title") or v.get("displayTitle") or v.get("name") or ""
    ).strip()
    if not title:
        # Try to construct from make/model/year
        make  = str(v.get("make") or "").strip()
        model = str(v.get("model") or "").strip()
        year  = str(v.get("year") or v.get("registrationYear") or "").strip()
        title = " ".join(filter(None, [year, make, model]))
    if not title:
        return None

    # --- Price ---
    price_raw = (
        v.get("price")
        or v.get("askingPrice")
        or v.get("displayPrice")
        or v.get("priceGbp")
        or 0
    )
    price = _coerce_price(price_raw)
    if price <= 0 or price < price_min or price > price_max:
        return None

    # --- Year ---
    year_raw = v.get("year") or v.get("registrationYear") or v.get("modelYear")
    year: Optional[int] = None
    if year_raw:
        try:
            year = int(str(year_raw)[:4])
        except (ValueError, TypeError):
            pass

    # --- Mileage ---
    mileage_raw = v.get("mileage") or v.get("odometerReading") or v.get("miles")
    mileage: Optional[int] = None
    if mileage_raw:
        try:
            mileage = int(re.sub(r"[^\d]", "", str(mileage_raw)))
        except (ValueError, TypeError):
            pass

    # --- Location ---
    location = str(
        v.get("location") or v.get("town") or v.get("dealerLocation") or ""
    ).strip()

    # --- URL ---
    url = str(v.get("url") or v.get("adUrl") or v.get("listingUrl") or "").strip()
    if url and url.startswith("/"):
        url = "https://www.motors.co.uk" + url

    # --- Image ---
    image_url = _extract_best_image_url(v)

    # --- Fuel type ---
    fuel_type = str(v.get("fuelType") or v.get("fuel") or "").strip().lower()

    # --- Build Listing ---
    seen_ids.add(item_id)

    guess = guess_make_model(title)
    if not year:
        year = guess.year
    if not mileage:
        mileage = guess.mileage
    if not fuel_type:
        fuel_type = guess.fuel_type

    vrm = ""
    vrm_result = _scan_patterns(title.upper(), SAFE_VRM_PATTERNS)
    if vrm_result:
        candidate, _, conf = vrm_result
        candidate = normalise_vrm(candidate)
        if candidate and looks_plausible_uk_vrm(candidate):
            vrm = candidate

    listing = Listing(
        platform="motors",
        item_id=item_id,
        title=title,
        price_gbp=price,
        url=url,
        location=location,
        condition="Used",
        vrm=vrm,
        raw=v,
        fuel_type=fuel_type,
        year=year,
        mileage=mileage,
        ulez_compliant=is_ulez_compliant(year, fuel_type),
        first_image_url=_prefer_non_placeholder_image(image_url, url),
    )

    if vrm:
        listing.vrm_source = "regex_title_motors"
        listing.vrm_confidence = 0.85

    return listing


def _parse_bs4(
    html: str,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """
    Parse Motors.co.uk search results using BeautifulSoup.

    This is the first fallback when __NEXT_DATA__ is absent or empty.
    Looks for common listing card patterns in the HTML.

    Args:
        html:       Raw HTML page content.
        price_min:  Min price filter.
        price_max:  Max price filter.
        seen_ids:   Deduplication set.

    Returns:
        List[Listing] or [] if no cards found.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings: List[Listing] = []

    # Listing card selectors — ordered from most to least specific.
    # Motors.co.uk has changed markup several times; try all known variants.
    card_selectors = [
        # data-attribute patterns (2022–2024)
        "article[data-vehicle-id]",
        "div[data-vehicle-id]",
        "div[data-ad-id]",
        "li[data-vehicle-id]",
        # data-testid patterns (2024–2025 React refactor)
        "[data-testid='vehicle-card']",
        "[data-testid='listing-card']",
        "[data-testid='search-result-card']",
        # BEM / component class names
        "[class*='VehicleCard']",
        "[class*='vehicle-card']",
        "[class*='ListingCard']",
        "[class*='listing-card']",
        "[class*='SearchResult']",
        "[class*='search-result']",
        "[class*='AdCard']",
        "[class*='ad-card']",
        # Current Motors card wrappers
        ".result-card",
        ".result-card___wrap",
        ".result-card__link",
        # Generic article fallback (grab all, filter by link pattern below)
        "article",
    ]

    cards = []
    for selector in card_selectors:
        cards = soup.select(selector)
        if cards:
            break

    for card in cards:
        listing = _bs4_card_to_listing(card, price_min, price_max, seen_ids)
        if listing is not None:
            listings.append(listing)

    return listings


def _bs4_card_to_listing(
    card,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> Optional[Listing]:
    """
    Convert a BeautifulSoup Tag (listing card) to a Listing.

    Attempts to extract from common Motors.co.uk card structure:
      - data-vehicle-id or data-ad-id attribute -> item_id
      - <a> with href -> URL
      - heading text -> title
      - price element -> price
      - spec elements -> year, mileage, location

    Returns:
        Listing or None.
    """
    # ID from data attribute
    raw_id = (
        card.get("data-vehicle-id")
        or card.get("data-ad-id")
        or card.get("data-listing-id")
        or ""
    )
    item_id = f"motors_{raw_id}" if raw_id else ""

    # URL
    url = ""
    link = card.find("a", href=re.compile(r"/used-cars/"))
    if not link:
        link = card.find("a", href=re.compile(r"/car-details/"))
    if not link:
        link = card.find("a", href=re.compile(r"/cars/"))
    if not link:
        link = card.find("a", href=re.compile(r"/motors/"))
    if not link:
        link = card.find("a", href=re.compile(r"motors\.co\.uk"))
    if not link:
        link = card.find("a", href=True)
    if link:
        href = link.get("href", "")
        url = "https://www.motors.co.uk" + href if href.startswith("/") else href

    if not url:
        return None

    # Deduplicate
    url_key = url.split("?")[0].rstrip("/")
    if not item_id:
        item_id = f"motors_{abs(hash(url_key)):x}"
    if item_id in seen_ids:
        return None

    # Title
    title = ""
    for heading_tag in ["h2", "h3", "h4"]:
        h = card.find(heading_tag)
        if h:
            title = h.get_text(" ", strip=True)[:200]
            break
    if _is_junk_title(title):
        return None
    if not title:
        title = card.get_text(" ", strip=True)[:100]

    # Price
    price_text = ""
    price_el = card.find(class_=re.compile(r"price", re.I))
    if price_el:
        price_text = price_el.get_text(strip=True)
    if not price_text:
        # Fallback: search for £ in text
        price_match = re.search(r"£\s?([\d,]+)", card.get_text())
        if price_match:
            price_text = price_match.group(0)
    price = _coerce_price(price_text)

    if price <= 0 or price < price_min or price > price_max:
        return None

    # Year
    year: Optional[int] = None
    year_match = re.search(r"\b(19[6-9]\d|20[0-3]\d)\b", card.get_text())
    if year_match:
        try:
            year = int(year_match.group(1))
        except ValueError:
            pass

    # Mileage (Sprint 8: use shared helper covering k-shorthand)
    mileage: Optional[int] = _parse_mileage_from_text(card.get_text())

    # Location
    location = ""
    loc_el = card.find(class_=re.compile(r"location|dealer|town", re.I))
    if loc_el:
        location = loc_el.get_text(strip=True)[:80]

    # Image
    image_url = ""
    img = card.find("img")
    if img:
        image_url = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or ((img.get("srcset") or "").split(",")[0].strip().split(" ")[0] if img.get("srcset") else "")
            or ((img.get("data-srcset") or "").split(",")[0].strip().split(" ")[0] if img.get("data-srcset") else "")
        )

    seen_ids.add(item_id)
    guess = guess_make_model(title)
    fuel_type = guess.fuel_type
    if not year:
        year = guess.year
    if not mileage:
        mileage = guess.mileage

    vrm = ""
    vrm_result = _scan_patterns(title.upper(), SAFE_VRM_PATTERNS)
    if vrm_result:
        candidate, _, conf = vrm_result
        candidate = normalise_vrm(candidate)
        if candidate and looks_plausible_uk_vrm(candidate):
            vrm = candidate

    listing = Listing(
        platform="motors",
        item_id=item_id,
        title=title,
        price_gbp=price,
        url=url,
        location=location,
        condition="Used",
        vrm=vrm,
        raw={"html_card": card.get_text(" ", strip=True)[:500]},
        fuel_type=fuel_type,
        year=year,
        mileage=mileage,
        ulez_compliant=is_ulez_compliant(year, fuel_type),
        first_image_url=_prefer_non_placeholder_image(image_url, url),
    )

    if vrm:
        listing.vrm_source = "regex_title_motors"
        listing.vrm_confidence = 0.85

    return listing


def _parse_regex(
    html: str,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """
    Last-resort regex extraction from Motors.co.uk HTML.

    Looks for JSON-like price patterns and ad-id anchors in the raw HTML
    to extract minimal listing data when structured parsing fails.

    This is an intentionally simple fallback that produces fewer and less
    complete listings than the JSON/BS4 paths, but is resilient to markup
    changes.

    Args:
        html:       Raw HTML page content.
        price_min:  Min price filter.
        price_max:  Max price filter.
        seen_ids:   Deduplication set.

    Returns:
        List[Listing] (often empty or sparse if this fallback is reached).
    """
    listings: List[Listing] = []

    # Look for patterns like: "adId":"12345678","title":"2019 Honda Jazz..."
    # or data-vehicle-id="12345678" href="/used-cars/..."
    pattern = re.compile(
        r'"(?:adId|vehicleId|listingId)"\s*:\s*"?(\d+)"?'
        r'.*?"(?:title|displayTitle)"\s*:\s*"([^"]{10,200})"'
        r'.*?"(?:price|askingPrice)"\s*:\s*(\d+)',
        re.DOTALL,
    )

    for m in pattern.finditer(html):
        raw_id = m.group(1)
        title  = m.group(2)
        price  = float(m.group(3))

        item_id = f"motors_{raw_id}"
        if item_id in seen_ids:
            continue
        if price < price_min or price > price_max:
            continue

        url = f"https://www.motors.co.uk/car-details/{raw_id}/"
        seen_ids.add(item_id)

        guess = guess_make_model(title)
        listing = Listing(
            platform="motors",
            item_id=item_id,
            title=title,
            price_gbp=price,
            url=url,
            location="",
            condition="Used",
            vrm="",
            raw={"regex_match": m.group(0)[:200]},
            fuel_type=guess.fuel_type,
            year=guess.year,
            mileage=guess.mileage,
            ulez_compliant=is_ulez_compliant(guess.year, guess.fuel_type),
        )
        listings.append(listing)

    return listings


def _parse_json_ld(
    html: str,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """Parse listing candidates from JSON-LD scripts."""
    listings: List[Listing] = []
    scripts = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    )
    for raw in scripts:
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            # Try list-like structures first
            item_list = node.get("itemListElement") or node.get("items") or []
            if isinstance(item_list, list):
                for item in item_list:
                    entity = item.get("item") if isinstance(item, dict) else None
                    if isinstance(entity, dict):
                        listing = _jsonld_entity_to_listing(entity, price_min, price_max, seen_ids)
                        if listing:
                            listings.append(listing)
            # Also allow single Product/Vehicle-like node
            listing = _jsonld_entity_to_listing(node, price_min, price_max, seen_ids)
            if listing:
                listings.append(listing)
    return listings


def _jsonld_entity_to_listing(
    entity: Dict[str, Any],
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> Optional[Listing]:
    """Convert JSON-LD entity dict into Listing when possible."""
    title = str(entity.get("name") or entity.get("title") or "").strip()
    url = str(entity.get("url") or "").strip()
    if url.startswith("/"):
        url = "https://www.motors.co.uk" + url
    offers = entity.get("offers") or {}
    price_raw = (
        entity.get("price")
        or (offers.get("price") if isinstance(offers, dict) else 0)
        or 0
    )
    price = _coerce_price(price_raw)
    if not title or not url or price <= 0 or price < price_min or price > price_max:
        return None
    raw_id = ""
    id_match = re.search(r"(\d{5,})", url)
    if id_match:
        raw_id = id_match.group(1)
    if not raw_id:
        raw_id = f"{abs(hash(url.split('?')[0])):x}"
    item_id = f"motors_{raw_id}"
    if item_id in seen_ids:
        return None
    seen_ids.add(item_id)
    # Sprint 12: extract structured year, mileage, and gallery images from JSON-LD.
    image_url = ""
    extra_imgs: list[str] = []
    img = entity.get("image")
    if isinstance(img, str):
        image_url = img
    elif isinstance(img, list) and img:
        image_url = str(img[0])
        extra_imgs = [str(i) for i in img[1:5] if i]

    # Year from structured fields (vehicleModelDate / modelDate / productionDate)
    _jld_year: Optional[int] = None
    for _yr_key in ("vehicleModelDate", "modelDate", "productionDate"):
        _yr_raw = entity.get(_yr_key)
        if _yr_raw:
            try:
                _yr_val = int(str(_yr_raw)[:4])
                if 1990 <= _yr_val <= 2030:
                    _jld_year = _yr_val
                    break
            except (ValueError, TypeError):
                pass

    # Mileage from mileageFromOdometer (schema.org QuantitativeValue or bare number)
    _jld_mileage: Optional[int] = None
    _odometer = entity.get("mileageFromOdometer")
    if isinstance(_odometer, dict):
        _odo_val = _odometer.get("value") or _odometer.get("@value")
        _odo_unit = str(
            _odometer.get("unitCode") or _odometer.get("unitText") or "mi"
        ).lower()
        try:
            _raw_mi = float(str(_odo_val).replace(",", ""))
            if "km" in _odo_unit or "kmt" in _odo_unit:
                _raw_mi = _raw_mi / 1.60934
            _jld_mileage_candidate = int(_raw_mi)
            if 100 <= _jld_mileage_candidate <= 400_000:
                _jld_mileage = _jld_mileage_candidate
        except (ValueError, TypeError):
            pass
    elif isinstance(_odometer, (int, float)):
        if 100 <= int(_odometer) <= 400_000:
            _jld_mileage = int(_odometer)
    elif isinstance(_odometer, str):
        _jld_mileage = _parse_mileage_from_text(_odometer)

    guess = guess_make_model(title)
    final_year = _jld_year if _jld_year is not None else guess.year
    final_mileage = _jld_mileage if _jld_mileage is not None else guess.mileage
    return Listing(
        platform="motors",
        item_id=item_id,
        title=title,
        price_gbp=price,
        url=url,
        location="",
        condition="Used",
        vrm="",
        raw={"jsonld": entity},
        fuel_type=guess.fuel_type,
        year=final_year,
        mileage=final_mileage,
        ulez_compliant=is_ulez_compliant(final_year, guess.fuel_type),
        first_image_url=_prefer_non_placeholder_image(image_url, url),
        extra_image_urls=",".join(extra_imgs),
    )


def _parse_href_price(
    html: str,
    price_min: int,
    price_max: int,
    seen_ids: set,
) -> List[Listing]:
    """
    Parse listing URLs by pairing used-cars links with nearby prices.
    """
    listings: List[Listing] = []
    # Capture url + nearby text fragment where price commonly appears.
    for m in re.finditer(
        r'href=["\']([^"\']*?/(?:used-cars|cars)/[^"\']+)["\'][^>]*>(.*?)</a>',
        html,
        re.I | re.S,
    ):
        href = m.group(1)
        fragment = re.sub(r"<[^>]+>", " ", m.group(2))
        price_m = re.search(r"£\s?([\d,]+)", fragment)
        if not price_m:
            # scan a short window after the link for a price token
            tail = html[m.end(): m.end() + 500]
            price_m = re.search(r"£\s?([\d,]+)", tail)
        if not price_m:
            continue
        try:
            price = float(price_m.group(1).replace(",", ""))
        except Exception:
            continue
        if price < price_min or price > price_max:
            continue
        url = href if href.startswith("http") else f"https://www.motors.co.uk{href}"
        if not _is_probable_vehicle_detail_url(url):
            continue
        title = re.sub(r"\s+", " ", fragment).strip()[:200]
        if _is_junk_title(title):
            continue
        if len(title) < 8:
            title = "Used car listing"
        raw_id = re.sub(r"[^a-zA-Z0-9]", "", url.split("?")[0])[-18:] or f"{abs(hash(url)):x}"
        item_id = f"motors_{raw_id}"
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        guess = guess_make_model(title)
        listings.append(
            Listing(
                platform="motors",
                item_id=item_id,
                title=title,
                price_gbp=price,
                url=url,
                location="",
                condition="Used",
                vrm="",
                raw={"href_fragment": fragment[:200]},
                fuel_type=guess.fuel_type,
                year=guess.year,
                mileage=guess.mileage,
                ulez_compliant=is_ulez_compliant(guess.year, guess.fuel_type),
            )
        )
    return listings


# ---------------------------------------------------------------------------
# Shared price coercion helper
# ---------------------------------------------------------------------------

def _coerce_price(raw) -> float:
    """
    Convert a raw price value (int, float, or string like "£1,200") to float.

    Returns 0.0 if conversion is not possible.
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        cleaned = re.sub(r"[^\d.]", "", raw)
        try:
            return float(cleaned)
        except ValueError:
            pass
    return 0.0

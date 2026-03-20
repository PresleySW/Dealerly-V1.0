"""
dealerly/facebook.py
====================
Sprint 3: Facebook Marketplace automated scraping adapter.

Uses Playwright (sync API) for headless Chrome.  Playwright is an optional
dependency — if it is not installed the adapter degrades gracefully:
  - is_available returns False
  - fetch_listings() returns []

Authentication strategy:
  FB Marketplace requires a logged-in session.  We persist the browser
  session as cookies in ``fb_cookies.json`` (stored alongside the .env file,
  i.e. DATA_DIR from config.py).  Run the setup helper once to log in
  manually and save cookies:

      python -m dealerly.facebook_setup

  The setup function opens a real (non-headless) Chrome window, waits for
  the user to log in, then saves cookies to fb_cookies.json.

Search URL template:
    https://www.facebook.com/marketplace/search/?query={q}&minPrice={min}
        &maxPrice={max}&exact=false

Rate limiting:
    3–5 s random sleep between page requests to avoid bot detection.

Output:
    List[Listing] with platform="facebook", identical in format to eBay
    pipeline output so downstream phases are platform-agnostic.

Depends on:
  - dealerly.ingestion   (BaseIngestionAdapter)
  - dealerly.models      (Listing)
  - dealerly.ebay        (guess_make_model, merge_dedupe)
  - dealerly.vrm         (SAFE_VRM_PATTERNS, _scan_patterns,
                          is_ulez_compliant, looks_plausible_uk_vrm,
                          normalise_vrm)
  - dealerly.config      (DATA_DIR)
  - playwright (optional, sync_api)
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# Optional Playwright import — graceful degradation
# ---------------------------------------------------------------------------

try:
    from playwright.sync_api import sync_playwright, BrowserContext, Page
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

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
from dealerly.config import DATA_DIR

# Path for the saved Facebook session cookies (alongside the .env file)
FB_COOKIES_PATH: Path = DATA_DIR / "fb_cookies.json"

# Rate-limiting range (seconds) between requests
_SLEEP_MIN: float = 3.0
_SLEEP_MAX: float = 5.0

# FB Marketplace search URL template
_FB_SEARCH_URL = (
    "https://www.facebook.com/marketplace/search/"
    "?query={query}&minPrice={min_price}&maxPrice={max_price}&exact=false"
)
_FB_SEARCH_URL_FALLBACK = (
    "https://www.facebook.com/marketplace/search/?query={query}&exact=false"
)
_FB_M_SEARCH_URL = (
    "https://m.facebook.com/marketplace/search/?query={query}"
)

# Selectors used to scrape listing cards from the FB Marketplace page.
# These are fragile and may need updating if FB changes its markup.
_LISTING_CONTAINER_SELECTOR = "[data-testid='marketplace_feed_item']"
_ALT_LISTING_SELECTOR = "div[aria-label='Marketplace item']"


# ---------------------------------------------------------------------------
# FacebookAdapter
# ---------------------------------------------------------------------------

class FacebookAdapter(BaseIngestionAdapter):
    """
    Scrapes Facebook Marketplace using a headless Playwright browser.

    Requires:
      1. playwright Python package installed  (pip install playwright)
      2. playwright browsers installed        (playwright install chromium)
      3. fb_cookies.json in DATA_DIR          (run facebook_setup() once)
    """

    def platform_name(self) -> str:
        return "facebook"

    @property
    def is_available(self) -> bool:
        """
        Return True when Playwright is installed.

        Cookies improve result quality, but we still attempt an unauthenticated
        scrape when cookies are absent so Facebook is not always skipped.
        """
        if not _PLAYWRIGHT_AVAILABLE:
            return False
        return True

    @property
    def unavailable_reason(self) -> str:
        """
        Provide exact reason when adapter is unavailable.
        """
        if not _PLAYWRIGHT_AVAILABLE:
            return (
                "Playwright not installed. Run: "
                "pip install playwright && playwright install chromium"
            )
        if not FB_COOKIES_PATH.exists():
            return (
                f"No cookies file at {FB_COOKIES_PATH}. "
                "Will attempt unauthenticated scrape; for better results run: "
                "python -m dealerly.facebook_setup"
            )
        return ""

    def fetch_listings(
        self,
        queries: list[str],
        price_min: int,
        price_max: int,
        pages: int,
        **kwargs,
    ) -> List[Listing]:
        """
        Search Facebook Marketplace for each query term and return normalised
        Listing objects.

        If Playwright is unavailable or the cookies file is missing, prints
        a helpful setup message and returns an empty list rather than raising.

        Args:
            queries:    Search terms, e.g. ["honda jazz", "toyota yaris"].
            price_min:  Minimum asking price filter (GBP).
            price_max:  Maximum asking price filter (GBP).
            pages:      Number of scroll/load cycles per query (FB uses
                        infinite scroll rather than numbered pages; each
                        cycle attempts one additional scroll).
            **kwargs:   Unused by this adapter (accepted for interface compat).

        Returns:
            Deduplicated List[Listing] with platform="facebook".
        """
        if not _PLAYWRIGHT_AVAILABLE:
            print(
                "  [FB] Playwright not installed. "
                "Install with: pip install playwright && playwright install chromium"
            )
            return []

        if not FB_COOKIES_PATH.exists():
            print(
                f"  [FB] Cookies file not found at {FB_COOKIES_PATH}.\n"
                "  Continuing unauthenticated scrape fallback "
                "(run facebook_setup for higher yield)."
            )

        if _is_asyncio_loop_running():
            print("  [FB] Async loop detected — running Playwright scraper via subprocess...")
            return _fetch_listings_subprocess(queries, price_min, price_max, pages)
        return _fetch_listings_sync(queries, price_min, price_max, pages)


# ---------------------------------------------------------------------------
# Internal scraping helpers
# ---------------------------------------------------------------------------

def _load_context_with_cookies(browser) -> "BrowserContext":
    """
    Create a new browser context and inject the saved FB session cookies.
    """
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )

    if FB_COOKIES_PATH.exists():
        try:
            cookies = json.loads(FB_COOKIES_PATH.read_text(encoding="utf-8"))
            ctx.add_cookies(cookies)
        except Exception as exc:
            print(f"  [FB] Warning: could not load cookies from {FB_COOKIES_PATH}: {exc}")

    return ctx


def _accept_cookie_prompt(page: "Page") -> None:
    """
    Best-effort cookie prompt dismissal.
    """
    try:
        page.evaluate(
            """
            () => {
              const labels = ['Allow all cookies', 'Accept all', 'Allow essential and optional cookies'];
              const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
              for (const b of buttons) {
                const txt = (b.innerText || b.textContent || '').trim();
                if (labels.some(l => txt.toLowerCase().includes(l.toLowerCase()))) {
                  b.click();
                  return true;
                }
              }
              return false;
            }
            """
        )
    except Exception:
        pass


def _is_asyncio_loop_running() -> bool:
    """
    True when called inside an active asyncio event loop in this thread.
    """
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _fetch_listings_sync(
    queries: list[str],
    price_min: int,
    price_max: int,
    pages: int,
) -> List[Listing]:
    """
    Blocking Playwright scrape implementation.
    """
    all_batches: List[List[Listing]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = _load_context_with_cookies(browser)
        for query in queries:
            try:
                batch = _scrape_query(
                    ctx,
                    query=query,
                    price_min=price_min,
                    price_max=price_max,
                    pages=pages,
                )
                all_batches.append(batch)
                print(f"  [FB] '{query}' -> {len(batch)} listings")
            except Exception as exc:
                print(f"  [FB] Query '{query}' failed: {exc}")
                all_batches.append([])
        browser.close()
    return merge_dedupe(all_batches) if all_batches else []


def _fetch_listings_subprocess(
    queries: list[str],
    price_min: int,
    price_max: int,
    pages: int,
) -> List[Listing]:
    """
    Run Playwright scraping in a separate process to avoid host loop conflicts.
    """
    payload = {
        "queries": queries,
        "price_min": int(price_min),
        "price_max": int(price_max),
        "pages": int(pages),
    }
    with tempfile.NamedTemporaryFile(prefix="dealerly_fb_", suffix=".json", delete=False) as tf:
        out_path = tf.name
    try:
        cmd = [
            sys.executable,
            "-m",
            "dealerly.facebook",
            "--subprocess-fetch",
            json.dumps(payload, ensure_ascii=True),
            "--out",
            out_path,
        ]
        env = os.environ.copy()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            print(f"  [FB] Subprocess scrape failed: {err[:220]}")
            return []
        try:
            data = json.loads(Path(out_path).read_text(encoding="utf-8"))
            out = []
            for row in (data.get("listings") or []):
                if isinstance(row, dict):
                    out.append(Listing(**row))
            return out
        except Exception as exc:
            print(f"  [FB] Subprocess output parse failed: {exc}")
            return []
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except Exception:
            pass


def _subprocess_fetch_entry(payload_json: str, out_path: str) -> int:
    """
    CLI entrypoint for subprocess scraping mode.
    """
    try:
        payload = json.loads(payload_json or "{}")
        queries = payload.get("queries") or []
        price_min = int(payload.get("price_min") or 0)
        price_max = int(payload.get("price_max") or 0)
        pages = int(payload.get("pages") or 1)
        rows = _fetch_listings_sync(queries, price_min, price_max, pages)
        blob = {"listings": [asdict(x) for x in rows]}
        Path(out_path).write_text(json.dumps(blob, ensure_ascii=True), encoding="utf-8")
        return 0
    except Exception as exc:
        try:
            Path(out_path).write_text(json.dumps({"error": str(exc)}, ensure_ascii=True), encoding="utf-8")
        except Exception:
            pass
        return 1


def _scrape_query(
    ctx: "BrowserContext",
    query: str,
    price_min: int,
    price_max: int,
    pages: int,
) -> List[Listing]:
    """
    Open a FB Marketplace search page for *query* and collect listings.

    FB uses infinite scroll.  Each "page" cycle scrolls to the bottom and
    waits for new cards to load.  We collect all visible cards after each
    scroll cycle, then deduplicate by URL before returning.

    Args:
        ctx:        Playwright browser context (with cookies already loaded).
        query:      Single search term.
        price_min:  Min price GBP.
        price_max:  Max price GBP.
        pages:      Number of scroll cycles (each loads ~24 extra cards).

    Returns:
        List[Listing] for this query.
    """
    url = _FB_SEARCH_URL.format(
        query=quote_plus(query),
        min_price=price_min,
        max_price=price_max,
    )

    page: "Page" = ctx.new_page()
    listings: List[Listing] = []
    seen_urls: set[str] = set()

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        _accept_cookie_prompt(page)
        # Brief wait for initial marketplace cards to render
        time.sleep(2)

        for cycle in range(max(1, pages)):
            # Extract cards currently visible
            new_listings = _extract_cards(page, seen_urls, price_min, price_max)
            listings.extend(new_listings)

            if cycle < pages - 1:
                # Scroll to bottom to trigger next infinite-scroll load
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))

        if not listings:
            # Public fallback paths when logged-in feed is unavailable.
            for alt in (
                _FB_SEARCH_URL_FALLBACK.format(query=quote_plus(query)),
                _FB_M_SEARCH_URL.format(query=quote_plus(query)),
            ):
                try:
                    page.goto(alt, wait_until="domcontentloaded", timeout=30_000)
                    _accept_cookie_prompt(page)
                    time.sleep(2)
                    for _ in range(max(2, pages + 1)):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        time.sleep(1.2)
                        new_listings = _extract_cards(page, seen_urls, price_min, price_max)
                        if new_listings:
                            listings.extend(new_listings)
                            break
                    if listings:
                        break
                except Exception:
                    continue

    except Exception as exc:
        print(f"  [FB] Page scrape error for '{query}': {exc}")
    finally:
        page.close()

    return listings


def _extract_cards(
    page: "Page",
    seen_urls: set,
    price_min: int,
    price_max: int,
) -> List[Listing]:
    """
    Extract all listing cards currently rendered on the page.

    Tries the data-testid selector first, falls back to aria-label selector.
    Parses each card for title, price, location, image URL, and listing URL.

    Args:
        page:       Active Playwright page.
        seen_urls:  Set of URLs already collected (mutated in place).
        price_min:  Listings below this price are skipped.
        price_max:  Listings above this price are skipped.

    Returns:
        New Listing objects not previously seen on this page.
    """
    new_listings: List[Listing] = []

    # Try to extract listing data from the page via JavaScript evaluation.
    # This approach is more robust than relying on specific CSS selectors
    # which change frequently in FB's compiled markup.
    raw_cards = page.evaluate("""
        () => {
            const results = [];
            // Try multiple selector strategies
            const selectors = [
                '[data-testid="marketplace_feed_item"]',
                'div[aria-label="Marketplace item"]',
                'a[href*="/marketplace/item/"]',
            ];

            let anchors = [];
            for (const sel of selectors) {
                const found = document.querySelectorAll(sel);
                if (found.length > 0) {
                    // If we matched anchors directly, use them
                    if (sel.startsWith('a')) {
                        anchors = Array.from(found);
                    } else {
                        // Otherwise find anchors within the containers
                        for (const el of found) {
                            const a = el.querySelector('a[href*="/marketplace/item/"]');
                            if (a) anchors.push(a);
                        }
                    }
                    if (anchors.length > 0) break;
                }
            }

            for (const anchor of anchors) {
                try {
                    const href = anchor.href || '';
                    const img = anchor.querySelector('img');
                    const spans = anchor.querySelectorAll('span');
                    const texts = Array.from(spans).map(s => s.textContent.trim()).filter(Boolean);

                    results.push({
                        url: href,
                        image: img ? img.src : '',
                        texts: texts,
                    });
                } catch (e) {}
            }
            return results;
        }
    """)

    for card in (raw_cards or []):
        listing = _card_to_listing(card, seen_urls, price_min, price_max)
        if listing is not None:
            new_listings.append(listing)

    return new_listings


def _card_to_listing(
    card: dict,
    seen_urls: set,
    price_min: int,
    price_max: int,
) -> Optional[Listing]:
    """
    Convert a raw card dict (from JS evaluation) into a Listing.

    Returns None if the listing is invalid, already seen, or outside the
    price range.

    Card dict keys:
        url    — listing URL (may be relative /marketplace/item/...)
        image  — hero image src URL
        texts  — list of visible text spans from the card

    Args:
        card:       Raw dict from the JS extractor.
        seen_urls:  Set of already-processed URLs (mutated in place).
        price_min:  Minimum acceptable price.
        price_max:  Maximum acceptable price.

    Returns:
        Listing or None.
    """
    url = (card.get("url") or "").strip()
    if not url:
        return None

    # Normalise relative URLs
    if url.startswith("/"):
        url = "https://www.facebook.com" + url

    # Strip query params after the item ID to aid deduplication
    url_key = url.split("?")[0].rstrip("/")
    if url_key in seen_urls:
        return None
    seen_urls.add(url_key)

    texts = card.get("texts") or []
    image_url = card.get("image") or ""

    # Parse title — typically the first non-price text span
    title = ""
    price = 0.0
    location = ""

    for text in texts:
        # Price: "£1,200" or "£1200"
        price_match = re.search(r"£\s?([\d,]+)", text)
        if price_match and price == 0.0:
            try:
                price = float(price_match.group(1).replace(",", ""))
            except ValueError:
                pass
            continue

        # Location: short text after price, often town/city name
        if price > 0 and not location and len(text) < 60:
            # Skip obvious non-location strings
            if not re.match(r"^[\d£]", text):
                location = text
            continue

        # Title: the longest meaningful text span
        if len(text) > len(title) and len(text) > 5:
            title = text[:200]

    if not title or price <= 0:
        return None

    if price < price_min or price > price_max:
        return None

    # Build item_id from URL path to ensure cross-run stability
    item_id_match = re.search(r"/marketplace/item/(\d+)", url)
    item_id = f"fb_{item_id_match.group(1)}" if item_id_match else f"fb_{abs(hash(url_key)):x}"

    # Guard: some FB cards expose only location text and no real title.
    # Keep a neutral fallback title instead of "London, United Kingdom".
    _title_norm = re.sub(r"\s+", " ", title.strip().lower())
    _loc_norm = re.sub(r"\s+", " ", location.strip().lower())
    if _title_norm and (_title_norm == _loc_norm or "united kingdom" in _title_norm):
        alt = ""
        for text in texts:
            t = str(text or "").strip()
            if len(t) < 8:
                continue
            tl = t.lower()
            if "£" in t:
                continue
            if tl == _loc_norm or "united kingdom" in tl:
                continue
            alt = t[:200]
            break
        title = alt or f"Facebook vehicle {item_id}"

    guess = guess_make_model(title)
    fuel = guess.fuel_type
    vrm_raw = ""

    # Attempt VRM extraction from title
    vrm_result = _scan_patterns(title.upper(), SAFE_VRM_PATTERNS)
    if vrm_result:
        candidate, _, conf = vrm_result
        candidate = normalise_vrm(candidate)
        if candidate and looks_plausible_uk_vrm(candidate):
            vrm_raw = candidate

    listing = Listing(
        platform="facebook",
        item_id=item_id,
        title=title,
        price_gbp=price,
        url=url,
        location=location,
        condition="Used",
        vrm=vrm_raw,
        raw={"card": card},
        fuel_type=fuel,
        year=guess.year,
        mileage=guess.mileage,
        ulez_compliant=is_ulez_compliant(guess.year, fuel),
        first_image_url=image_url,
    )

    # FB cards often omit structured mileage/year. Try a text fallback.
    if not listing.mileage:
        blob = " ".join(str(x) for x in texts).replace(",", "").lower()
        m = re.search(r"\b(\d{2,3})(?:\.(\d))?\s*k\s*(?:miles?|mi)?\b", blob)
        if m:
            whole = int(m.group(1))
            frac = int(m.group(2) or "0")
            val = whole * 1000 + frac * 100
            if 1000 <= val <= 400000:
                listing.mileage = val
        if not listing.mileage:
            m2 = re.search(r"\b(\d{4,6})\s*(?:miles?|mi)\b", blob)
            if m2:
                val = int(m2.group(1))
                if 1000 <= val <= 400000:
                    listing.mileage = val

    if vrm_raw:
        listing.vrm_source = "regex_title_fb"
        listing.vrm_confidence = 0.85

    return listing


# ---------------------------------------------------------------------------
# Setup helper — run once to save FB session cookies
# ---------------------------------------------------------------------------

def facebook_setup() -> None:
    """
    Open Facebook in a real (non-headless) browser window and wait for the
    user to log in manually.  Once logged in, saves the session cookies to
    fb_cookies.json so future runs can authenticate without a browser.

    Usage::

        python -m dealerly.facebook_setup

    Or import and call directly::

        from dealerly.facebook import facebook_setup
        facebook_setup()
    """
    if not _PLAYWRIGHT_AVAILABLE:
        print(
            "Playwright is not installed.\n"
            "Install it with:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )
        return

    print("Opening Facebook in a browser window.")
    print(
        "Log in to Facebook in that window. "
        "Setup will auto-save cookies when login is detected.\n"
    )

    with sync_playwright() as pw:
        # Launch a *visible* browser so the user can interact
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")

        # Auto-detect successful login by checking for common authenticated cookies.
        timeout_s = 300
        started = time.time()
        cookies = []
        while (time.time() - started) < timeout_s:
            try:
                cookies = ctx.cookies()
            except Exception:
                cookies = []
            names = {str(c.get("name", "")) for c in cookies}
            if "c_user" in names and "xs" in names:
                break
            time.sleep(2.0)

        # Last snapshot even if auth detection didn't trigger.
        if not cookies:
            try:
                cookies = ctx.cookies()
            except Exception:
                cookies = []

        FB_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        FB_COOKIES_PATH.write_text(
            json.dumps(cookies, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if cookies:
            print(f"\nCookies saved to {FB_COOKIES_PATH}")
            print("You can now run Dealerly with --input-mode facebook")
        else:
            print(
                "\nNo cookies were captured. "
                "Please retry setup in CMD/PowerShell (not Spyder) and complete login."
            )

        browser.close()


# ---------------------------------------------------------------------------
# Module entry point for setup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--subprocess-fetch" and sys.argv[3] == "--out":
        raise SystemExit(_subprocess_fetch_entry(sys.argv[2], sys.argv[4]))
    facebook_setup()

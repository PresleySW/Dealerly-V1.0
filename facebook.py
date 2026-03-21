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

  In Jupyter use: ``%cd`` to the project dir, then ``!python -m dealerly.facebook_setup``
  (see ``dealerly/facebook_setup.py`` docstring).

  The setup function opens a real (non-headless) Chrome window, waits for
  the user to log in, then saves cookies to fb_cookies.json.

  Scrape mode uses headless Chromium by default. Set environment variable
  ``DEALERLY_FB_HEADLESS`` to ``0`` / ``false`` / ``no`` to run a visible browser
  during ``fetch_listings`` — sometimes reduces bot blocks; slower.
  In **PowerShell** use ``$env:DEALERLY_FB_HEADLESS = "0"`` (not ``KEY=value``).
  In **cmd** use ``set DEALERLY_FB_HEADLESS=0``.

Search URL template:
    https://www.facebook.com/marketplace/search/?query={q}&minPrice={min}
        &maxPrice={max}&exact=false

Rate limiting:
    3–5 s random sleep between page requests to avoid bot detection.

Threading:
    Phase 1 runs adapters in a thread pool. Playwright's **sync** API is not
    safe on worker threads — when ``fetch_listings`` is called from a non-main
    thread, scraping runs in a **subprocess** (same as asyncio/Jupyter mode) so
    Chromium always runs on a process main thread.

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
import threading
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
from dealerly.config import DATA_DIR, fb_max_listings

# Path for the saved Facebook session cookies (alongside the .env file)
FB_COOKIES_PATH: Path = DATA_DIR / "fb_cookies.json"


def _fb_headless_from_env() -> bool:
    """Default True; set DEALERLY_FB_HEADLESS=0|false|no|off for visible Chromium."""
    v = os.environ.get("DEALERLY_FB_HEADLESS", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


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
# Sprint 5: per-run quality counters (reset at start of each sync fetch call)
# ---------------------------------------------------------------------------
# Read by pipeline.py Phase 1 after adapter.fetch_listings() returns.
FB_QUALITY: dict = {
    "fb_total": 0,
    "fb_titles_good": 0,
    "fb_mileage_found": 0,
    "fb_thumb_found": 0,
}

# Known make tokens for title quality classification (subset — cheap regex avoided)
_KNOWN_MAKES = frozenset({
    "honda", "toyota", "volkswagen", "vw", "ford", "vauxhall", "nissan",
    "bmw", "mercedes", "audi", "renault", "peugeot", "citroen", "hyundai",
    "kia", "mazda", "seat", "skoda", "fiat", "mitsubishi", "volvo",
    "subaru", "suzuki", "lexus", "land rover", "landrover", "jaguar",
    "jeep", "mini", "dacia", "alfa", "chevrolet", "chrysler", "dodge",
    "porsche", "tesla", "mg", "saab", "isuzu", "smart", "infiniti",
})


def _fb_text_looks_like_place_only(s: str) -> bool:
    """Heuristic: string is likely a location, not a vehicle title."""
    t = (s or "").strip().lower()
    if not t or len(t) < 4:
        return True
    if "united kingdom" in t or t.endswith(", uk"):
        return True
    if re.match(r"^[a-z\s,]+(?:united kingdom|england|scotland|wales)$", t):
        return True
    return False


def _parse_gbp_price_from_texts(*parts: str) -> float:
    """
    Extract the first plausible GBP price from arbitrary FB card text.
    Handles £1,200 · …, £1200, aria-label blobs, GBP 1200.
    """
    blob = " ".join((p or "") for p in parts if p)
    if not blob.strip():
        return 0.0
    for m in re.finditer(r"£\s*([0-9][0-9,\.]*)", blob):
        raw = m.group(1).strip().replace(",", "")
        if not raw:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        if 50.0 <= val <= 500_000.0:
            return val
    m2 = re.search(r"GBP\s*£?\s*([0-9][0-9,]*)", blob, flags=re.I)
    if m2:
        try:
            val = float(m2.group(1).replace(",", ""))
            if 50.0 <= val <= 500_000.0:
                return val
        except ValueError:
            pass
    return 0.0


def _fb_pick_title_from_hints(card: dict, texts: list, location: str, item_id: str) -> str:
    """
    Prefer aria-label / image alt over span soup when FB hides the real title.
    """
    hints: list[str] = []
    for key in ("aria_label", "img_alt", "img_title"):
        v = (card.get(key) or "").strip()
        if v and len(v) > 6:
            hints.append(v)
    loc_norm = re.sub(r"\s+", " ", (location or "").strip().lower())
    for h in hints:
        hn = re.sub(r"\s+", " ", h.lower())
        if loc_norm and hn == loc_norm:
            continue
        if _fb_text_looks_like_place_only(h) and "£" not in h:
            continue
        # aria often starts with "Marketplace listing:" or similar — strip noise
        cleaned = re.sub(
            r"^(marketplace\s*(listing|item)\s*[:\-]\s*)",
            "",
            h,
            flags=re.I,
        ).strip()
        if len(cleaned) > 8 and not _fb_text_looks_like_place_only(cleaned):
            return cleaned[:200]
    # Longest non-location span
    title = ""
    for text in texts:
        tx = str(text or "").strip()
        if len(tx) > len(title) and len(tx) > 5 and not _fb_text_looks_like_place_only(tx):
            title = tx[:200]
    if title:
        return title
    for text in texts:
        tx = str(text or "").strip()
        if len(tx) > len(title) and len(tx) > 5:
            title = tx[:200]
    return title or f"Facebook vehicle {item_id}"


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

        # Playwright sync API is not thread-safe and must not run on ThreadPoolExecutor
        # workers (Phase 1 runs eBay/Motors/FB/PistonHeads in parallel). On a worker
        # thread the browser often fails silently or returns 0 listings — same for
        # active asyncio loops (Spyder/Jupyter). Use a child process (main thread).
        _not_main = threading.current_thread() is not threading.main_thread()
        if _not_main:
            print(
                "  [FB] Isolating Playwright in subprocess (required: Phase 1 uses "
                "parallel threads; Playwright sync API is main-thread only)."
            )
            return _fetch_listings_subprocess(queries, price_min, price_max, pages)
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
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 900},
        locale="en-GB",
        timezone_id="Europe/London",
        color_scheme="light",
        device_scale_factor=1,
        has_touch=False,
        is_mobile=False,
    )

    if FB_COOKIES_PATH.exists():
        try:
            cookies = json.loads(FB_COOKIES_PATH.read_text(encoding="utf-8"))
            ctx.add_cookies(cookies)
        except Exception as exc:
            print(f"  [FB] Warning: could not load cookies from {FB_COOKIES_PATH}: {exc}")

    # Reduce obvious automation signals (no playwright-stealth dependency)
    ctx.add_init_script(
        """
        () => {
          try {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            if (!window.chrome) { window.chrome = { runtime: {} }; }
            const fakePlugins = { length: 3, item: () => null, namedItem: () => null };
            Object.defineProperty(navigator, 'plugins', { get: () => fakePlugins });
          } catch (e) {}
        }
        """
    )

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
    # Sprint 5: reset quality counters for this run
    global FB_QUALITY
    FB_QUALITY = {"fb_total": 0, "fb_titles_good": 0, "fb_mileage_found": 0, "fb_thumb_found": 0}

    all_batches: List[List[Listing]] = []
    with sync_playwright() as pw:
        _hl = _fb_headless_from_env()
        if not _hl:
            print(
                "  [FB] DEALERLY_FB_HEADLESS=0 — visible Chromium "
                "(slower; may reduce headless blocks)"
            )
        browser = pw.chromium.launch(
            headless=_hl,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
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
    result = merge_dedupe(all_batches) if all_batches else []
    FB_QUALITY["fb_total"] = len(result)
    return result


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
            capture_output=False,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if proc.returncode != 0:
            err = ""
            try:
                raw = Path(out_path).read_text(encoding="utf-8")
                err = (json.loads(raw).get("error") or "") if raw.strip() else ""
            except Exception:
                pass
            print(
                f"  [FB] Subprocess scrape failed (exit {proc.returncode}): "
                f"{(err or 'see messages above')[:300]}"
            )
            return []
        try:
            data = json.loads(Path(out_path).read_text(encoding="utf-8"))
            out = []
            for row in (data.get("listings") or []):
                if isinstance(row, dict):
                    out.append(Listing(**row))
            fbq = data.get("fb_quality")
            if isinstance(fbq, dict):
                for k in ("fb_total", "fb_titles_good", "fb_mileage_found", "fb_thumb_found"):
                    if k in fbq:
                        try:
                            FB_QUALITY[k] = int(fbq[k])
                        except (TypeError, ValueError):
                            pass
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
        blob = {
            "listings": [asdict(x) for x in rows],
            "fb_quality": dict(FB_QUALITY),
        }
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
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        # Remove navigator.webdriver flag that exposes automation
        page.evaluate("() => { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }) }")
        _accept_cookie_prompt(page)
        # Wait for SPA to render marketplace cards (FB hydration can be slow)
        try:
            page.wait_for_selector(
                'a[href*="/marketplace/item/"]',
                timeout=18_000,
            )
        except Exception:
            pass  # fallback: proceed with extraction even without selector match
        time.sleep(random.uniform(1.2, 2.2))
        # Feed often hydrates after scroll — prime the virtualised list
        try:
            for _ in range(5):
                page.evaluate(
                    "window.scrollBy(0, Math.min(520, window.innerHeight * 0.85))"
                )
                time.sleep(0.28)
        except Exception:
            pass

        _low_yield_streak = 0
        _cap = fb_max_listings()
        for cycle in range(max(1, pages)):
            # Sprint 5: cap check — stop if global unique URL count reached
            if len(seen_urls) >= _cap:
                print(f"  [FB] Cap reached ({_cap} unique URLs) — stopping scroll for '{query}'")
                break

            # Extract cards currently visible
            new_listings = _extract_cards(page, seen_urls, price_min, price_max)
            listings.extend(new_listings)

            # Sprint 5: early-exit when yield dries up
            if len(new_listings) < 5:
                _low_yield_streak += 1
                if _low_yield_streak >= 2:
                    print(f"  [FB] Early-exit: <5 new listings in last 2 cycles (cycle {cycle}) for '{query}'")
                    break
            else:
                _low_yield_streak = 0

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

        if not listings:
            try:
                title = page.title()
                snippet = (
                    page.evaluate(
                        "() => (document.body && document.body.innerText) "
                        "? document.body.innerText.slice(0, 700) : ''"
                    )
                    or ""
                )
                low = snippet.lower()
                blocked = any(
                    x in low
                    for x in (
                        "checkpoint",
                        "captcha",
                        "confirm your identity",
                        "log in to facebook",
                        "you must log in",
                        "temporarily blocked",
                    )
                )
                if blocked:
                    print(
                        f"  [FB] Login/block/challenge likely (title={title!r}). "
                        "Refresh session: python -m dealerly.facebook_setup"
                    )
                else:
                    print(
                        f"  [FB] 0 cards parsed for '{query}' (title={title!r}). "
                        "If this persists: run facebook_setup, update Playwright, try "
                        "DEALERLY_FB_HEADLESS=0, or FB may block headless Chrome."
                    )
            except Exception:
                print(f"  [FB] 0 listings for '{query}' — could not read page diagnostics.")

    except Exception as exc:
        print(f"  [FB] Page scrape error for '{query}': {exc}")
    finally:
        page.close()

    return listings


def _title_is_good(title: str) -> bool:
    """True if the title contains a recognisable vehicle make token."""
    t = title.lower()
    return any(make in t for make in _KNOWN_MAKES)


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
    global FB_QUALITY
    new_listings: List[Listing] = []

    # Try to extract listing data from the page via JavaScript evaluation.
    # This approach is more robust than relying on specific CSS selectors
    # which change frequently in FB's compiled markup.
    raw_cards = page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();
            // Primary: every marketplace item link (most stable across FB UI churn)
            const anchors = Array.from(
                document.querySelectorAll('a[href*="/marketplace/item/"]')
            );

            const findCardRoot = (a) => {
                const feed = a.closest('[data-testid="marketplace_feed_item"]');
                if (feed) return feed;
                let el = a.parentElement;
                for (let i = 0; i < 4 && el; i++) {
                    if (el.getAttribute && el.getAttribute('role') === 'article') return el;
                    el = el.parentElement;
                }
                return a.parentElement || a;
            };

            for (const anchor of anchors) {
                try {
                    const href = anchor.href || '';
                    if (!href) continue;
                    const path = href.split('?')[0];
                    if (seen.has(path)) continue;
                    seen.add(path);

                    const cardRoot = findCardRoot(anchor);
                    const img =
                        cardRoot.querySelector('img') || anchor.querySelector('img');
                    const spans = cardRoot.querySelectorAll('span');
                    const texts = Array.from(spans)
                        .map(s => (s.textContent || '').trim())
                        .filter(Boolean);
                    const aria = (anchor.getAttribute('aria-label') || '').trim();
                    const imgAlt = img ? (img.getAttribute('alt') || '').trim() : '';
                    const imgTitle = img ? (img.getAttribute('title') || '').trim() : '';
                    const innerText = (anchor.innerText || '').trim();
                    const fullCard =
                        (cardRoot.innerText || '').slice(0, 1800);

                    results.push({
                        url: href,
                        image: img ? img.src : '',
                        texts: texts,
                        aria_label: aria,
                        img_alt: imgAlt,
                        img_title: imgTitle,
                        anchor_inner_text: innerText,
                        full_card_text: fullCard,
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
            # Sprint 5: quality classification
            if _title_is_good(listing.title):
                FB_QUALITY["fb_titles_good"] = FB_QUALITY.get("fb_titles_good", 0) + 1
            if listing.mileage:
                FB_QUALITY["fb_mileage_found"] = FB_QUALITY.get("fb_mileage_found", 0) + 1
            if listing.first_image_url:
                FB_QUALITY["fb_thumb_found"] = FB_QUALITY.get("fb_thumb_found", 0) + 1

    raw_n = len(raw_cards or [])
    if raw_n > 0 and not new_listings:
        c0 = (raw_cards or [])[0]
        tx = c0.get("texts") or []
        sample_p = _parse_gbp_price_from_texts(
            *(list(tx) if isinstance(tx, list) else []),
            str(c0.get("aria_label") or ""),
            str(c0.get("full_card_text") or "")[:900],
        )
        print(
            f"  [FB] Found {raw_n} item link(s) but 0 in price band "
            f"£{price_min}-£{price_max} (sample extractable £={sample_p}). "
            "Widen capital band or check session/captcha."
        )

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

    # FB often puts £ only in aria-label / full card blob, not in span soup
    if price <= 0:
        price = _parse_gbp_price_from_texts(
            *(texts
              + [
                  str(card.get("aria_label") or ""),
                  str(card.get("img_alt") or ""),
                  str(card.get("img_title") or ""),
                  str(card.get("anchor_inner_text") or ""),
                  str(card.get("full_card_text") or ""),
              ])
        )

    if price <= 0:
        return None

    if price < price_min or price > price_max:
        return None

    # Build item_id from URL path to ensure cross-run stability
    item_id_match = re.search(r"/marketplace/item/(\d+)", url)
    item_id = f"fb_{item_id_match.group(1)}" if item_id_match else f"fb_{abs(hash(url_key)):x}"

    # Title: prefer aria-label / image alt when span soup is only location/price
    title = _fb_pick_title_from_hints(card, texts, location, item_id)

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
        blob = " ".join(str(x) for x in texts)
        for key in ("aria_label", "img_alt", "img_title"):
            blob += " " + str(card.get(key) or "")
        blob = blob.replace(",", "").lower()
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

def _fb_url_suggests_logged_in(url: str) -> bool:
    """True when the browser is past the login screen (feed, marketplace, etc.)."""
    u = (url or "").lower()
    if "facebook.com" not in u:
        return False
    if "/login" in u or "/recover" in u:
        return False
    if (
        "checkpoint" in u
        or "two_factor" in u
        or "two-step" in u
        or "device_based" in u
    ):
        return False
    return True


def _fb_cookie_names_indicate_session(names: set[str]) -> bool:
    """
    Detect logged-in session. Historically we required c_user + xs; Meta
    sometimes delays xs or pairs c_user with fr/sb on newer builds.
    """
    if "c_user" not in names:
        return False
    if "xs" in names:
        return True
    if "fr" in names:
        return True
    if "sb" in names and len(names) >= 4:
        return True
    return False


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
            "  playwright install chromium",
            flush=True,
        )
        return

    print("Opening Facebook in a browser window.", flush=True)
    print(
        "Log in to Facebook in that window.\n"
        "When you see your feed or Marketplace, switch back here and press Enter "
        "to save cookies — or wait for auto-detect.\n",
        flush=True,
    )

    manual_save = threading.Event()

    def _wait_for_enter() -> None:
        try:
            input(">>> Press Enter in this window after you are logged in (browser can stay open). ")
        except (EOFError, KeyboardInterrupt):
            pass
        manual_save.set()

    threading.Thread(target=_wait_for_enter, daemon=True).start()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            page.goto(
                "https://www.facebook.com/login",
                wait_until="domcontentloaded",
            )

            timeout_s = 300
            started = time.time()
            cookies: list = []
            last_hint = started
            away_from_login_streak = 0

            while (time.time() - started) < timeout_s:
                if manual_save.is_set():
                    print("\nSaving cookies (manual).", flush=True)
                    break

                now = time.time()
                if now - last_hint >= 45:
                    print(
                        "Still waiting… If you are already logged in, click this terminal "
                        "and press Enter to save cookies.",
                        flush=True,
                    )
                    last_hint = now

                try:
                    cookies = ctx.cookies()
                except Exception:
                    cookies = []
                names = {str(c.get("name", "")) for c in cookies}

                if _fb_cookie_names_indicate_session(names):
                    print("\nLogin detected (session cookies).", flush=True)
                    break

                try:
                    url = page.url
                except Exception:
                    url = ""
                if (
                    "c_user" in names
                    and _fb_url_suggests_logged_in(url)
                    and "/login" not in url.lower()
                ):
                    away_from_login_streak += 1
                    if away_from_login_streak >= 4:
                        print(
                            "\nLogin detected (you left the login page; saving cookies).",
                            flush=True,
                        )
                        break
                else:
                    away_from_login_streak = 0

                time.sleep(1.0)

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
                _names = {str(c.get("name", "")) for c in cookies}
                print(f"\nCookies saved to {FB_COOKIES_PATH}", flush=True)
                print(
                    f"Cookie names captured: {', '.join(sorted(_names)[:20])}"
                    f"{'…' if len(_names) > 20 else ''}",
                    flush=True,
                )
                if "c_user" not in _names:
                    print(
                        "Warning: expected cookie `c_user` missing — "
                        "session may not work for Marketplace.",
                        flush=True,
                    )
                print(
                    "You can now run Dealerly with input mode 'all' "
                    "(or CLI --input-mode facebook).",
                    flush=True,
                )
            else:
                print(
                    "\nNo cookies were captured. "
                    "Retry in CMD/PowerShell, complete login, then press Enter here.",
                    flush=True,
                )
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Module entry point for setup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--subprocess-fetch" and sys.argv[3] == "--out":
        raise SystemExit(_subprocess_fetch_entry(sys.argv[2], sys.argv[4]))
    facebook_setup()

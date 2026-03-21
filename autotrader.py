"""
dealerly/autotrader.py
======================
AutoTrader.co.uk retail price comparables scraper.

Uses the __NEXT_DATA__ JSON blob embedded in AutoTrader search pages (Next.js
SSR) with a regex price fallback for resilience against page structure changes.

Respects robots.txt by only scraping search result pages, not individual
listing detail pages.

Depends on:
  - dealerly.config  (AUTOTRADER_TTL_HOURS, AUTOTRADER_SLEEP_S)
  - dealerly.db      (load_recent_autotrader_comps)
  - dealerly.utils   (now_utc_iso)

I/O: HTTP requests to autotrader.co.uk + SQLite writes for caching.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

import requests

from dealerly.config import AUTOTRADER_SLEEP_S, AUTOTRADER_TTL_HOURS
from dealerly.db import load_recent_autotrader_comps
from dealerly.utils import now_utc_iso


# Browser-like headers — AutoTrader returns 403 on obvious bot user-agents
_AT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

_AT_BASE = "https://www.autotrader.co.uk"
_DEFAULT_POSTCODE = "TW200AY"  # RHUL / Egham area


def _autotrader_price_to_cap(listing_price: Optional[float]) -> int:
    """
    Upper price bound for AutoTrader search results.

    Previously hard-coded at £15,000, which excluded higher-priced retail comps
    and biased the median **down** for £10k+ listings (high-capital runs).
    Override with DEALERLY_AUTOTRADER_PRICE_TO (integer GBP).
    """
    env = os.environ.get("DEALERLY_AUTOTRADER_PRICE_TO", "").strip()
    if env.isdigit():
        return max(5_000, min(250_000, int(env)))
    if listing_price and listing_price > 0:
        # Include anchors above the ask so median reflects retail, not a capped band
        return max(22_000, min(120_000, int(listing_price * 1.65) + 4_000))
    return 45_000


class AutoTraderComps:
    """
    Fetch retail price comparables from AutoTrader search results.

    Caching strategy (two layers):
      1. Run-level dict: avoids re-fetching the same vehicle_key within one
         pipeline run (fast, in-memory).
      2. SQLite DB (autotrader_comps table): persists across runs for ttl_hours.
         Only fetches from the network if the DB cache has fewer than 5 prices.
    """

    def __init__(self, postcode: str = "", radius: int = 100) -> None:
        self.postcode = postcode.replace(" ", "").upper() or _DEFAULT_POSTCODE
        self.radius = radius
        self._last_req_t: float = 0.0
        self._run_cache: Dict[str, List[float]] = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Enforce a minimum gap between HTTP requests."""
        elapsed = time.time() - self._last_req_t
        if elapsed < AUTOTRADER_SLEEP_S:
            time.sleep(AUTOTRADER_SLEEP_S - elapsed)
        self._last_req_t = time.time()

    def _build_url(
        self,
        make: str,
        model: str,
        year_from: Optional[int],
        year_to: Optional[int],
        price_to: int,
    ) -> str:
        make_q  = make.upper().replace(" ", "%20")
        model_q = model.upper().replace(" ", "%20")
        url = (
            f"{_AT_BASE}/car-search"
            f"?make={make_q}&model={model_q}"
            f"&postcode={self.postcode}&radius={self.radius}"
            f"&price-to={price_to}"
        )
        if year_from:
            url += f"&year-from={year_from}"
        if year_to:
            url += f"&year-to={year_to}"
        return url

    def _parse_prices(
        self, html: str
    ) -> List[Tuple[float, Optional[int], Optional[int]]]:
        """
        Extract (price, year, mileage) tuples from AutoTrader search HTML.

        Primary path: parse __NEXT_DATA__ JSON blob (Next.js SSR).
        Fallback: regex scan for rendered price strings (e.g. "£1,495").
        """
        prices: List[Tuple[float, Optional[int], Optional[int]]] = []

        # Primary: __NEXT_DATA__ JSON (Next.js server-side render)
        nd_match = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if nd_match:
            try:
                data = json.loads(nd_match.group(1))
                page_props = data.get("props", {}).get("pageProps", {})

                # Path 1: initialState → taxonomy → listings → results
                listings = (
                    page_props
                    .get("initialState", {})
                    .get("taxonomy", {})
                    .get("listings", {})
                    .get("results") or []
                )
                # Path 2: direct listings key (alternative page structure)
                if not listings:
                    listings = page_props.get("listings") or []

                for listing in listings:
                    price_info = listing.get("price") or listing.get("pricing") or {}
                    if isinstance(price_info, dict):
                        p = (
                            price_info.get("value")
                            or price_info.get("amount")
                            or price_info.get("retailPrice")
                        )
                    elif isinstance(price_info, (int, float)):
                        p = price_info
                    else:
                        p = None

                    if p:
                        year    = listing.get("year") or listing.get("vehicleYear")
                        mileage = listing.get("mileage") or listing.get("odometerReading")
                        if isinstance(mileage, dict):
                            mileage = mileage.get("value")
                        try:
                            prices.append((
                                float(p),
                                int(year) if year else None,
                                int(mileage) if mileage else None,
                            ))
                        except (TypeError, ValueError):
                            pass
            except (json.JSONDecodeError, AttributeError):
                pass

        # Fallback: regex scan for rendered "£X,XXX" price strings
        if not prices:
            for m in re.finditer(r"£([\d,]+)", html):
                try:
                    v = float(m.group(1).replace(",", ""))
                    if 400 < v < 30_000:
                        prices.append((v, None, None))
                except ValueError:
                    pass

        return prices

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self,
        make: str,
        model: str,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        limit: int = 30,
        *,
        price_to: Optional[int] = None,
    ) -> List[Tuple[float, Optional[int], Optional[int]]]:
        """
        Scrape AutoTrader search results for (price, year, mileage) tuples.

        Returns an empty list if make/model are unknown or on network error.
        """
        if not make or make == "unknown" or not model or model == "unknown":
            return []

        self._throttle()
        pt = price_to if price_to is not None else _autotrader_price_to_cap(None)
        url = self._build_url(make, model, year_from, year_to, pt)

        try:
            r = requests.get(url, headers=_AT_HEADERS, timeout=25)
            if r.status_code != 200:
                return []
            return self._parse_prices(r.text)[:limit]
        except Exception:
            return []

    def fetch_for_key(
        self,
        vehicle_key: str,
        conn: sqlite3.Connection,
        ttl_hours: float = AUTOTRADER_TTL_HOURS,
        *,
        listing_price: Optional[float] = None,
    ) -> List[float]:
        """
        Return retail price comps for a vehicle_key string ("make|model|yband").

        Cache hierarchy (fastest first):
          1. Run-level dict (in-memory, current process only)
          2. SQLite DB (persistent, up to ttl_hours old, min 5 prices required)
          3. Live AutoTrader scrape → written to DB + run cache

        ``listing_price`` sets the AT ``price-to`` search cap so high-ask cars
        still pull in appropriate retail comps (not capped at £15k).

        Returns a list of prices (floats) in GBP, possibly empty.
        """
        price_cap = _autotrader_price_to_cap(listing_price)
        run_key = f"{vehicle_key}|pt={price_cap}"

        # 1) Run-level cache (key includes price band — avoids wrong reuse)
        if run_key in self._run_cache:
            self._cache_hits += 1
            return self._run_cache[run_key]

        # 2) DB cache (vehicle_key only — may reflect older price-to; live refresh
        #    still applies when run cache misses)
        cached = load_recent_autotrader_comps(conn, vehicle_key, ttl_hours)
        if len(cached) >= 3:
            self._cache_hits += 1
            self._run_cache[run_key] = cached
            return cached

        # 3) Live fetch
        self._cache_misses += 1
        try:
            make, model, yband = vehicle_key.split("|", 2)
        except ValueError:
            return []

        year_from = year_to = None
        if "-" in yband and yband[:4].isdigit():
            year_from = int(yband[:4]) - 1
            year_to   = int(yband[:4]) + 4

        results = self.fetch(
            make, model, year_from, year_to, price_to=price_cap,
        )

        if results:
            ts = now_utc_iso()
            params = [
                (vehicle_key, p, y, m, ts, "")
                for (p, y, m) in results
            ]
            max_attempts = 12
            for attempt in range(max_attempts):
                try:
                    cur = conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    try:
                        cur.executemany(
                            "INSERT INTO autotrader_comps"
                            " (vehicle_key, price, year, mileage, date_seen, url)"
                            " VALUES (?, ?, ?, ?, ?, ?)",
                            params,
                        )
                        conn.commit()
                        break
                    except BaseException:
                        conn.rollback()
                        raise
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if ("locked" in msg or "busy" in msg) and attempt < max_attempts - 1:
                        time.sleep(min(3.0, 0.05 * (2 ** attempt)))
                        continue
                    raise

        prices = [p for (p, _, _) in results]
        self._run_cache[run_key] = prices
        return prices

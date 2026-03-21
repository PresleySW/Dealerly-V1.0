"""
dealerly/ingestion.py
=====================
Facebook Marketplace listing ingestion + abstract adapter base class.

Sprint 3 additions:
  - BaseIngestionAdapter: abstract base for all platform adapters
  - EbayIngestionAdapter: wraps existing eBay fetch functions

Legacy input modes (unchanged):
  - load_facebook_from_csv()   reads dealerly_input.csv
  - load_facebook_paste()      interactive stdin paste mode

Both produce List[Listing] in the same format as the eBay pipeline so the
rest of the pipeline treats them identically.

CSV column contract (dealerly_input.csv):
    platform, title, price_gbp, expected_resale,
    expected_days_to_sell, url, location, vrm

If expected_resale / expected_days_to_sell are supplied they are stored on
the Listing as csv_expected_resale / csv_expected_days and used directly
during scoring, bypassing the comps lookup entirely.

Depends on:
  - dealerly.models   (Listing)
  - dealerly.ebay     (guess_make_model)
  - dealerly.vrm      (normalise_vrm, looks_plausible_uk_vrm,
                       is_ulez_compliant, SAFE_VRM_PATTERNS, _scan_patterns)

I/O: file reads (CSV mode) and stdin (paste mode).
"""
from __future__ import annotations

import abc
import csv
import re
import time
from pathlib import Path
from typing import List, Optional

from dealerly.models import Listing
from dealerly.ebay import guess_make_model
from dealerly.vrm import (
    SAFE_VRM_PATTERNS,
    _scan_patterns,
    is_ulez_compliant,
    detect_ulez_from_text,
    looks_plausible_uk_vrm,
    normalise_vrm,
)


# ---------------------------------------------------------------------------
# Sprint 3: Abstract adapter base class
# ---------------------------------------------------------------------------

class BaseIngestionAdapter(abc.ABC):
    """
    Abstract base for all platform ingestion adapters.

    Each adapter is responsible for fetching raw listings from one platform
    and normalising them into List[Listing], identical in format to eBay
    pipeline output so downstream phases are platform-agnostic.

    Sub-classes must implement:
      - platform_name()    — short slug, e.g. "ebay", "facebook", "motors"
      - fetch_listings()   — return normalised List[Listing]

    Optionally override:
      - is_available()     — return False if a required dependency is missing
                             (e.g. Playwright not installed, cookies absent).
                             Pipeline will skip unavailable adapters gracefully.
    """

    @abc.abstractmethod
    def platform_name(self) -> str:
        """Short platform slug used for logging and the Listing.platform field."""

    @abc.abstractmethod
    def fetch_listings(
        self,
        queries: list[str],
        price_min: int,
        price_max: int,
        pages: int,
        **kwargs,
    ) -> List[Listing]:
        """
        Fetch and normalise listings from this platform.

        Args:
            queries:    List of search terms (e.g. ["honda jazz", "toyota yaris"]).
            price_min:  Minimum price filter in GBP.
            price_max:  Maximum price filter in GBP.
            pages:      Maximum result pages to fetch per query.
            **kwargs:   Platform-specific extras (e.g. buyer_postcode, sort).

        Returns:
            List[Listing] — deduplicated, normalised, ready for Phase 2+.
        """

    @property
    def is_available(self) -> bool:
        """
        Return True if this adapter can run in the current environment.

        Override to return False when optional dependencies (Playwright,
        credentials, cookie files, etc.) are absent. The pipeline skips
        unavailable adapters rather than raising.
        """
        return True

    @property
    def unavailable_reason(self) -> str:
        """
        Human-readable reason when is_available is False.

        Adapters may override this to provide actionable setup guidance.
        """
        return ""


# ---------------------------------------------------------------------------
# Sprint 3: eBay adapter (wraps existing eBay fetch functions)
# ---------------------------------------------------------------------------

class EbayIngestionAdapter(BaseIngestionAdapter):
    """
    eBay ingestion adapter — thin wrapper around the existing eBay fetch
    infrastructure already used in pipeline.py.

    Rather than duplicating the paged-search logic here, this adapter
    accepts a callable ``fetch_fn`` that is provided by pipeline.py at
    construction time (the ``fetch_paged`` inner function).  This keeps
    the eBay auth/token lifecycle in pipeline.py where it belongs.
    """

    def __init__(self, fetch_paged_fn):
        """
        Args:
            fetch_paged_fn: Callable[[str], List[Listing]] — the ``fetch_paged``
                            closure from pipeline.run(), already bound to the
                            active eBay token and config.
        """
        self._fetch_paged = fetch_paged_fn

    def platform_name(self) -> str:
        return "ebay"

    @property
    def is_available(self) -> bool:
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
        Iterate over all query terms and collect paged eBay results.

        The actual HTTP calls, deduplication, and normalisation are handled
        by the ``fetch_paged`` closure injected at construction time.
        merge_dedupe() is called here to handle cross-query duplicates.
        """
        from dealerly.ebay import merge_dedupe
        from concurrent.futures import ThreadPoolExecutor

        # Cap per-query results so no single model dominates the merged pool.
        MAX_PER_QUERY = 20
        # Sprint 15: run eBay queries in parallel (max 3 concurrent, rate-limit safe)
        def _fetch_one(term: str) -> List[Listing]:
            try:
                return self._fetch_paged(term)[:MAX_PER_QUERY]
            except Exception as exc:
                print(f"  [ebay] Query '{term}' failed: {exc}")
                return []

        with ThreadPoolExecutor(max_workers=min(3, len(queries))) as _pool:
            batches = list(_pool.map(_fetch_one, queries))

        return merge_dedupe(batches) if batches else []


# ---------------------------------------------------------------------------
# ULEZ helper (Sprint 15)
# ---------------------------------------------------------------------------

def _csv_ulez(year, fuel: str, text: str):
    """Year+fuel inference, overridden by text if an explicit ULEZ phrase found."""
    result = is_ulez_compliant(year, fuel)
    text_result = detect_ulez_from_text(text)
    if text_result is not None:
        return text_result
    return result


# ---------------------------------------------------------------------------
# CSV ingestion
# ---------------------------------------------------------------------------

def load_facebook_from_csv(csv_path: Path) -> List[Listing]:
    """
    Load Facebook Marketplace listings from dealerly_input.csv.

    Skips rows with no title or a zero/missing price.
    Skips rows that raise parsing errors (prints a warning per row).
    Returns an empty list if the file does not exist or cannot be read.
    """
    if not csv_path.exists():
        print(f"  [FB] CSV not found: {csv_path}")
        return []

    listings: List[Listing] = []
    try:
        with csv_path.open(encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                listing = _row_to_listing(row)
                if listing is not None:
                    listings.append(listing)
    except Exception as exc:
        print(f"  [FB] CSV read error: {exc}")

    return listings


def _row_to_listing(row: dict) -> Optional[Listing]:
    """
    Convert a single CSV DictReader row to a Listing, or None on invalid data.
    """
    try:
        platform = str(row.get("platform", "facebook")).strip().lower()
        title    = str(row.get("title", "")).strip()
        price    = float(row.get("price_gbp", 0) or 0)
        vrm_raw  = normalise_vrm(str(row.get("vrm", "") or ""))
        url      = str(row.get("url", "")).strip()
        location = str(row.get("location", "")).strip()

        if not title or price <= 0:
            return None

        csv_resale = _parse_optional_float(row.get("expected_resale"))
        csv_days   = _parse_optional_int(row.get("expected_days_to_sell"))

        guess = guess_make_model(title)
        fuel  = guess.fuel_type
        vrm   = vrm_raw if (vrm_raw and looks_plausible_uk_vrm(vrm_raw)) else ""

        listing = Listing(
            platform=platform,
            item_id=f"csv_{hash(title + str(price)):x}",
            title=title,
            price_gbp=price,
            url=url,
            location=location,
            condition="Used",
            vrm=vrm,
            raw=dict(row),
            fuel_type=fuel,
            year=guess.year,
            mileage=guess.mileage,
            ulez_compliant=_csv_ulez(guess.year, fuel, title),
            csv_expected_resale=csv_resale,
            csv_expected_days=csv_days,
        )

        if vrm:
            listing.vrm_source     = "csv_input"
            listing.vrm_confidence = 0.99

        return listing

    except Exception as exc:
        print(f"  [FB] Skipping row: {exc}")
        return None


# ---------------------------------------------------------------------------
# Paste ingestion (interactive)
# ---------------------------------------------------------------------------

def load_facebook_paste() -> List[Listing]:
    """
    Interactive mode: user pastes Facebook listing text into the terminal.

    One listing per paste block; blank line signals end of block.
    Type 'done' to finish.

    Attempts to extract price, VRM, and URL from the pasted text.
    Falls back to prompting for price if none found.
    """
    listings: List[Listing] = []
    print("\n[Facebook Marketplace — Paste Mode]")
    print("Paste each listing then press Enter twice. Type 'done' on a new line to finish.\n")

    while True:
        print("--- Paste listing text (or 'done' to finish) ---")
        lines: List[str] = []

        while True:
            line = input()
            if line.strip().lower() == "done":
                return listings
            if line.strip() == "" and lines:
                break
            lines.append(line)

        if not lines:
            continue

        listing = _paste_block_to_listing(lines)
        if listing is not None:
            listings.append(listing)
            print(
                f"  Added: {listing.title[:60]} — £{listing.price_gbp:.0f}"
                + (f" | VRM: {listing.vrm}" if listing.vrm else "")
            )


def _paste_block_to_listing(lines: List[str]) -> Optional[Listing]:
    """
    Parse a list of pasted text lines into a Listing.
    Returns None if no valid price can be determined.
    """
    text  = " ".join(lines)
    title = lines[0][:150].strip()

    # Price extraction
    price_match = re.search(r"£\s?([\d,]+)", text)
    if price_match:
        try:
            price = float(price_match.group(1).replace(",", ""))
        except ValueError:
            price = 0.0
    else:
        price = 0.0

    if price <= 0:
        price_str = input("  Price (£): ").strip()
        try:
            price = float(price_str.replace("£", "").replace(",", ""))
        except ValueError:
            return None
        if price <= 0:
            return None

    # URL extraction
    url = ""
    for line in lines:
        if "facebook.com" in line.lower() or "fb.com" in line.lower():
            url_match = re.search(r"https?://\S+", line)
            if url_match:
                url = url_match.group(0)
                break

    guess = guess_make_model(title)
    fuel  = guess.fuel_type

    listing = Listing(
        platform="facebook",
        item_id=f"fb_{hash(title + str(price)):x}",
        title=title,
        price_gbp=price,
        url=url,
        location="",
        condition="Used",
        vrm="",
        raw={"raw_text": text},
        fuel_type=fuel,
        year=guess.year,
        mileage=guess.mileage,
        ulez_compliant=_csv_ulez(guess.year, fuel, text),
    )

    # Attempt VRM extraction from pasted text
    result = _scan_patterns(text.upper(), SAFE_VRM_PATTERNS)
    if result:
        vrm, name, conf = result
        listing.vrm            = vrm
        listing.vrm_source     = f"regex_paste_{name}"
        listing.vrm_confidence = conf

    return listing


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_optional_float(raw: object) -> Optional[float]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_optional_int(raw: object) -> Optional[int]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None

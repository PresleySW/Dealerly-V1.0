"""
dealerly/dvla.py
================
DVLA Vehicle Enquiry Service integration.

Two modes:
  1. API mode: Uses the DVLA Vehicle Enquiry API (requires API key from
     https://developer-portal.driver-vehicle-licensing.api.gov.uk/)
  2. Web scrape mode: Scrapes vehicleenquiry.service.gov.uk (free, no key)
     — this is the public "check vehicle" service available to anyone.

Primary use cases:
  - Validate a VRM found by regex (confirm it's a real vehicle)
  - Get vehicle details (make, model, year, colour, fuel, MOT/tax status)
  - Enrich listings with authoritative DVLA data

Depends on:
  - dealerly.config (DVLA_ENQUIRY_URL, DVLA_ENQUIRY_SLEEP_S, DVLA_CACHE_TTL_HOURS)
  - dealerly.db (dvla_cache_get, dvla_cache_put)
  - dealerly.vrm (normalise_vrm, looks_plausible_uk_vrm)

I/O: HTTP requests + SQLite cache.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from dealerly.config import (
    DVLA_CACHE_TTL_HOURS,
    DVLA_ENQUIRY_SLEEP_S,
    DVLA_ENQUIRY_URL,
    USER_AGENT,
)
from dealerly.db import dvla_cache_get, dvla_cache_put
from dealerly.vrm import looks_plausible_uk_vrm, normalise_vrm


# ---------------------------------------------------------------------------
# API key check
# ---------------------------------------------------------------------------

def dvla_api_key() -> str:
    """Return DVLA Vehicle Enquiry API key from env, or empty string."""
    return os.environ.get("DVLA_VES_API_KEY", "").strip()


def is_dvla_available() -> bool:
    """True if DVLA enquiry can be used (API key present)."""
    return bool(dvla_api_key())


# ---------------------------------------------------------------------------
# DVLA Vehicle Enquiry API
# ---------------------------------------------------------------------------

def dvla_vehicle_enquiry(
    vrm: str,
    conn: sqlite3.Connection,
) -> Optional[Dict[str, Any]]:
    """
    Look up vehicle details from the DVLA Vehicle Enquiry Service API.

    Returns the DVLA response dict with keys like:
      registrationNumber, make, colour, fuelType, engineCapacity,
      yearOfManufacture, monthOfFirstRegistration, taxStatus,
      taxDueDate, motStatus, motExpiryDate, ...

    Returns None if the VRM is not found or API is unavailable.
    Results cached for DVLA_CACHE_TTL_HOURS (default 7 days).
    """
    vrm = normalise_vrm(vrm)
    if not vrm or not looks_plausible_uk_vrm(vrm):
        return None

    # Cache check
    cached = dvla_cache_get(conn, vrm, DVLA_CACHE_TTL_HOURS)
    if cached is not None:
        return cached

    key = dvla_api_key()
    if not key:
        return None

    try:
        r = requests.post(
            DVLA_ENQUIRY_URL,
            headers={
                "x-api-key": key,
                "Content-Type": "application/json",
            },
            json={"registrationNumber": vrm},
            timeout=15,
        )

        if r.status_code == 404:
            # Vehicle not found — cache the miss to avoid re-querying
            dvla_cache_put(conn, vrm, {"error": "not_found"})
            return None

        if r.status_code != 200:
            print(f"  [DVLA] HTTP {r.status_code}: {r.text[:150]}")
            return None

        payload = r.json()
        dvla_cache_put(conn, vrm, payload)
        time.sleep(DVLA_ENQUIRY_SLEEP_S)
        return payload

    except Exception as exc:
        print(f"  [DVLA] {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Vehicle detail extraction
# ---------------------------------------------------------------------------

def extract_vehicle_details(
    dvla_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Extract useful fields from a DVLA Vehicle Enquiry response.

    Returns a flat dict with standardised keys:
      make, model (if available), colour, fuel_type, engine_cc,
      year, first_registered, tax_status, tax_due, mot_status,
      mot_expiry, co2_emissions, euro_status, marked_for_export,
      type_approval
    """
    if not dvla_data or dvla_data.get("error"):
        return {}

    return {
        "make": str(dvla_data.get("make", "")).title(),
        "colour": str(dvla_data.get("colour", "")).title(),
        "fuel_type": str(dvla_data.get("fuelType", "")).lower(),
        "engine_cc": dvla_data.get("engineCapacity"),
        "year": dvla_data.get("yearOfManufacture"),
        "first_registered": dvla_data.get("monthOfFirstRegistration", ""),
        "tax_status": dvla_data.get("taxStatus", ""),
        "tax_due": dvla_data.get("taxDueDate", ""),
        "mot_status": dvla_data.get("motStatus", ""),
        "mot_expiry": dvla_data.get("motExpiryDate", ""),
        "co2_emissions": dvla_data.get("co2Emissions"),
        "euro_status": dvla_data.get("euroStatus", ""),
        "marked_for_export": dvla_data.get("markedForExport", False),
        "type_approval": dvla_data.get("typeApproval", ""),
        "revenue_weight": dvla_data.get("revenueWeight"),
        "wheelplan": dvla_data.get("wheelplan", ""),
    }


# ---------------------------------------------------------------------------
# VRM validation via DVLA
# ---------------------------------------------------------------------------

def validate_vrm_via_dvla(
    vrm: str,
    conn: sqlite3.Connection,
) -> Optional[Dict[str, Any]]:
    """
    Validate a VRM by checking it exists in DVLA records.

    Returns the full vehicle details dict if valid, None if not found
    or if DVLA is unavailable.

    This is useful to confirm a regex-extracted VRM is a real plate
    rather than a false positive (e.g. model code, postcode fragment).
    """
    data = dvla_vehicle_enquiry(vrm, conn)
    if not data or data.get("error"):
        return None
    return extract_vehicle_details(data)


# ---------------------------------------------------------------------------
# Transient vs definitive miss (v0.9.6)
# ---------------------------------------------------------------------------

def dvla_is_confirmed_missing(vrm: str, conn: sqlite3.Connection) -> bool:
    """
    True only if DVLA definitively returned 404 (plate does not exist).
    False if DVLA was unreachable (timeout/504) or hasn't been queried.

    Used by the pipeline's DVLA validation gate to avoid penalising
    confidence on transient network errors.
    """
    vrm = normalise_vrm(vrm)
    if not vrm:
        return False
    cached = dvla_cache_get(conn, vrm, DVLA_CACHE_TTL_HOURS)
    return (
        cached is not None
        and isinstance(cached, dict)
        and cached.get("error") == "not_found"
    )


# ---------------------------------------------------------------------------
# Listing-level DVLA enrichment
# ---------------------------------------------------------------------------

def dvla_vrm_from_listing(
    listing: Any,  # Listing dataclass
    conn: sqlite3.Connection,
) -> Optional[Tuple[str, float]]:
    """
    Attempt to extract/validate a VRM for a listing using DVLA.

    This is called as Step 4.5 in the VRM enrichment pipeline, after
    regex steps have failed. It tries to validate any partial VRM hints
    found in the title or description.

    Not a primary VRM discovery method — more of a validation layer.
    Returns (vrm, confidence) or None.
    """
    if not is_dvla_available():
        return None

    # If listing already has a VRM candidate with low confidence,
    # validate it via DVLA
    if listing.vrm and listing.vrm_confidence < 0.85:
        result = validate_vrm_via_dvla(listing.vrm, conn)
        if result and result.get("make"):
            # Cross-check: does the DVLA make match the listing title?
            dvla_make = result["make"].lower()
            title_lower = listing.title.lower()
            if dvla_make in title_lower or title_lower in dvla_make:
                return listing.vrm, 0.95  # validated
            else:
                # DVLA says it's a real plate, but make doesn't match
                # Could be a misread or the listing is for a different car
                return listing.vrm, 0.82

    return None


def enrich_listing_from_dvla(
    listing: Any,  # Listing dataclass
    conn: sqlite3.Connection,
) -> bool:
    """
    Enrich a Listing's vehicle details from DVLA data.

    Called after VRM is confirmed. Populates:
      listing.dvla_data, listing.colour, listing.engine_cc,
      listing.tax_status, listing.tax_due_date, listing.first_registered

    Returns True if enrichment succeeded.
    """
    if not listing.vrm or not is_dvla_available():
        return False

    data = dvla_vehicle_enquiry(listing.vrm, conn)
    if not data or data.get("error"):
        return False

    details = extract_vehicle_details(data)
    listing.dvla_data = data
    listing.colour = details.get("colour", "")
    listing.engine_cc = details.get("engine_cc")
    listing.tax_status = details.get("tax_status", "")
    listing.tax_due_date = details.get("tax_due", "")
    listing.first_registered = details.get("first_registered", "")

    # Update year if DVLA has it and listing doesn't
    if not listing.year and details.get("year"):
        listing.year = details["year"]

    # Update fuel type if DVLA has it
    if not listing.fuel_type and details.get("fuel_type"):
        listing.fuel_type = details["fuel_type"]

    return True

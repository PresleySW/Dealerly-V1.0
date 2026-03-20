"""
dealerly/sheets.py — Sprint 2
==============================
Google Sheets export — pushes pipeline state after each run.

Columns written:
  item_id | title | platform | buy_price | expected_profit | decision |
  VRM | MOT status | lead_status | last_updated | URL

Auth: gspread + Google Service Account JSON (no OAuth flow needed).

Requirements:
  pip install gspread google-auth

Config (in .env or environment):
  GOOGLE_SHEET_ID              — the spreadsheet ID from its URL
  GOOGLE_SERVICE_ACCOUNT_JSON  — path to the service account JSON key file

Depends on:
  - dealerly.config (GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON_PATH)
  - dealerly.db     (lead_list)
  - dealerly.models (DealInput, DealOutput, Listing)
  - dealerly.utils  (now_utc_iso)
  - dealerly.vrm    (is_vrm_displayable)

I/O: Google Sheets API (write only). SQLite read (lead status lookup).
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from dealerly.config import GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON_PATH
from dealerly.db import lead_list
from dealerly.models import DealInput, DealOutput, Listing
from dealerly.utils import now_utc_iso
from dealerly.vrm import is_vrm_displayable


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def is_sheets_available() -> bool:
    """True if gspread is installed and credentials are configured."""
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON_PATH:
        return False
    try:
        import gspread  # noqa: F401
        from google.oauth2.service_account import Credentials  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_client():
    """Return an authenticated gspread client using the service account."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds = Credentials.from_service_account_file(
        str(GOOGLE_SERVICE_ACCOUNT_JSON_PATH),
        scopes=scopes,
    )
    return gspread.authorize(creds)


# ---------------------------------------------------------------------------
# Build rows from pipeline output
# ---------------------------------------------------------------------------

_HEADERS = [
    "item_id", "title", "platform", "buy_price", "expected_profit",
    "decision", "VRM", "MOT status", "lead_status", "last_updated", "URL",
]


def _mot_status(listing: Listing) -> str:
    if listing.mot_history:
        tests = listing.mot_history.get("motTests") or []
        if tests:
            latest = tests[0]
            result = str(latest.get("testResult", "")).upper()
            expiry = latest.get("expiryDate", "")
            return f"{result} exp {expiry}" if expiry else result
        return "DVSA: no tests"
    return "unverified"


def _build_rows(
    top_rows: List[Tuple[Listing, DealInput, DealOutput]],
    lead_status_map: Dict[str, str],
    ts: str,
) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for listing, deal, out in top_rows:
        vrm = (
            listing.vrm
            if is_vrm_displayable(listing.vrm, listing.vrm_confidence)
            else ""
        )
        rows.append([
            listing.item_id,
            listing.title[:120],
            listing.platform,
            f"{listing.price_gbp:.0f}",
            f"{out.expected_profit:.0f}",
            out.decision,
            vrm,
            _mot_status(listing),
            lead_status_map.get(listing.item_id, ""),
            ts,
            listing.url,
        ])
    return rows


# ---------------------------------------------------------------------------
# Main export function
# ---------------------------------------------------------------------------

def export_to_sheets(
    top_rows: List[Tuple[Listing, DealInput, DealOutput]],
    conn: sqlite3.Connection,
) -> bool:
    """
    Push pipeline state to the configured Google Sheet.

    Clears the existing data range (row 2 onwards) and rewrites it so the
    sheet always reflects the current run. Row 1 is the header and is only
    written if the sheet is empty.

    Returns True on success, False on any error.
    """
    if not is_sheets_available():
        return False

    try:
        # Build lead-status lookup from SQLite
        leads = lead_list(conn, limit=500)
        lead_status_map = {r["item_id"]: r["status"] for r in leads if r.get("item_id")}

        ts = now_utc_iso()
        data_rows = _build_rows(top_rows, lead_status_map, ts)

        client = _get_client()
        sheet  = client.open_by_key(GOOGLE_SHEET_ID).sheet1

        # Write or validate header in row 1
        existing = sheet.row_values(1)
        if not existing or existing != _HEADERS:
            sheet.update("A1", [_HEADERS])

        # Clear old data (row 2 down) and write fresh
        last_col = chr(ord("A") + len(_HEADERS) - 1)  # e.g. "K" for 11 columns
        if data_rows:
            sheet.batch_clear([f"A2:{last_col}5000"])
            sheet.update(f"A2", data_rows)
        else:
            sheet.batch_clear([f"A2:{last_col}5000"])

        return True

    except Exception as exc:
        print(f"  [Sheets] {type(exc).__name__}: {exc}")
        return False

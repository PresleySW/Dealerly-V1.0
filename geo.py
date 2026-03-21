"""
dealerly/geo.py
===============
UK postcode → lat/lon for the stats map. Uses postcodes.io (HTTPS) with SQLite cache.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from dealerly.config import USER_AGENT
from dealerly.utils import now_utc_iso


def resolve_postcode_coords(
    conn: sqlite3.Connection,
    postcode: str,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Return (lat, lon) for a UK outward/inward postcode, or (None, None) if unknown.
    Results are cached in postcode_geo_cache.
    """
    raw = (postcode or "").strip().upper().replace(" ", "")
    if len(raw) < 5:
        return None, None

    cur = conn.execute(
        "SELECT lat, lon FROM postcode_geo_cache WHERE postcode = ?",
        (raw,),
    )
    row = cur.fetchone()
    if row:
        return float(row[0]), float(row[1])

    url = f"https://api.postcodes.io/postcodes/{urllib.parse.quote(raw)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None, None

    result = payload.get("result") if isinstance(payload, dict) else None
    if not result:
        return None, None

    try:
        lat = float(result["latitude"])
        lon = float(result["longitude"])
    except (KeyError, TypeError, ValueError):
        return None, None

    conn.execute(
        "INSERT INTO postcode_geo_cache (postcode, lat, lon, resolved_at)"
        " VALUES (?, ?, ?, ?)",
        (raw, lat, lon, now_utc_iso()),
    )
    conn.commit()
    return lat, lon

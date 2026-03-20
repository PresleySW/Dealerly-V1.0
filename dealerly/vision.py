"""
dealerly/vision.py
==================
Plate recognition from listing photos using Plate Recognizer ANPR API.

v0.9.6 changes:
  - Negative results no longer cached — retries ANPR each run for misses
  - max_images increased to 6 (was 4), matching pipeline collect limit
  - Cache version bumped to bust stale negative caches from v0.9.1-0.9.5
  - Rate limit: retries once after 5s wait instead of immediate give-up

v0.9.1: Replaced Google Cloud Vision (generic OCR) with Plate Recognizer
(purpose-built ANPR). Google Vision was returning 0% VRM hit rate because
generic OCR dumps all text from an image — signs, stickers, watermarks —
and regex has to find a plate in that noise. Plate Recognizer detects the
plate region first, then reads the characters. Huge accuracy improvement.

Free tier: 2,500 lookups/month. Typical run enriches 18 listings at up to
4 images each = max 72 calls/run. ~34 free runs/month.

Depends on:
  - dealerly.config  (USER_AGENT, PLATE_RECOGNIZER_URL, PLATE_RECOGNIZER_SLEEP_S)
  - dealerly.db      (ai_cache_get, ai_cache_put)
  - dealerly.vrm     (normalise_vrm, looks_plausible_uk_vrm)

I/O: HTTP requests (image downloads + Plate Recognizer API). SQLite cache.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from dealerly.config import (
    PLATE_RECOGNIZER_SLEEP_S,
    PLATE_RECOGNIZER_URL,
    USER_AGENT,
)
from dealerly.db import ai_cache_get, ai_cache_put
from dealerly.vrm import looks_plausible_uk_vrm, normalise_vrm

_VISION_CACHE_PREFIX = "platerecog_"


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

def plate_recognizer_token() -> str:
    """Return Plate Recognizer API token from env, or empty string."""
    return os.environ.get("PLATE_RECOGNIZER_TOKEN", "").strip()


def is_vision_available() -> bool:
    """True if Plate Recognizer ANPR can be used."""
    return bool(plate_recognizer_token())


# ---------------------------------------------------------------------------
# Image download (reused from v0.9.0 — eBay CDN blocks direct API fetches)
# ---------------------------------------------------------------------------

def _download_image_bytes(url: str) -> Optional[bytes]:
    """
    Download an image and return raw bytes, or None.
    Uses browser-like headers because eBay CDN blocks bot user-agents.
    """
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.ebay.co.uk/",
            },
            timeout=10,
            stream=True,
        )
        if r.status_code != 200:
            return None
        ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not ct.startswith("image/"):
            return None
        return r.content
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Plate Recognizer API call
# ---------------------------------------------------------------------------

def _call_plate_recognizer(image_bytes: bytes) -> Optional[List[Dict[str, Any]]]:
    """
    Send image bytes to Plate Recognizer Snapshot API.

    Returns list of plate results, each with:
      plate: str (e.g. "AB12CDE")
      score: float (0-1 overall confidence)
      dscore: float (0-1 detection confidence)
      region: dict with code/score
      vehicle: dict with type/score/box

    Returns None on failure.
    """
    token = plate_recognizer_token()
    if not token:
        return None

    try:
        r = requests.post(
            PLATE_RECOGNIZER_URL,
            headers={"Authorization": f"Token {token}"},
            files={"upload": ("image.jpg", image_bytes, "image/jpeg")},
            data={"regions": "gb"},  # UK plates
            timeout=20,
        )

        if r.status_code == 403:
            print("  [ANPR] API token invalid or quota exceeded")
            return None
        if r.status_code == 429:
            print("  [ANPR] Rate limited — waiting 5s")
            time.sleep(5)
            # v0.9.6: retry once after rate limit instead of giving up
            try:
                r = requests.post(
                    PLATE_RECOGNIZER_URL,
                    headers={"Authorization": f"Token {token}"},
                    files={"upload": ("image.jpg", image_bytes, "image/jpeg")},
                    data={"regions": "gb"},
                    timeout=20,
                )
                if r.status_code in (200, 201):
                    return r.json().get("results", [])
            except Exception:
                pass
            return None
        if r.status_code != 200 and r.status_code != 201:
            print(f"  [ANPR] HTTP {r.status_code}: {r.text[:150]}")
            return None

        data = r.json()
        return data.get("results", [])

    except Exception as exc:
        print(f"  [ANPR] {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------------
# VRM extraction from Plate Recognizer results
# ---------------------------------------------------------------------------

def _extract_best_vrm(results: List[Dict[str, Any]]) -> Optional[Tuple[str, float]]:
    """
    Extract the best VRM from Plate Recognizer results.

    Returns (vrm, confidence) or None.
    Confidence is the product of detection score and OCR score.
    Only returns plausible UK VRMs above 0.60 confidence.
    """
    if not results:
        return None

    best: Optional[Tuple[str, float]] = None

    for result in results:
        plate_text = result.get("plate", "")
        ocr_score = float(result.get("score", 0))
        det_score = float(result.get("dscore", 0))

        # Combined confidence
        confidence = ocr_score * det_score

        vrm = normalise_vrm(plate_text)
        if not looks_plausible_uk_vrm(vrm):
            # Common ANPR OCR confusions (0/O, 1/I, 5/S, 8/B) on visible plates.
            vrm = _repair_ocr_vrm(vrm)
        if not vrm or not looks_plausible_uk_vrm(vrm):
            continue

        # Check if this is a UK region result
        region = result.get("region", {})
        region_code = str(region.get("code", "")).lower() if isinstance(region, dict) else ""

        # Boost confidence for UK-region matches
        if region_code.startswith("gb"):
            confidence = min(confidence * 1.05, 0.99)

        if confidence >= 0.60:
            if best is None or confidence > best[1]:
                best = (vrm, round(confidence, 3))

    return best


def _repair_ocr_vrm(vrm: str) -> str:
    """
    Attempt light OCR repair for UK-format 7-char plates.
    """
    t = normalise_vrm(vrm)
    if len(t) != 7:
        return t
    chars = list(t)
    # New format AB12CDE: letters, letters, digits, digits, letters, letters, letters
    for i in (0, 1, 4, 5, 6):
        if chars[i] == "0":
            chars[i] = "O"
        elif chars[i] == "1":
            chars[i] = "I"
        elif chars[i] == "5":
            chars[i] = "S"
        elif chars[i] == "8":
            chars[i] = "B"
    for i in (2, 3):
        if chars[i] == "O":
            chars[i] = "0"
        elif chars[i] == "I":
            chars[i] = "1"
        elif chars[i] == "S":
            chars[i] = "5"
        elif chars[i] == "B":
            chars[i] = "8"
    candidate = "".join(chars)
    return candidate if looks_plausible_uk_vrm(candidate) else t


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def _vision_cache_key(item_id: str, image_urls: List[str]) -> str:
    payload = json.dumps({
        "item_id": item_id,
        "urls": sorted(image_urls[:6]),
        "v": "v0.9.6_platerecog",  # v0.9.6: bumped to bust old negative caches
    }, sort_keys=True)
    return _VISION_CACHE_PREFIX + hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Image ranking for ANPR
# ---------------------------------------------------------------------------

# Keywords in a URL/filename that suggest the image is unlikely to show a plate
_ANPR_DEPRIORITISE: tuple = (
    "interior", "dashboard", "engine", "wheel", "seat",
    "boot", "cabin", "door", "odometer", "speedo",
)


def rank_images_for_anpr(urls: List[str]) -> List[str]:
    """
    Reorder image URLs to maximise ANPR hit rate before sending to Plate Recognizer.

    Scoring heuristic (higher = more likely to show a readable plate):
      +0.4  index 0 (hero/front 3-quarter shot) or last image (often rear)
      -0.6  URL/filename contains a deprioritise keyword (interior, dashboard, etc.)

    This is a soft preference, not a hard filter — all images are still returned
    so that if ranked images yield nothing, the caller can fall back to the full
    set. Relative order of equal-scored images is preserved (stable sort).
    """
    if not urls:
        return urls

    total = len(urls)

    def _score(url: str, idx: int) -> float:
        score = 0.5
        if idx == 0 or idx == total - 1:
            score += 0.4
        u = url.lower()
        if any(kw in u for kw in _ANPR_DEPRIORITISE):
            score -= 0.6
        return score

    scored = [(url, _score(url, i)) for i, url in enumerate(urls)]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [url for url, _ in scored]


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def extract_vrm_via_vision(
    item_id: str,
    image_urls: List[str],
    conn: sqlite3.Connection,
    max_images: int = 6,
) -> Optional[Tuple[str, float]]:
    """
    Attempt to extract a VRM from listing photos using Plate Recognizer ANPR.

    Tries up to max_images photos, stopping as soon as a confident VRM is found.
    Results are cached per (item_id, image_urls) so the API is never called
    twice for the same listing.

    Returns (vrm, confidence) or None if not found / API unavailable.
    """
    if not is_vision_available():
        return None
    if not image_urls:
        return None

    urls_to_try = image_urls[:max_images]
    cache_key = _vision_cache_key(item_id, urls_to_try)

    # Cache hit
    cached = ai_cache_get(conn, cache_key)
    if cached is not None:
        vrm = cached.get("vrm", "")
        conf = float(cached.get("confidence", 0.0))
        return (vrm, conf) if vrm and looks_plausible_uk_vrm(vrm) else None

    best: Optional[Tuple[str, float]] = None

    for url in urls_to_try:
        img_bytes = _download_image_bytes(url)
        if not img_bytes:
            continue

        results = _call_plate_recognizer(img_bytes)
        if results is None:
            time.sleep(0.3)
            continue

        result = _extract_best_vrm(results)
        if result:
            vrm, conf = result
            if best is None or conf > best[1]:
                best = (vrm, conf)
            # Stop on confident hit — ANPR is accurate enough
            if conf >= 0.80:
                break

        time.sleep(PLATE_RECOGNIZER_SLEEP_S)

    # v0.9.6: only cache positive results. Negative results (rate limit,
    # download failure, no plate visible) should retry on next run.
    if best:
        payload: Dict[str, Any] = {
            "vrm": best[0],
            "confidence": best[1],
        }
        ai_cache_put(conn, cache_key, "plate_recognizer", payload)

    return best

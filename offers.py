"""
dealerly/offers.py
==================
AI-generated offer messages for BUY/OFFER listings.

Claude is the primary backend; the module also exposes a plain-text fallback
so the pipeline always produces a message even when the API is unavailable.

Dead code note: claude_extract_vrm() and _parse_vrm_ai_response() from the
monolith are intentionally NOT ported — VRM extraction is regex-only in
v0.9.0 and those functions were unused since v0.8.5.

Depends on:
  - dealerly.config  (DEFAULT_CLAUDE_MODEL, CLAUDE_BASE_URL,
                      CLAUDE_ANTHROPIC_VERSION, CLAUDE_TIMEOUT_S)
  - dealerly.models  (Listing, DealInput, DealOutput)
  - dealerly.db      (ai_cache_get, ai_cache_put)
  - dealerly.ebay    (guess_make_model)
  - dealerly.utils   (round_to_nearest)

I/O: Claude API calls + SQLite cache reads/writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from dealerly.config import (
    CLAUDE_ANTHROPIC_VERSION,
    CLAUDE_BASE_URL,
    CLAUDE_TIMEOUT_S,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_OPENAI_MODEL,
    OPENAI_BASE_URL,
    OPENAI_TIMEOUT_S,
)
from dealerly.db import ai_cache_get, ai_cache_put
from dealerly.ebay import guess_make_model
from dealerly.models import DealInput, DealOutput, Listing
from dealerly.utils import round_to_nearest


# ---------------------------------------------------------------------------
# API key helpers
# ---------------------------------------------------------------------------

def claude_api_key() -> str:
    return os.environ.get("CLAUDE_API_KEY", "").strip()


def openai_api_key() -> str:
    """
    OpenAI API key, or a placeholder when using a local OpenAI-compatible server
    (Ollama, LM Studio, vLLM) — those often accept any non-empty Bearer token.
    """
    raw = os.environ.get("OPENAI_API_KEY", "").strip()
    if raw:
        return raw
    base = (os.environ.get("OPENAI_BASE_URL") or "").strip().lower()
    if "localhost" in base or "127.0.0.1" in base:
        return "ollama"
    return ""


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _claude_request(
    messages: List[Dict[str, Any]],
    system: str = "",
    max_tokens: int = 400,
    model: str = DEFAULT_CLAUDE_MODEL,
) -> Optional[str]:
    """
    POST to the Claude Messages API and return the text response, or None.

    Prints a brief warning on API errors / timeouts so the pipeline can
    continue with the fallback message without crashing.
    """
    key = claude_api_key()
    if not key:
        return None

    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        body["system"] = system

    try:
        r = requests.post(
            f"{CLAUDE_BASE_URL}/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": CLAUDE_ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
            timeout=CLAUDE_TIMEOUT_S,
        )
        if r.status_code >= 400:
            print(f"  [Claude API] HTTP {r.status_code}: {r.text[:200] or '(empty)'}")
            return None
        text = "".join(
            block.get("text", "")
            for block in (r.json().get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return text.strip() or None

    except requests.exceptions.Timeout:
        print(f"  [Claude API] Timeout after {CLAUDE_TIMEOUT_S}s")
        return None
    except Exception as exc:
        print(f"  [Claude API] {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------------
# OpenAI fallback API call (v0.9.5.3)
# ---------------------------------------------------------------------------

def _openai_request(
    messages: List[Dict[str, Any]],
    system: str = "",
    max_tokens: int = 400,
    model: str = DEFAULT_OPENAI_MODEL,
) -> Optional[str]:
    """
    POST to the OpenAI Chat Completions API and return the text response, or None.
    Used as a fallback when Claude credits are exhausted.
    """
    key = openai_api_key()
    if not key:
        return None

    oai_messages: List[Dict[str, Any]] = []
    if system:
        oai_messages.append({"role": "system", "content": system})
    oai_messages.extend(messages)

    try:
        r = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": oai_messages,
            },
            timeout=OPENAI_TIMEOUT_S,
        )
        if r.status_code >= 400:
            print(f"  [OpenAI API] HTTP {r.status_code}: {r.text[:200] or '(empty)'}")
            return None
        text = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return text.strip() or None

    except requests.exceptions.Timeout:
        print(f"  [OpenAI API] Timeout after {OPENAI_TIMEOUT_S}s")
        return None
    except Exception as exc:
        print(f"  [OpenAI API] {type(exc).__name__}: {exc}")
        return None


def _ai_request(
    messages: List[Dict[str, Any]],
    system: str = "",
    max_tokens: int = 400,
    preferred_backend: str = "openai",
) -> tuple[Optional[str], str]:
    """
    Try OpenAI first to save Claude credits; fall back to Claude on failure.
    Returns (text_or_None, backend_label).
    """
    pref = (preferred_backend or "openai").strip().lower()
    if pref == "local":
        # Windows-friendly: same as OpenAI path (Ollama / LM Studio / NIM OpenAI wrapper).
        pref = "openai"
    if pref == "claude":
        text = _claude_request(messages, system=system, max_tokens=max_tokens)
        if text:
            return text, "[Claude]"
        print("  [offers] Claude unavailable or failed — trying OpenAI fallback...")
        text = _openai_request(messages, system=system, max_tokens=max_tokens)
        if text:
            return text, "[OpenAI fallback]"
        return None, "[no backend]"

    text = _openai_request(messages, system=system, max_tokens=max_tokens)
    if text:
        return text, "[OpenAI]"
    print("  [offers] OpenAI unavailable or failed — trying Claude fallback...")
    text = _claude_request(messages, system=system, max_tokens=max_tokens)
    if text:
        return text, "[Claude fallback]"
    return None, "[no backend]"

def _short_vehicle_name(listing: Listing) -> str:
    """
    Return a short natural vehicle name: 'Honda Jazz', 'Seat Leon' etc.
    Falls back to the first three words of the listing title.
    """
    guess = guess_make_model(listing.title)
    if guess.make != "unknown" and guess.model != "unknown":
        return f"{guess.make.title()} {guess.model.title()}"
    words = listing.title.split()
    clean_words: List[str] = []
    for w in words:
        t = w.strip("()[]-_,./")
        if not t:
            continue
        if any(ch.isdigit() for ch in t):
            continue
        if len(t) < 2:
            continue
        clean_words.append(t)
        if len(clean_words) >= 3:
            break
    if clean_words:
        return " ".join(clean_words)
    return " ".join(words[:3]) if len(words) >= 3 else listing.title[:30]


def _mot_context_note(listing: Listing) -> str:
    """
    Extract a brief MOT context note for the offer prompt.

    Returns a note covering: recent failures/advisories (negative signals) and
    total passed-test count (positive signal for clean history).
    Returns "" if no MOT data is present.
    """
    if not listing.mot_history:
        return ""
    tests = listing.mot_history.get("motTests") or []
    if not tests:
        return ""
    last       = tests[0]
    defects    = last.get("defects") or []
    advisories = [d for d in defects if str(d.get("type", "")).upper() in ("ADVISORY", "MONITOR")]
    failures   = [d for d in defects if str(d.get("type", "")).upper() in ("FAIL", "MAJOR", "DANGEROUS")]

    if failures:
        base = "recent MOT failure on record"
    elif len(advisories) >= 2:
        base = "a couple of MOT advisories noted"
    else:
        base = ""

    passed = sum(1 for t in tests if str(t.get("testResult", "")).upper() == "PASSED")
    count_note = f"{passed} passed MOT tests" if passed >= 2 else ""

    parts = [p for p in [base, count_note] if p]
    return "; ".join(parts) if parts else ""

def _get_mot_days_left(listing: Listing) -> Optional[int]:
    """Calculate remaining MOT days from the DVSA expiry date."""
    if not listing.mot_history:
        return None
    tests = listing.mot_history.get("motTests") or []
    for t in tests:
        if str(t.get("testResult", "")).upper() == "PASSED":
            expiry = t.get("expiryDate")
            if expiry:
                try:
                    exp_date = datetime.strptime(expiry.replace(".", "-"), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    return max(0, (exp_date - datetime.now(timezone.utc)).days)
                except Exception:
                    pass
            break
    return None


def _offer_cache_key(
    item_id: str,
    rounded_offer: int,
    short_name: str,
    year: Optional[int],
    mileage: Optional[int],
) -> str:
    raw = f"{item_id}:{rounded_offer}:{short_name}:{year or 0}:{mileage or 0}"
    return "msg_" + hashlib.sha256(raw.encode()).hexdigest()


def generate_offer_message(
    listing: Listing,
    deal: DealInput,
    out: DealOutput,
    conn: sqlite3.Connection,
    *,
    preferred_backend: str = "openai",
) -> str:
    """
    Generate a short, friendly eBay Best Offer message using Claude.

    The offer amount is rounded to the nearest GBP 50 for a natural feel.
    Repair costs are never mentioned in the message.
    Results are cached per (item_id, offer_amount, vehicle_name) so the API
    is not called again on re-runs.

    Falls back to a plain-text template if Claude is unavailable.
    """
    rounded_offer = round_to_nearest(out.max_bid, 50)
    short_name    = _short_vehicle_name(listing)
    cache_key     = _offer_cache_key(
        listing.item_id, rounded_offer, short_name,
        listing.year, listing.mileage,
    )

    # Cache hit
    cached = ai_cache_get(conn, cache_key)
    if cached and cached.get("message"):
        return str(cached["message"])

    mot_note = _mot_context_note(listing)
    mot_days = _get_mot_days_left(listing)

    # Determine MOT leverage status
    mot_status = "Clean MOT"
    if mot_days is not None:
        if mot_days < 90:
            mot_status = f"Short MOT ({mot_days} days left)"
        else:
            mot_status = f"Long MOT ({mot_days} days left)"
    elif not listing.mot_history:
        mot_status = "Unknown MOT"

    _year_str = str(listing.year) if listing.year else ""
    _miles_str = f"{listing.mileage:,} miles" if listing.mileage else ""
    vehicle_ctx = " ".join(filter(None, [_year_str, short_name]))
    if _miles_str:
        vehicle_ctx = f"{vehicle_ctx} — {_miles_str}"

    prompt = f"""Write a SHORT, specific UK used-car offer message for eBay.
Rules:
- Under 36 words
- Use the short vehicle name only (e.g. "{short_name}")
- Do NOT mention specific repair costs
- Round-number offer only — already calculated for you
- Professional, polite, and buyer-conscious (budget-aware)
- Start with "Hi," — do NOT start with "I"
- If the car has a "Short MOT", politely use it as leverage to justify the offer.
- Mention one concrete confidence signal (ready funds, quick collection, or same-day response).

Vehicle: {vehicle_ctx}
Listed price: £{listing.price_gbp:.0f}
Offer amount: £{rounded_offer}
MOT Status: {mot_status}
MOT Note (do NOT quote exactly): {mot_note or "none"}

Write ONLY the message. Nothing else."""

    text, backend = _ai_request(
        [{"role": "user", "content": prompt}],
        system="You write concise, professional used-car offer messages for a UK private buyer.",
        max_tokens=120,
        preferred_backend=preferred_backend,
    )

    # Fallback template if both backends fail
    if not text:
        if mot_note:
            text = (
                f"Hi, I'm interested in the {short_name}. "
                f"Having looked at comparable sales and condition, "
                f"I'd like to offer £{rounded_offer}. Can collect quickly. Thanks."
            )
        else:
            text = (
                f"Hi, I'd like to offer £{rounded_offer} for the {short_name}. "
                f"Happy to collect at your convenience. Thanks."
            )
        backend = "[template fallback]"

    cache_model = DEFAULT_OPENAI_MODEL if "OpenAI" in backend else DEFAULT_CLAUDE_MODEL
    try:
        ai_cache_put(conn, cache_key, cache_model, {"message": text})
    except Exception as e:
        print(f"  [cache] write failed ({e.__class__.__name__}), continuing")
    # Task 7: print AFTER success so output order is clear
    print(f"  Generated {backend}: {short_name[:50]}")
    return text

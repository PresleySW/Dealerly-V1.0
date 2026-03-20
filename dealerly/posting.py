"""
dealerly/posting.py
===================
Multi-platform listing generation and posting.

Responsibilities:
  - AI-powered listing description generation (Claude)
  - Price suggestion based on comps + analytics
  - Image tagging and quality assessment
  - Draft management (create, edit, approve)
  - Platform-specific formatting (eBay, Facebook, AutoTrader)
  - eBay listing creation via Trading API (future)

Depends on:
  - dealerly.config (POSTING_*, DEFAULT_CLAUDE_MODEL, CLAUDE_*)
  - dealerly.db (posting_draft_create, posting_draft_list, ai_cache_get/put)
  - dealerly.offers (_claude_request)
  - dealerly.ebay (guess_make_model)
  - dealerly.models (Listing, Lead, PostingDraft)

I/O: Claude API calls + SQLite reads/writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from dealerly.config import (
    DEFAULT_CLAUDE_MODEL,
    POSTING_MAX_DESCRIPTION_WORDS,
    POSTING_PLATFORMS,
)
from dealerly.db import (
    ai_cache_get,
    ai_cache_put,
    lead_get,
    posting_draft_create,
    posting_draft_list,
)
from dealerly.ebay import guess_make_model
from dealerly.models import Listing, PostingDraft
from dealerly.offers import _claude_request


# ---------------------------------------------------------------------------
# AI listing description generation
# ---------------------------------------------------------------------------

def generate_listing_description(
    title: str,
    vrm: str = "",
    year: Optional[int] = None,
    mileage: Optional[int] = None,
    fuel_type: str = "",
    colour: str = "",
    mot_status: str = "",
    mot_expiry: str = "",
    condition_notes: str = "",
    repair_notes: str = "",
    price: float = 0.0,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, str]:
    """
    Generate an AI-powered listing description using Claude.

    Returns dict with:
      title: optimised listing title
      description: full listing description
      bullet_points: JSON list of key selling points
      seo_keywords: comma-separated keywords for search optimisation

    Falls back to template-based generation if Claude is unavailable.
    """
    # Cache check
    cache_key = f"listing_desc_{hashlib.sha256(f'{title}:{vrm}:{price}'.encode()).hexdigest()}"
    if conn:
        cached = ai_cache_get(conn, cache_key)
        if cached:
            return cached

    guess = guess_make_model(title)
    vehicle_name = f"{guess.make.title()} {guess.model.title()}"
    if guess.make == "unknown":
        vehicle_name = title[:50]

    # Build context for Claude
    details = []
    if year:
        details.append(f"Year: {year}")
    if mileage:
        details.append(f"Mileage: {mileage:,} miles")
    if fuel_type:
        details.append(f"Fuel: {fuel_type.title()}")
    if colour:
        details.append(f"Colour: {colour}")
    if mot_status:
        details.append(f"MOT status: {mot_status}")
    if mot_expiry:
        details.append(f"MOT expires: {mot_expiry}")
    if condition_notes:
        details.append(f"Condition: {condition_notes}")
    details_str = "\n".join(details) if details else "No additional details available"

    prompt = f"""Generate a compelling used car listing for a UK dealer.

Vehicle: {vehicle_name}
Original title: {title}
Price: £{price:.0f}
{details_str}

Provide your response as JSON with these keys:
1. "title" — Optimised listing title (max 80 chars), include year, make, model, key specs
2. "description" — Professional description ({POSTING_MAX_DESCRIPTION_WORDS} words max).
   Include: overview, key features, condition, practical info. Honest but positive.
   UK English. No emojis. No ALL CAPS.
3. "bullet_points" — JSON array of 5-8 key selling points (short phrases)
4. "seo_keywords" — Comma-separated search keywords

Respond ONLY with valid JSON. No markdown fences."""

    text = _claude_request(
        [{"role": "user", "content": prompt}],
        system=(
            "You are an expert UK used car dealer writing compelling, "
            "honest eBay/Facebook/AutoTrader listings. Write in British "
            "English. Be professional but approachable."
        ),
        max_tokens=800,
    )

    result: Dict[str, str] = {}

    if text:
        try:
            # Strip markdown fences if present
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()
            if clean.startswith("json"):
                clean = clean[4:].strip()
            result = json.loads(clean)
        except (json.JSONDecodeError, IndexError):
            result = {}

    # Fallback template if AI failed
    if not result.get("description"):
        result = _template_listing(
            vehicle_name, title, year, mileage, fuel_type,
            colour, mot_status, mot_expiry, price,
        )

    # Cache the result
    if conn:
        ai_cache_put(conn, cache_key, DEFAULT_CLAUDE_MODEL, result)

    return result


def _template_listing(
    vehicle_name: str,
    title: str,
    year: Optional[int],
    mileage: Optional[int],
    fuel_type: str,
    colour: str,
    mot_status: str,
    mot_expiry: str,
    price: float,
) -> Dict[str, str]:
    """Fallback template-based listing when AI is unavailable."""
    year_str = str(year) if year else ""
    mile_str = f"{mileage:,} miles" if mileage else ""
    fuel_str = fuel_type.title() if fuel_type else ""

    opt_title = f"{year_str} {vehicle_name}"
    if fuel_str:
        opt_title += f" {fuel_str}"
    if mile_str:
        opt_title += f" {mile_str}"
    opt_title = opt_title.strip()[:80]

    desc_parts = [f"For sale: {opt_title}."]
    if mile_str:
        desc_parts.append(f"Currently showing {mile_str} on the clock.")
    if mot_status:
        desc_parts.append(f"MOT {mot_status}.")
        if mot_expiry:
            desc_parts.append(f"MOT valid until {mot_expiry}.")
    if colour:
        desc_parts.append(f"Finished in {colour}.")
    desc_parts.append("Starts and drives well.")
    desc_parts.append(f"Priced to sell at £{price:.0f}.")
    desc_parts.append("Any questions, please ask. Viewings welcome.")

    bullets = [b for b in [
        f"{year_str} {vehicle_name}" if year_str else vehicle_name,
        mile_str,
        fuel_str,
        f"{colour}" if colour else "",
        f"MOT until {mot_expiry}" if mot_expiry else "",
        "Starts and drives",
    ] if b]

    return {
        "title": opt_title,
        "description": " ".join(desc_parts),
        "bullet_points": json.dumps(bullets),
        "seo_keywords": f"{vehicle_name}, {fuel_str}, used car, {year_str}".strip(", "),
    }


# ---------------------------------------------------------------------------
# Price suggestion
# ---------------------------------------------------------------------------

def suggest_listing_price(
    buy_price: float,
    expected_resale: float,
    repairs: float = 0,
    target_margin_pct: float = 0.15,
) -> Dict[str, float]:
    """
    Suggest listing prices based on cost basis and market data.

    Returns dict with:
      floor_price: minimum to break even
      target_price: price to hit target margin
      market_price: expected resale from comps
      optimistic_price: market_price + 10%
    """
    total_cost = buy_price + repairs
    floor = total_cost * 1.08  # 8% for fees
    target = total_cost * (1 + target_margin_pct) + total_cost * 0.08
    market = expected_resale
    optimistic = market * 1.10

    return {
        "floor_price": round(floor, 0),
        "target_price": round(target, 0),
        "market_price": round(market, 0),
        "optimistic_price": round(optimistic, 0),
    }


# ---------------------------------------------------------------------------
# Draft management
# ---------------------------------------------------------------------------

def create_posting_draft(
    conn: sqlite3.Connection,
    lead_id: Optional[int] = None,
    title: str = "",
    vrm: str = "",
    price: float = 0.0,
    year: Optional[int] = None,
    mileage: Optional[int] = None,
    fuel_type: str = "",
    colour: str = "",
    mot_status: str = "",
    mot_expiry: str = "",
    condition_notes: str = "",
    platforms: str = "",
    image_paths: str = "",
) -> int:
    """
    Create a posting draft with AI-generated content.

    Returns the draft ID.
    """
    # Generate AI description
    ai_content = generate_listing_description(
        title=title, vrm=vrm, year=year, mileage=mileage,
        fuel_type=fuel_type, colour=colour,
        mot_status=mot_status, mot_expiry=mot_expiry,
        condition_notes=condition_notes, price=price, conn=conn,
    )

    # Suggest price
    price_suggestion = suggest_listing_price(price, price * 1.2)

    draft_id = posting_draft_create(
        conn,
        lead_id=lead_id,
        vrm=vrm,
        title=ai_content.get("title", title),
        description=ai_content.get("description", ""),
        price_gbp=price,
        suggested_price=price_suggestion["target_price"],
        ai_description=ai_content.get("description", ""),
        ai_bullet_points=ai_content.get("bullet_points", "[]"),
        ai_price_suggestion=price_suggestion["market_price"],
        ai_image_tags="",
        platforms=platforms or ",".join(POSTING_PLATFORMS),
        image_paths=image_paths,
    )

    # Link draft to lead if applicable
    if lead_id:
        from dealerly.db import lead_update_fields
        lead_update_fields(conn, lead_id, listing_draft_id=draft_id)

    return draft_id


# ---------------------------------------------------------------------------
# Platform-specific formatting
# ---------------------------------------------------------------------------

def format_for_ebay(draft: Dict[str, Any]) -> Dict[str, str]:
    """Format a posting draft for eBay listing."""
    bullets = json.loads(draft.get("ai_bullet_points", "[]"))

    # eBay HTML description
    html = f"""<div style="font-family: Arial, sans-serif; max-width: 800px;">
<h2>{draft.get('title', '')}</h2>
<p>{draft.get('ai_description', draft.get('description', ''))}</p>
<h3>Key Features</h3>
<ul>
{''.join(f'<li>{b}</li>' for b in bullets)}
</ul>
<p><strong>Price: £{draft.get('price_gbp', 0):.0f}</strong></p>
<p>Any questions please ask. Viewings by appointment.</p>
</div>"""

    return {
        "title": draft.get("title", "")[:80],
        "description_html": html,
        "price": str(draft.get("price_gbp", 0)),
        "condition": "Used",
    }


def format_for_facebook(draft: Dict[str, Any]) -> Dict[str, str]:
    """Format a posting draft for Facebook Marketplace."""
    bullets = json.loads(draft.get("ai_bullet_points", "[]"))
    bullet_text = "\n".join(f"✓ {b}" for b in bullets)

    text = f"""{draft.get('title', '')}

{draft.get('ai_description', draft.get('description', ''))}

{bullet_text}

£{draft.get('price_gbp', 0):.0f} — Message for more info or to arrange viewing."""

    return {
        "title": draft.get("title", "")[:100],
        "description": text,
        "price": str(draft.get("price_gbp", 0)),
    }


def format_for_autotrader(draft: Dict[str, Any]) -> Dict[str, str]:
    """Format a posting draft for AutoTrader."""
    return {
        "title": draft.get("title", "")[:100],
        "description": draft.get("ai_description", draft.get("description", "")),
        "price": str(draft.get("price_gbp", 0)),
        "vrm": draft.get("vrm", ""),
    }

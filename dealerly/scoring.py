"""
dealerly/scoring.py
===================
Core deal scoring pipeline.

v0.9.3 changes:
  - Passes make/model to evaluate_deal() for model-aware shock threshold
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from dealerly.autotrader import AutoTraderComps
from dealerly.config import DEFAULT_EXPECTED_DAYS, DEFAULT_TOP_N
from dealerly.db import insert_comps, load_recent_comps, upsert_verified_vehicle, get_verified_vehicle
from dealerly.ebay import guess_make_model, vehicle_key_from_title
from dealerly.models import DealInput, DealOutput, Listing
from dealerly.mot import MOTProvider
from dealerly.repair import (
    estimate_p_mot_from_signals,
    estimate_repairs,
    mot_uplift_and_confidence,
)
from dealerly.vrm import contains_category_s_signal
from dealerly.risk import (
    apply_vrm_buy_gate,
    estimate_fees,
    evaluate_deal,
    mileage_correction,
)
from dealerly.utils import median, now_utc_iso


# ---------------------------------------------------------------------------
# Fraud signals
# ---------------------------------------------------------------------------

_FRAUD_TEXT_SIGNALS: List[Tuple[int, str, str]] = [
    (99, "shipping agent",              "shipping agent scam"),
    (95, "located overseas",            "seller overseas scam"),
    (95, "currently abroad",            "seller overseas scam"),
    (95, "military personnel",          "military scam"),
    (95, "armed forces",                "military scam"),
    (85, "photos for illustration",     "stock photo listing"),
    (85, "photos are for illustration", "stock photo listing"),
    (80, "outstanding finance",         "outstanding finance risk"),
    (90, "needs new engine",            "major powertrain issue"),
    (88, "new engine needed",           "major powertrain issue"),
    (45, "on finance",                  "may have outstanding finance"),
    (70, "category s",                  "Cat S structural damage"),
    (70, "cat s",                       "Cat S structural damage"),
    (70, "s category",                  "Cat S structural damage"),
    (70, "categorys",                   "Cat S structural damage"),
    (68, "structural",                  "structural damage risk"),
    (65, "category n",                  "Cat N non-structural damage"),
    (65, "cat n",                       "Cat N non-structural damage"),
    (60, "selling on behalf",           "third-party seller risk"),
    (60, "on behalf of",               "third-party seller risk"),
    (55, "no v5",                       "no V5 logbook"),
    (55, "no log book",                 "no logbook"),
    (55, "no logbook",                  "no logbook"),
    (40, "cash only",                   "cash only payment"),
    (40, "collection only cash",        "cash only payment"),
]


def fraud_score(
    listing: Listing,
    comps_median: Optional[float],
) -> Tuple[int, List[str]]:
    """
    Return (score 0-100, [reasons]).
    >=80: filter silently. 60-79: AVOID. <60: pass through.
    """
    text  = (listing.title + " " + str(listing.raw.get("shortDescription", ""))).lower()
    score = 0
    flags: List[str] = []

    for sig_score, keyword, reason in _FRAUD_TEXT_SIGNALS:
        if keyword in text:
            score = max(score, sig_score)
            flags.append(reason)

    if comps_median and comps_median > 0:
        ratio = listing.price_gbp / comps_median
        if ratio < 0.30:
            score = max(score, 90)
            flags.append(f"price {ratio:.0%} of median (likely scam/salvage)")
        elif ratio < 0.45:
            score = max(score, 65)
            flags.append(f"price {ratio:.0%} of median (unusually cheap)")

    return score, flags


# ---------------------------------------------------------------------------
# Write-off category detection (v0.9.10)
# ---------------------------------------------------------------------------
# Cat S = structural damage, Cat N = non-structural. Both severely reduce
# resale value (25-35%) but many sellers still list at near-market prices.
# Detection runs on title + description text before resale estimation.

_WRITEOFF_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # Cat N checked FIRST — "non-structural" contains "structural" as a substring,
    # so Cat N must match before Cat S to avoid false positives.
    ("Cat N", re.compile(
        r"\b(?:cat(?:egory)?\s*n\b|write[\s-]?off\s*(?:cat(?:egory)?)?\s*n\b"
        r"|non[\s-]?structural\s+(?:damage|write[\s-]?off))",
        re.IGNORECASE,
    )),
    ("Cat S", re.compile(
        r"\b(?:cat(?:egory)?[\s-]*s\b|s[\s-]*category\b|categorys\b|write[\s-]?off\s*(?:cat(?:egory)?)?[\s-]*s\b"
        r"|structural(?:\s+(?:damage|write[\s-]?off))?)",
        re.IGNORECASE,
    )),
]

# Resale reduction factors — Cat S is structural (worse), Cat N is cosmetic
WRITEOFF_RESALE_DISCOUNT: Dict[str, float] = {
    "Cat S": 0.70,   # 30% reduction
    "Cat N": 0.75,   # 25% reduction
}

# Additional expected-resale pressure for fuzzy Cat S signal hits.
_CATEGORY_S_HIGH_RISK_RESALE_FACTOR: float = 0.85


def detect_writeoff_category(listing: Listing) -> str:
    """
    Scan listing title + description for Cat S/N write-off indicators.

    Returns "Cat S", "Cat N", or "" (no write-off detected).
    Cat S is checked first — if both match, structural takes precedence.
    """
    raw = listing.raw or {}
    text_parts = [
        listing.title or "",
        str(raw.get("shortDescription", "") or ""),
        str(raw.get("description", "") or ""),
        str(raw.get("itemDescription", "") or ""),
        str(raw.get("enriched_description_text", "") or ""),
        str(raw.get("enriched_text_blob", "") or ""),
        str(raw.get("subtitle", "") or ""),
        str(raw.get("conditionDescription", "") or ""),
        str(raw.get("itemSpecifics", "") or ""),
        str(raw.get("localizedAspects", "") or ""),
    ]
    text = " ".join(text_parts)
    for category, pattern in _WRITEOFF_PATTERNS:
        if pattern.search(text):
            return category
    if contains_category_s_signal(text):
        return "Cat S"
    return ""


# ---------------------------------------------------------------------------
# MOT cache helpers
# ---------------------------------------------------------------------------

def _mot_cache_get(
    conn: sqlite3.Connection,
    vrm: str,
    cache_hours: float,
) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("SELECT fetched_at, payload_json FROM mot_cache WHERE vrm = ?", (vrm,))
    row = cur.fetchone()
    if not row:
        return None
    age_h = (
        datetime.now(timezone.utc)
        - datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    ).total_seconds() / 3600
    return json.loads(row[1]) if age_h <= cache_hours else None


def _mot_cache_put(
    conn: sqlite3.Connection,
    vrm: str,
    provider_name: str,
    payload: Dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO mot_cache (vrm, fetched_at, provider, payload_json)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(vrm) DO UPDATE SET"
        "   fetched_at   = excluded.fetched_at,"
        "   provider     = excluded.provider,"
        "   payload_json = excluded.payload_json",
        (vrm, now_utc_iso(), provider_name, json.dumps(payload)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Resale estimation
# ---------------------------------------------------------------------------

def estimate_resale_and_days(
    *,
    conn: sqlite3.Connection,
    listing: Listing,
    fetch_ebay_comps_fn: Callable[[str], List[Tuple]],
    at_comps: Optional[AutoTraderComps],
    comps_ttl: float,
    store_comps: bool,
    resale_discount: float,
) -> Tuple[float, int, str, Optional[float], int]:
    """
    Resolve expected resale price and days-to-sell.

    Resolution order (first match wins):
      0. CSV override  - user-supplied expected_resale on the Listing
      1. AutoTrader    - retail comps if >=5 prices
      2. eBay cached   - DB comps if >=10 prices and fresh
      3. eBay fresh    - live search if >=6 prices returned
      4. Fallback      - 1.10x buy price (mileage-adjusted)

    Returns (resale, days, source_note, comps_median|None, n_comps).
    """
    # 0) CSV override
    if listing.csv_expected_resale and listing.csv_expected_resale > 0:
        days = listing.csv_expected_days or DEFAULT_EXPECTED_DAYS
        return (
            float(listing.csv_expected_resale),
            int(days),
            f"resale: CSV user-supplied ({listing.csv_expected_resale:.0f}, {days}d)",
            None,
            0,
        )

    key      = vehicle_key_from_title(listing.title)
    mil_corr = mileage_correction(listing.mileage)
    mil_note = (
        f" mileage_adj={mil_corr:.2f} ({listing.mileage:,}mi)"
        if listing.mileage else " mileage=unknown"
    )

    def _days_from_mileage(mileage: Optional[int]) -> int:
        """Lower mileage cars sell faster. Baseline 12 days at 80k miles."""
        if not mileage or mileage <= 0:
            return DEFAULT_EXPECTED_DAYS
        if mileage < 30_000:   return 7
        if mileage < 50_000:   return 9
        if mileage < 70_000:   return 11
        if mileage < 100_000:  return DEFAULT_EXPECTED_DAYS
        if mileage < 130_000:  return 16
        return 20

    def _apply(raw_median: float, n: int, source: str) -> Tuple[float, int, str, float, int]:
        adjusted = float(raw_median * resale_discount * mil_corr)
        days = _days_from_mileage(listing.mileage)
        return adjusted, days, f"resale: {source} (n={n}){mil_note}", raw_median, n

    # 1) AutoTrader
    if at_comps:
        at_prices = at_comps.fetch_for_key(key, conn, comps_ttl)
        if len(at_prices) >= 3:
            mv = median(at_prices)
            if mv:
                return _apply(mv, len(at_prices), f"AutoTrader median x{resale_discount}")

    # 2) eBay cached
    cached = load_recent_comps(conn, key, comps_ttl)
    if len(cached) >= 10:
        mv = median(cached)
        if mv:
            return _apply(mv, len(cached), f"eBay comps cached x{resale_discount}")

    # 3) eBay fresh
    fresh_rows   = fetch_ebay_comps_fn(key)
    fresh_prices = [p for (p, *_) in fresh_rows]
    if store_comps and fresh_rows:
        insert_comps(conn, key, fresh_rows)
    if len(fresh_prices) >= 6:
        mv = median(fresh_prices)
        if mv:
            return _apply(mv, len(fresh_prices), f"eBay comps fresh x{resale_discount}")

    # 4) Fallback
    # v0.9.9+: raised multiplier 1.30→1.35. At £3k capital with typical costs
    # (repairs £0–200, holding £90, fees £156, buffers £70) a 1.30x fallback
    # generated £0–80 profit — barely OFFER. 1.35x gives £80–150 margin on
    # clean comp-less listings, enough to surface genuine sub-market buys.
    # Still conservative vs observed 30-40% retail uplift on sub-£2500 cars.
    fallback = float(listing.price_gbp * 1.35 * mil_corr)
    days = _days_from_mileage(listing.mileage)
    return fallback, days, f"resale: fallback 1.35x (no comps){mil_note}", None, 0


def _extract_latest_mot_mileage(payload: Optional[dict]) -> Optional[int]:
    """
    Best-effort extraction of latest MOT odometer reading from DVSA payload.
    """
    if not payload:
        return None
    tests = payload.get("motTests") or []
    if not isinstance(tests, list) or not tests:
        return None
    for test in tests:
        if not isinstance(test, dict):
            continue
        raw = test.get("odometerValue")
        if raw is None:
            continue
        s = str(raw).strip().replace(",", "")
        m = re.search(r"\d{3,7}", s)
        if not m:
            continue
        try:
            v = int(m.group(0))
        except ValueError:
            continue
        if 1_000 <= v <= 500_000:
            return v
    return None


# ---------------------------------------------------------------------------
# Main scoring pipeline
# ---------------------------------------------------------------------------

def score_listings(
    listings: List[Listing],
    *,
    conn: sqlite3.Connection,
    capital: float,
    target_margin: float,
    holding_cost: float,
    mot_provider: Optional[MOTProvider],
    ebay_fee_rate: float,
    pay_fee_rate: float,
    admin_buffer: float,
    transport_buffer: float,
    fetch_ebay_comps_fn: Callable[[str], List[Tuple]],
    at_comps: Optional[AutoTraderComps],
    comps_ttl: float,
    store_comps: bool,
    resale_discount: float,
    misprice_ratio: float,
    require_comps: bool,
    mot_cache_hours: float = 24.0,
    top_n: int = DEFAULT_TOP_N,
) -> Tuple[List[Tuple[Listing, DealInput, DealOutput]], Dict[str, int]]:
    """
    Score a list of listings through the full deal evaluation pipeline.

    Steps per listing:
      1. Resolve resale price
      2. Pre-filters (no-comps, misprice, fraud)
      3. Estimate repairs
      4. MOT lookup + uplift
      5. Build DealInput and evaluate_deal()
      6. VRM gate + fraud override
      7. Annotate notes

    Returns (top_n ranked results, stats dict).
    """
    scored: List[Tuple[Listing, DealInput, DealOutput]] = []
    stats: Dict[str, int] = {
        "avoid_shock": 0, "buy": 0, "offer": 0, "pass": 0, "total": 0,
        "filtered_misprice": 0, "filtered_nocomps": 0, "filtered_fraud": 0,
    }

    for listing in listings:
        # 1. Resale
        exp_res, exp_days, res_src, comps_med, _ = estimate_resale_and_days(
            conn=conn, listing=listing,
            fetch_ebay_comps_fn=fetch_ebay_comps_fn,
            at_comps=at_comps, comps_ttl=comps_ttl,
            store_comps=store_comps, resale_discount=resale_discount,
        )

        # 2. Pre-filters
        if require_comps and comps_med is None:
            stats["filtered_nocomps"] += 1
            continue
        if comps_med and listing.price_gbp > comps_med * misprice_ratio:
            stats["filtered_misprice"] += 1
            continue
        fscore, fflags = fraud_score(listing, comps_med)
        if fscore >= 80:
            stats["filtered_fraud"] += 1
            continue

        # 2b. Write-off detection (v0.9.10)
        listing.writeoff_category = detect_writeoff_category(listing)
        if listing.writeoff_category:
            discount = WRITEOFF_RESALE_DISCOUNT.get(listing.writeoff_category, 0.75)
            exp_res *= discount
            res_src += f" | {listing.writeoff_category} write-off → resale x{discount:.2f}"
            if listing.writeoff_category == "Cat S":
                exp_res *= _CATEGORY_S_HIGH_RISK_RESALE_FACTOR
                res_src += (
                    f" | Cat S high-risk penalty → resale x"
                    f"{_CATEGORY_S_HIGH_RISK_RESALE_FACTOR:.2f}"
                )

        # 3. Repairs
        guess = guess_make_model(listing.title)
        base_rep, worst_rep, profile_notes = estimate_repairs(
            listing.title, guess.make, guess.model,
            fuel_type=listing.fuel_type or guess.fuel_type,
            condition_notes=" ".join(
                [
                    str(listing.raw.get("shortDescription", "") or ""),
                    str(listing.raw.get("description", "") or ""),
                    str(listing.raw.get("itemDescription", "") or ""),
                    str(listing.raw.get("enriched_description_text", "") or ""),
                    str(listing.raw.get("conditionDescription", "") or ""),
                    str(listing.raw.get("subtitle", "") or ""),
                    str(listing.raw.get("itemSpecifics", "") or ""),
                ]
            ),
        )

        # 4. MOT
        #    v0.9.5: signal-based p_mot when no VRM/DVSA data available.
        #    Previously hardcoded to 0.92 for all listings without VRM,
        #    which made the column meaningless. Now uses title keywords,
        #    age, mileage, and model reliability tier for a realistic spread.
        p_mot, mot_notes = estimate_p_mot_from_signals(
            listing.title,
            year=listing.year or guess.year,
            mileage=listing.mileage or guess.mileage,
            make=guess.make, model=guess.model,
        )
        if mot_provider and not listing.vrm:
            mot_notes = f"MOT: DVSA enabled but no verified VRM — {mot_notes}"

        if mot_provider and listing.vrm:
            payload: Optional[Dict[str, Any]] = None
            try:
                # 1. Check short-term cache (24hr TTL)
                payload = _mot_cache_get(conn, listing.vrm, mot_cache_hours)
                if payload is not None:
                    mot_notes = f"MOT: cache hit for {listing.vrm}"
                    n_tests = len((payload or {}).get("motTests") or [])
                    print(f"  [MOT] {listing.vrm}: cache hit ({n_tests} tests)")
                else:
                    # 2. Check verified_vehicles (30-day persistent store)
                    payload = get_verified_vehicle(conn, listing.vrm, max_age_days=30)
                    if payload is not None:
                        n_tests = len((payload or {}).get("motTests") or [])
                        mot_notes = f"MOT: DB verified (30d) for {listing.vrm}"
                        print(f"  [MOT] {listing.vrm}: DB verified ({n_tests} tests)")
                        # Also populate short-term cache so it's fast on repeat runs
                        _mot_cache_put(conn, listing.vrm, "db_verified", payload)
                    else:
                        # 3. Fetch live from DVSA
                        import time as _time
                        print(f"  [MOT] {listing.vrm}: fetching via {mot_provider.provider_name}...")
                        payload = mot_provider.fetch(listing.vrm)
                        _time.sleep(0.4)   # DVSA rate-limit guard
                        if payload is not None:
                            _mot_cache_put(conn, listing.vrm, mot_provider.provider_name, payload)
                            upsert_verified_vehicle(conn, listing.vrm, payload)
                            n_tests = len((payload or {}).get("motTests") or [])
                            mot_notes = f"MOT: DVSA fetched for {listing.vrm}"
                            print(f"  [MOT] {listing.vrm}: fetched OK ({n_tests} tests) — saved to DB")
                        else:
                            mot_notes = f"MOT: DVSA returned nothing for {listing.vrm}"
                            print(f"  [MOT] {listing.vrm}: 404 / not found in DVSA")
            except Exception as exc:
                mot_notes = f"MOT: error ({type(exc).__name__}: {str(exc)[:100]})"
                print(f"  [MOT] {listing.vrm}: ERROR — {mot_notes}")

            if payload is not None:
                listing.mot_history = payload
                if not listing.mileage:
                    mot_mileage = _extract_latest_mot_mileage(payload)
                    if mot_mileage:
                        listing.mileage = mot_mileage
                mu_b, mu_w, p_mot, mot_notes = mot_uplift_and_confidence(payload)
                base_rep  += mu_b
                worst_rep += mu_w

        # 5. Deal evaluation
        fees = estimate_fees(exp_res, ebay_fee_rate, pay_fee_rate)
        ulez_str = (
            "ULEZ:yes" if listing.ulez_compliant
            else ("ULEZ:no" if listing.ulez_compliant is False else "ULEZ:?")
        )
        deal = DealInput(
            reg=listing.vrm, capital_available=capital,
            buy_price=listing.price_gbp, expected_resale=float(exp_res),
            base_repair_estimate=float(base_rep),
            worst_case_repair=float(max(worst_rep, base_rep)),
            expected_days_to_sell=int(exp_days),
            holding_cost=float(holding_cost), target_margin=float(target_margin),
            fees_total=float(fees), admin_buffer=float(admin_buffer),
            transport_buffer=float(transport_buffer),
            repair_profile_notes=profile_notes,
        )
        # v0.9.3: pass make/model for model-aware shock threshold
        out = evaluate_deal(deal, make=guess.make, model=guess.model)

        # 6. Gates
        out.decision, out.reason = apply_vrm_buy_gate(
            out.decision, out.reason,
            mot_enabled=bool(mot_provider), vrm=listing.vrm,
            vrm_source=listing.vrm_source, vrm_confidence=listing.vrm_confidence,
        )
        # Enforce actionable confidence: with MOT mode enabled, unverified MOT
        # should not stay BUY even if projected economics look strong.
        if mot_provider and out.decision == "BUY" and not listing.mot_history:
            out.decision = "OFFER"
            out.reason = "No DVSA MOT history yet — treat as OFFER until verified."
        if fscore >= 60 and out.decision != "AVOID":
            out.decision = "AVOID"
            out.reason   = f"Risk flags: {'; '.join(fflags)}"
        # Cat S is treated as structurally high-risk: add decision pressure on top
        # of resale discounting to avoid false-positive BUY recommendations.
        if listing.writeoff_category == "Cat S":
            out.max_bid = max(0.0, out.max_bid * 0.85)
            out.expected_profit *= 0.82
            if out.decision == "BUY":
                out.decision = "OFFER"
                out.reason = "Cat S structural risk detected — downgrade BUY to OFFER."
            elif out.decision == "OFFER" and out.expected_profit < (target_margin * 0.60):
                out.decision = "PASS"
                out.reason = "Cat S structural risk + thin margin after penalty."

        # 7. Annotate
        out.p_mot  = p_mot
        vrm_meta   = f"vrm_src={listing.vrm_source or '-'} conf={listing.vrm_confidence:.0%}"
        fraud_note = f" | fraud={fscore}" if fflags else ""
        out.notes  = (
            f"{mot_notes} | {vrm_meta} | {ulez_str}"
            f" | {res_src} | fees~{fees:.0f}{fraud_note}"
        )

        _DEC_STAT = {"AVOID": "avoid_shock", "BUY": "buy", "OFFER": "offer"}
        stats[_DEC_STAT.get(out.decision, "pass")] += 1
        scored.append((listing, deal, out))

    stats["total"] = len(scored)

    def _rank_key(row: Tuple) -> float:
        l, _, o = row
        tier = {"BUY": 3, "OFFER": 2, "PASS": 1, "AVOID": 0}.get(o.decision, 0)
        # v0.9.5.2: VRM verification bonus — prefer actionable intel within same tier
        vrm_bonus = 0.0
        if l.mot_history:   vrm_bonus = 0.15   # DVSA verified = strong
        elif l.vrm:         vrm_bonus = 0.08   # VRM found but no MOT data
        # v0.9.7: mileage bonus — lower mileage sells faster, higher velocity
        mileage_bonus = 0.0
        if l.mileage:
            if l.mileage < 40_000:    mileage_bonus = 0.12
            elif l.mileage < 65_000:  mileage_bonus = 0.07
            elif l.mileage < 90_000:  mileage_bonus = 0.03
        # Within a decision tier, prefer stronger unit economics first so "top"
        # cards are not dominated by thin-margin listings.
        profit = float(o.expected_profit or 0.0)
        profit_score = max(-200.0, min(900.0, profit))
        margin_ratio = (profit / target_margin) if target_margin > 0 else 0.0
        margin_score = max(-1.0, min(2.5, margin_ratio))
        velocity_quality = (
            o.velocity_score * (1 - min(o.shock_impact_ratio, 1.0)) * (1 + vrm_bonus + mileage_bonus)
        )
        return (
            tier * 1e6
            + profit_score * 500.0
            + margin_score * 10_000.0
            + velocity_quality * 100.0
        )

    scored.sort(key=_rank_key, reverse=True)
    return scored[:top_n], stats

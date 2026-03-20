"""
dealerly/analytics.py
=====================
Predictive analytics for Dealerly.

Responsibilities:
  - Record price observations from every pipeline run
  - Compute price trends (rising/falling/stable) per vehicle key
  - Compute demand signals (high/medium/low) per vehicle key
  - Predict deal success probability using historical data
  - Seasonal adjustment factors for UK used car market

All analytics are derived from the price_observations table in SQLite,
which accumulates data across runs. More data = better predictions.

Depends on:
  - dealerly.config (SEASONAL_FACTORS, ANALYTICS_*)
  - dealerly.db (insert_price_observation, load_price_observations)
  - dealerly.ebay (vehicle_key_from_title, guess_make_model)
  - dealerly.utils (median)

No external API calls. Pure computation on local data.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dealerly.config import (
    ANALYTICS_DEMAND_HIGH_THRESHOLD,
    ANALYTICS_DEMAND_LOW_THRESHOLD,
    ANALYTICS_MIN_OBSERVATIONS,
    ANALYTICS_TREND_WINDOW_DAYS,
    SEASONAL_FACTORS,
)
from dealerly.db import insert_price_observation, load_price_observations
from dealerly.ebay import guess_make_model, vehicle_key_from_title
from dealerly.models import DealInput, DealOutput, Listing
from dealerly.utils import median


# ---------------------------------------------------------------------------
# Record observations
# ---------------------------------------------------------------------------

def record_price_observations(
    conn: sqlite3.Connection,
    listings: List[Listing],
) -> int:
    """
    Record a price observation for each listing.
    Called at Phase 6 — builds the historical dataset for trend analysis.
    Returns the number of observations recorded.
    """
    count = 0
    for listing in listings:
        try:
            key = vehicle_key_from_title(listing.title)
            insert_price_observation(
                conn,
                vehicle_key=key,
                platform=listing.platform,
                price=listing.price_gbp,
                mileage=listing.mileage,
                year=listing.year,
                location=listing.location,
                item_id=listing.item_id,
                url=listing.url,
            )
            count += 1
        except Exception:
            pass
    return count


# ---------------------------------------------------------------------------
# Price trends
# ---------------------------------------------------------------------------

def compute_price_trends(
    conn: sqlite3.Connection,
    vehicle_key: str,
    window_days: int = ANALYTICS_TREND_WINDOW_DAYS,
) -> Optional[Dict[str, Any]]:
    """
    Compute price trend for a vehicle key.

    Returns dict with:
      direction: "rising" | "falling" | "stable"
      pct_change_7d: % change over last 7 days
      pct_change_30d: % change over last 30 days
      current_median: current median price
      sample_size: number of observations used
      confidence: 0-1 based on sample size

    Returns None if insufficient data.
    """
    obs_30d = load_price_observations(conn, vehicle_key, days=30)
    if len(obs_30d) < ANALYTICS_MIN_OBSERVATIONS:
        return None

    prices_30d = [o["price"] for o in obs_30d]
    median_30d = median(prices_30d)
    if not median_30d or median_30d <= 0:
        return None

    # Split into recent (7d) vs older (8-30d)
    obs_7d = [o for o in obs_30d if o["age_days"] <= 7]
    obs_old = [o for o in obs_30d if o["age_days"] > 7]

    prices_7d = [o["price"] for o in obs_7d] if obs_7d else prices_30d
    prices_old = [o["price"] for o in obs_old] if obs_old else prices_30d

    median_7d = median(prices_7d) or median_30d
    median_old = median(prices_old) or median_30d

    # Percentage changes
    pct_7d = ((median_7d - median_old) / median_old * 100) if median_old else 0
    pct_30d = 0.0
    obs_full = load_price_observations(conn, vehicle_key, days=60)
    obs_older = [o for o in obs_full if o["age_days"] > 30]
    if obs_older:
        median_older = median([o["price"] for o in obs_older])
        if median_older and median_older > 0:
            pct_30d = (median_30d - median_older) / median_older * 100

    # Direction
    if pct_7d > 3:
        direction = "rising"
    elif pct_7d < -3:
        direction = "falling"
    else:
        direction = "stable"

    # Confidence based on sample size
    confidence = min(1.0, len(obs_30d) / 30)

    return {
        "direction": direction,
        "pct_change_7d": round(pct_7d, 1),
        "pct_change_30d": round(pct_30d, 1),
        "current_median": round(median_7d, 0),
        "sample_size": len(obs_30d),
        "confidence": round(confidence, 2),
    }


# ---------------------------------------------------------------------------
# Demand signals
# ---------------------------------------------------------------------------

def compute_demand_signals(
    conn: sqlite3.Connection,
    vehicle_key: str,
) -> Optional[Dict[str, Any]]:
    """
    Compute demand signal for a vehicle key.

    Returns dict with:
      level: "high" | "medium" | "low"
      listings_per_day: average new listings per day (last 7 days)
      avg_days_on_market: estimated days on market
      seasonal_factor: current month's seasonal adjustment
      competition_count: active listings count

    Returns None if insufficient data.
    """
    obs_7d = load_price_observations(conn, vehicle_key, days=7)
    obs_30d = load_price_observations(conn, vehicle_key, days=30)

    if not obs_30d:
        return None

    # Listings per day (last 7 days)
    listings_per_day = len(obs_7d) / 7 if obs_7d else len(obs_30d) / 30

    # Estimate avg days on market from listing frequency
    # Higher listing frequency = faster turnover = higher demand
    # This is a rough proxy — real data would come from tracking
    # individual listings appearing and disappearing
    if listings_per_day >= ANALYTICS_DEMAND_HIGH_THRESHOLD:
        level = "high"
        avg_days = 8
    elif listings_per_day >= ANALYTICS_DEMAND_LOW_THRESHOLD:
        level = "medium"
        avg_days = 14
    else:
        level = "low"
        avg_days = 21

    # Seasonal factor
    month = datetime.now().month - 1  # 0-indexed
    seasonal = SEASONAL_FACTORS[month] if month < len(SEASONAL_FACTORS) else 1.0

    return {
        "level": level,
        "listings_per_day": round(listings_per_day, 2),
        "avg_days_on_market": avg_days,
        "seasonal_factor": seasonal,
        "competition_count": len(obs_7d),
    }


# ---------------------------------------------------------------------------
# Deal success prediction
# ---------------------------------------------------------------------------

def predict_deal_success(
    listing: Listing,
    deal: DealInput,
    out: DealOutput,
    trend: Optional[Dict[str, Any]],
    demand: Optional[Dict[str, Any]],
) -> float:
    """
    Predict the probability of a deal being successful (profitable).

    Uses a weighted scoring model combining:
      - Margin quality (profit vs buy price)
      - Risk level (shock impact ratio)
      - Market trend alignment (buying in a falling market = good)
      - Demand level (higher demand = faster sale)
      - Seasonal timing
      - VRM confidence (verified plates = lower risk)
      - Vehicle reliability profile

    Returns probability 0.0–1.0.
    """
    score = 0.5  # base probability

    # 1. Margin quality (0 to +0.20)
    if deal.buy_price > 0:
        margin_pct = out.expected_profit / deal.buy_price
        if margin_pct >= 0.20:
            score += 0.20
        elif margin_pct >= 0.10:
            score += 0.12
        elif margin_pct > 0:
            score += 0.05
        else:
            score -= 0.15

    # 2. Risk level (-0.15 to +0.10)
    if out.shock_impact_ratio <= 0.10:
        score += 0.10
    elif out.shock_impact_ratio <= 0.20:
        score += 0.05
    elif out.shock_impact_ratio >= 0.35:
        score -= 0.15
    elif out.shock_impact_ratio >= 0.25:
        score -= 0.08

    # 3. Market trend (-0.10 to +0.10)
    if trend:
        if trend["direction"] == "falling":
            # Prices falling = good time to buy (cheap), but harder to sell
            score += 0.02
        elif trend["direction"] == "rising":
            # Prices rising = expensive to buy, but easier to sell higher
            score += 0.08
        # Stable = neutral

    # 4. Demand level (-0.05 to +0.10)
    if demand:
        if demand["level"] == "high":
            score += 0.10
        elif demand["level"] == "low":
            score -= 0.05

    # 5. Seasonal timing (-0.05 to +0.05)
    if demand and demand.get("seasonal_factor"):
        sf = demand["seasonal_factor"]
        if sf >= 1.05:
            score += 0.05
        elif sf <= 0.85:
            score -= 0.05

    # 6. VRM confidence (0 to +0.05)
    if listing.vrm and listing.vrm_confidence >= 0.90:
        score += 0.05
    elif listing.vrm and listing.vrm_confidence >= 0.80:
        score += 0.02

    # 7. Decision alignment (+0.05)
    if out.decision == "BUY":
        score += 0.05

    # Clamp to [0.05, 0.95]
    return max(0.05, min(0.95, score))


# ---------------------------------------------------------------------------
# Batch analytics for pipeline
# ---------------------------------------------------------------------------

def compute_analytics_for_rows(
    conn: sqlite3.Connection,
    rows: List[Tuple[Listing, DealInput, DealOutput]],
) -> None:
    """
    Compute and attach analytics data to scored rows.
    Modifies DealOutput.notes in place.
    """
    for listing, deal, out in rows:
        try:
            key = vehicle_key_from_title(listing.title)
            trend = compute_price_trends(conn, key)
            demand = compute_demand_signals(conn, key)
            success = predict_deal_success(listing, deal, out, trend, demand)

            trend_dir = trend["direction"] if trend else "?"
            demand_lvl = demand["level"] if demand else "?"
            out.notes += (f" | trend={trend_dir}"
                          f" demand={demand_lvl}"
                          f" success={success:.0%}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# P&L tracking helpers
# ---------------------------------------------------------------------------

def compute_pnl_summary(
    conn: sqlite3.Connection,
) -> Dict[str, Any]:
    """
    Compute profit/loss summary from completed deals (leads with status='sold').

    Returns dict with:
      total_deals, total_invested, total_revenue, total_profit,
      avg_profit, avg_roi_pct, avg_days_to_sell,
      best_make_model, worst_make_model
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT title, actual_buy_price, actual_sale_price, actual_repairs,"
        "       actual_days_to_sell, realised_profit"
        " FROM leads WHERE status = 'sold'"
        "   AND actual_buy_price IS NOT NULL"
        "   AND actual_sale_price IS NOT NULL"
    )

    deals = cur.fetchall()
    if not deals:
        return {
            "total_deals": 0, "total_invested": 0, "total_revenue": 0,
            "total_profit": 0, "avg_profit": 0, "avg_roi_pct": 0,
            "avg_days_to_sell": 0,
        }

    total_invested = sum(r[1] for r in deals)
    total_revenue = sum(r[2] for r in deals)
    total_repairs = sum(r[3] or 0 for r in deals)
    total_profit = sum(r[5] or (r[2] - r[1] - (r[3] or 0)) for r in deals)
    days_list = [r[4] for r in deals if r[4] is not None]

    # Best/worst by make|model
    by_key: Dict[str, List[float]] = {}
    for title, buy, sale, rep, days, profit in deals:
        g = guess_make_model(title)
        k = f"{g.make}|{g.model}"
        real_profit = profit or (sale - buy - (rep or 0))
        by_key.setdefault(k, []).append(real_profit)

    best_key = max(by_key, key=lambda k: sum(by_key[k]) / len(by_key[k])) if by_key else ""
    worst_key = min(by_key, key=lambda k: sum(by_key[k]) / len(by_key[k])) if by_key else ""

    return {
        "total_deals": len(deals),
        "total_invested": round(total_invested, 2),
        "total_revenue": round(total_revenue, 2),
        "total_repairs": round(total_repairs, 2),
        "total_profit": round(total_profit, 2),
        "avg_profit": round(total_profit / len(deals), 2) if deals else 0,
        "avg_roi_pct": round(
            total_profit / total_invested * 100, 1) if total_invested else 0,
        "avg_days_to_sell": (
            round(sum(days_list) / len(days_list), 1) if days_list else 0),
        "best_make_model": best_key.replace("|", " ").title(),
        "worst_make_model": worst_key.replace("|", " ").title(),
    }

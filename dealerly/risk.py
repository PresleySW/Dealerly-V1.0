"""
dealerly/risk.py
================
Core financial risk and margin model.

v0.9.3 changes:
  - allowed_shock_threshold() now accepts optional make/model to scale
    by reliability tier from config.MODEL_RELIABILITY_TIERS.
  - New model_shock_adjustment() helper.

Responsibilities:
  - risk_buffer: capital protection reserve
  - allowed_shock_threshold: max tolerable worst-case repair ratio
  - model_shock_adjustment: per-model reliability offset (v0.9.3)
  - classify_decision: BUY / OFFER / PASS / AVOID from profit + shock
  - evaluate_deal: full DealInput -> DealOutput calculation
  - apply_vrm_buy_gate: VRM-based decision override (currently a pass-through)
  - estimate_fees: eBay + payment fee calculation
  - mileage_correction: resale price adjustment for odometer reading

Depends on:
  - dealerly.models  (DealInput, DealOutput)
  - dealerly.utils   (clamp)
  - dealerly.config  (MODEL_RELIABILITY_TIERS)

No I/O. No DB. Pure logic only.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

from dealerly.config import MODEL_RELIABILITY_TIERS
from dealerly.models import DealInput, DealOutput
from dealerly.utils import clamp


# ---------------------------------------------------------------------------
# Capital risk parameters
# ---------------------------------------------------------------------------

def risk_buffer(capital: float) -> float:
    """
    Emergency reserve held back from max_bid calculation.
    5% of capital -- scales with exposure.
    """
    return 0.05 * max(capital, 0.0)


# ---------------------------------------------------------------------------
# Model reliability adjustment (v0.9.3)
# ---------------------------------------------------------------------------

# Tier -> shock threshold offset
_TIER_OFFSETS: dict[int, float] = {
    1:  0.06,    # very reliable: loosen by +6pp
    2:  0.03,    # reliable: loosen by +3pp
    3:  0.00,    # average: no change
    4: -0.03,    # risky: tighten by -3pp
}


def model_shock_adjustment(make: Optional[str], model: Optional[str]) -> float:
    """
    Return the shock threshold offset for a given make/model.
    Positive = more lenient, negative = stricter.
    Unknown models default to tier 3 (no change).
    """
    if not make or not model:
        return 0.0
    key = (make.strip().lower(), model.strip().lower())
    tier = MODEL_RELIABILITY_TIERS.get(key, 3)
    return _TIER_OFFSETS.get(tier, 0.0)


def allowed_shock_threshold(
    capital: float,
    make: Optional[str] = None,
    model: Optional[str] = None,
) -> float:
    """
    Maximum worst-case repair cost as a fraction of capital before AVOID fires.
    Tighter at higher capital because proportionally more is at risk.

    v0.9.3: Adjusted by model reliability tier. A Honda Jazz at GBP3k capital
    gets 0.30 + 0.06 = 0.36, while a VW Golf gets 0.30 - 0.03 = 0.27.
    Result is clamped to [0.12, 0.45] to prevent extremes.
    """
    if capital < 5_000:
        base = 0.50  # £3k→£1500, £4k→£2000 AVOID floor
    elif capital < 8_000:
        base = 0.35  # £5k→£1750, £7k→£2450 AVOID floor
                     # The old 0.20 at £5k gave a £1000 floor — tighter than the
                     # £1500 floor at £3k (same 0.50 rate applied to smaller capital).
                     # A clean car with one MOT advisory easily exceeds £1000 worst-
                     # case, causing 0 BUY in dealer mode despite solid comps. 0.35
                     # keeps genuine basket-cases out (worst > £1750) while allowing
                     # cars with a single advisory or a timing-chain profile through.
    elif capital < 15_000:
        base = 0.25  # £8k→£2000, £14k→£3500 AVOID floor
    else:
        base = 0.15
    adjustment = model_shock_adjustment(make, model)
    return clamp(base + adjustment, 0.12, 0.60)


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def classify_decision(
    profit: float,
    target: float,
    shock: float,
    threshold: float,
) -> Tuple[str, str]:
    """
    Map profit and shock ratio to a BUY / OFFER / PASS / AVOID decision.

    Args:
        profit:    expected net profit at listed price
        target:    minimum profit required for BUY
        shock:     worst_case_repair / capital
        threshold: capital-dependent max allowed shock ratio

    Returns:
        (decision, reason)
    """
    if shock > threshold:
        return "AVOID", "Shock impact too high for current capital."
    if profit >= target:
        return "BUY",   "Meets target margin."
    if profit > 0:
        return "OFFER", "Profitable only if you negotiate down to max bid."
    return "PASS", "Not profitable on current assumptions."


def apply_vrm_buy_gate(
    decision: str,
    reason: str,
    *,
    mot_enabled: bool,
    vrm: str,
    vrm_source: str,
    vrm_confidence: float,
) -> Tuple[str, str]:
    """
    Post-evaluation VRM confidence gate.

    v0.8.2+: No longer downgrades BUY -> OFFER for missing VRM.
    Without a verified VRM the MOT is simply unverified -- not a financial
    disqualifier at the GBP800-GBP2500 price point. The report shows a warning
    badge; the buyer decides on inspection.

    Preserved as a hook for future policy changes.
    """
    return decision, reason


# ---------------------------------------------------------------------------
# Deal evaluation
# ---------------------------------------------------------------------------

def evaluate_deal(d: DealInput, make: str = "", model: str = "") -> DealOutput:
    """
    Evaluate a fully-resolved DealInput and return a DealOutput.

    v0.9.3: accepts optional make/model for model-aware shock threshold.

    Profit formula:
        profit = resale - buy_price - repairs - holding - fees - admin - transport

    Max bid = the highest price at which the deal still meets target_margin
    after all costs and the risk buffer.

    Velocity = profit per day (proxy for capital efficiency).
    """
    rb = risk_buffer(d.capital_available)
    shock_ratio = (
        d.worst_case_repair / d.capital_available
        if d.capital_available > 0
        else math.inf
    )
    shock_threshold = allowed_shock_threshold(d.capital_available, make, model)

    profit = (
        d.expected_resale
        - d.buy_price
        - d.base_repair_estimate
        - d.holding_cost
        - d.fees_total
        - d.admin_buffer
        - d.transport_buffer
    )
    max_bid = (
        d.expected_resale
        - d.base_repair_estimate
        - d.holding_cost
        - d.fees_total
        - d.admin_buffer
        - d.transport_buffer
        - d.target_margin
        - rb
    )
    velocity = profit / max(d.expected_days_to_sell, 1)
    decision, reason = classify_decision(profit, d.target_margin, shock_ratio, shock_threshold)

    return DealOutput(
        expected_profit=profit,
        risk_buffer=rb,
        shock_impact_ratio=shock_ratio,
        max_bid=max_bid,
        velocity_score=velocity,
        decision=decision,
        reason=reason,
        p_mot=1.0,   # placeholder -- overwritten by scoring.py after MOT lookup
        notes="",    # placeholder -- overwritten by scoring.py
    )


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------

def estimate_fees(resale: float, ebay_fee: float, pay_fee: float) -> float:
    """
    Combined eBay listing fee + payment processing fee on expected resale price.
    Both rates are fractions (e.g. 0.06 for 6%).
    """
    return (ebay_fee + pay_fee) * max(resale, 0.0)


# ---------------------------------------------------------------------------
# Mileage correction
# ---------------------------------------------------------------------------

def mileage_correction(mileage: Optional[int], baseline: int = 80_000) -> float:
    """
    Adjust the comps median price for a listing's odometer reading.

    Baseline 80k miles -> factor 1.0.
    Each 10k miles above/below baseline shifts factor by +/-1.5%.
    Hard adjustments at extremes (>140k, >180k, <40k).
    Clamped to 0.60-1.20.
    """
    if not mileage or mileage <= 0:
        return 1.0
    diff_10k = (mileage - baseline) / 10_000
    factor = 1.0 - 0.015 * diff_10k
    if mileage > 140_000:
        factor -= 0.08
    if mileage > 180_000:
        factor -= 0.08
    if mileage < 40_000:
        factor += 0.06
    return clamp(factor, 0.60, 1.20)

"""
dealerly/calibration.py
=======================
Historical deal-log analysis and scoring threshold calibration.

Reads dealerly_log.csv (append-only log of every scored listing) and
computes statistics that reveal whether current thresholds are too tight,
too loose, or well-tuned.  Outputs a CalibrationResult that other modules
can optionally consume to override static defaults.

Responsibilities:
  - Parse and validate the deal log
  - Per-decision-bucket statistics (profit, shock, velocity distributions)
  - Threshold recommendations (shock, margin, near-miss band)
  - Resale accuracy analysis (systematic over/under-estimation)
  - VRM enrichment success metrics
  - Human-readable calibration summary for reports / CLI

Depends on:
  - dealerly.config (DEAL_LOG_PATH, thresholds, default_target_margin)
  - dealerly.ebay   (guess_make_model — for make/model extraction)

No external API calls. No DB writes. Pure computation on local CSV data.

Integration:
  - scoring.py: pass CalibrationResult to override static thresholds
  - report.py:  embed format_calibration_html() card in flip reports
  - cli.py:     add --calibrate flag → print format_calibration_summary()
  - pipeline.py: optionally run calibrate() before scoring phase
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dealerly.config import (
    DEAL_LOG_PATH,
    DEFAULT_NEAR_MISS_BAND,
    DEFAULT_RESALE_DISCOUNT,
    MODEL_RELIABILITY_TIERS,
    default_target_margin,
)
from dealerly.ebay import guess_make_model


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class BucketStats:
    """Aggregate statistics for one decision bucket (BUY/OFFER/PASS/AVOID)."""
    count: int = 0
    avg_profit: float = 0.0
    median_profit: float = 0.0
    avg_shock: float = 0.0
    p75_shock: float = 0.0
    p95_shock: float = 0.0
    avg_velocity: float = 0.0
    median_velocity: float = 0.0
    avg_buy_price: float = 0.0
    avg_resale: float = 0.0
    vrm_rate: float = 0.0          # fraction with VRM found


@dataclass
class ThresholdRecommendation:
    """A single threshold adjustment recommendation."""
    parameter: str                  # e.g. "shock_threshold_base"
    current: float
    recommended: float
    direction: str                  # "tighten" | "loosen" | "keep"
    confidence: str                 # "high" | "medium" | "low"
    reason: str


@dataclass
class CalibrationResult:
    """Full calibration output — consumed by scoring, report, CLI."""
    total_rows: int = 0
    buckets: Dict[str, BucketStats] = field(default_factory=dict)
    recommendations: List[ThresholdRecommendation] = field(default_factory=list)

    # Resale accuracy
    resale_bias: float = 0.0       # +ve = overestimates, -ve = underestimates
    resale_bias_pct: float = 0.0   # as % of expected_resale

    # Enrichment health
    vrm_hit_rate: float = 0.0
    avg_vrm_confidence: float = 0.0

    # Decision distribution
    buy_rate: float = 0.0
    offer_rate: float = 0.0
    avoid_rate: float = 0.0

    # Suggested overrides (None = keep current)
    suggested_shock_base: Optional[float] = None
    suggested_margin_pct: Optional[float] = None
    suggested_near_miss: Optional[float] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _percentile(values: List[float], p: float) -> float:
    """Return the p-th percentile (0-100) of a sorted list."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _median(values: List[float]) -> float:
    return _percentile(values, 50.0)


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

@dataclass
class _LogRow:
    """Parsed and typed row from the deal log CSV."""
    timestamp: str
    platform: str
    item_id: str
    title: str
    vrm: str
    vrm_source: str
    vrm_confidence: float
    buy_price: float
    expected_resale: float
    base_repair: float
    worst_repair: float
    fees_total: float
    expected_profit: float
    max_bid: float
    shock_ratio: float
    velocity: float
    decision: str
    ulez: str
    url: str
    make: str = ""
    model: str = ""


def _parse_log(path: Path) -> List[_LogRow]:
    """Load and type-cast the deal log CSV. Skips malformed rows."""
    rows: List[_LogRow] = []
    if not path.exists():
        return rows

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            try:
                guess = guess_make_model(raw.get("title", ""))
                make = guess.make if hasattr(guess, "make") else (guess[0] if isinstance(guess, tuple) else "")
                model = guess.model if hasattr(guess, "model") else (guess[1] if isinstance(guess, tuple) else "")
                rows.append(_LogRow(
                    timestamp=raw.get("timestamp", ""),
                    platform=raw.get("platform", ""),
                    item_id=raw.get("item_id", ""),
                    title=raw.get("title", ""),
                    vrm=raw.get("vrm", "").strip(),
                    vrm_source=raw.get("vrm_source", ""),
                    vrm_confidence=float(raw.get("vrm_confidence", 0)),
                    buy_price=float(raw.get("buy_price", 0)),
                    expected_resale=float(raw.get("expected_resale", 0)),
                    base_repair=float(raw.get("base_repair", 0)),
                    worst_repair=float(raw.get("worst_repair", 0)),
                    fees_total=float(raw.get("fees_total", 0)),
                    expected_profit=float(raw.get("expected_profit", 0)),
                    max_bid=float(raw.get("max_bid", 0)),
                    shock_ratio=float(raw.get("shock_ratio", 0)),
                    velocity=float(raw.get("velocity", 0)),
                    decision=raw.get("decision", "").strip().upper(),
                    ulez=raw.get("ulez", ""),
                    url=raw.get("url", ""),
                    make=make,
                    model=model,
                ))
            except (ValueError, TypeError):
                continue  # skip unparseable rows
    return rows


# ---------------------------------------------------------------------------
# Bucket statistics
# ---------------------------------------------------------------------------

def _compute_bucket(rows: List[_LogRow]) -> BucketStats:
    """Compute aggregate stats for a list of log rows."""
    if not rows:
        return BucketStats()

    profits = [r.expected_profit for r in rows]
    shocks = [r.shock_ratio for r in rows]
    velocities = [r.velocity for r in rows]
    vrm_found = sum(1 for r in rows if r.vrm)

    return BucketStats(
        count=len(rows),
        avg_profit=_mean(profits),
        median_profit=_median(profits),
        avg_shock=_mean(shocks),
        p75_shock=_percentile(shocks, 75),
        p95_shock=_percentile(shocks, 95),
        avg_velocity=_mean(velocities),
        median_velocity=_median(velocities),
        avg_buy_price=_mean([r.buy_price for r in rows]),
        avg_resale=_mean([r.expected_resale for r in rows]),
        vrm_rate=vrm_found / len(rows),
    )


# ---------------------------------------------------------------------------
# Threshold analysis
# ---------------------------------------------------------------------------

_DECISIONS = ("BUY", "OFFER", "PASS", "AVOID")
_MIN_ROWS_FOR_CALIBRATION = 30


def _analyse_shock(
    buckets: Dict[str, BucketStats],
    rows: List[_LogRow],
    capital: float,
) -> List[ThresholdRecommendation]:
    """
    Check whether the shock threshold is appropriately calibrated.

    Heuristics:
      - If >15% of BUYs have shock above base threshold → tighten
      - If AVOID bucket is >10% of total and BUY P95 well below → loosen
      - Otherwise → keep
    """
    recs: List[ThresholdRecommendation] = []
    buy_rows = [r for r in rows if r.decision == "BUY"]
    total = len(rows)

    if len(buy_rows) < 10:
        return recs

    from dealerly.risk import allowed_shock_threshold
    current_base = allowed_shock_threshold(capital)
    buy_shocks = [r.shock_ratio for r in buy_rows]
    pct_above = sum(1 for s in buy_shocks if s > current_base) / len(buy_shocks)
    p95 = _percentile(buy_shocks, 95)

    avoid_rate = buckets.get("AVOID", BucketStats()).count / total if total else 0

    if pct_above > 0.15:
        suggested = round(current_base - 0.02, 3)
        recs.append(ThresholdRecommendation(
            parameter="shock_threshold_base",
            current=current_base,
            recommended=suggested,
            direction="tighten",
            confidence="high" if pct_above > 0.25 else "medium",
            reason=(
                f"{pct_above:.0%} of BUY decisions have shock_ratio above "
                f"{current_base:.2f}. P95 shock in BUYs is {p95:.3f}. "
                f"Tightening by 2pp would filter highest-risk deals."
            ),
        ))
    elif avoid_rate > 0.10 and p95 < current_base * 0.80:
        suggested = round(current_base + 0.02, 3)
        recs.append(ThresholdRecommendation(
            parameter="shock_threshold_base",
            current=current_base,
            recommended=suggested,
            direction="loosen",
            confidence="medium",
            reason=(
                f"AVOID rate is {avoid_rate:.0%} but BUY P95 shock is only "
                f"{p95:.3f} (well below {current_base:.2f}). Loosening by "
                f"2pp could recover missed opportunities."
            ),
        ))
    else:
        recs.append(ThresholdRecommendation(
            parameter="shock_threshold_base",
            current=current_base,
            recommended=current_base,
            direction="keep",
            confidence="high" if len(buy_rows) > 30 else "medium",
            reason=(
                f"Only {pct_above:.0%} of BUYs exceed threshold; "
                f"P95 shock {p95:.3f} is within tolerance. No change needed."
            ),
        ))

    return recs


def _analyse_margin(
    buckets: Dict[str, BucketStats],
    rows: List[_LogRow],
    capital: float,
) -> List[ThresholdRecommendation]:
    """
    Check whether the margin target is well-calibrated.

    Heuristics:
      - BUY rate <25% with many near-miss OFFERs → margin too high
      - BUY rate >50% → margin too low (not selective enough)
      - Otherwise → healthy balance
    """
    recs: List[ThresholdRecommendation] = []
    total = len(rows)
    if total < _MIN_ROWS_FOR_CALIBRATION:
        return recs

    current_target = default_target_margin(capital)
    buy_rate = buckets.get("BUY", BucketStats()).count / total
    offer_bucket = buckets.get("OFFER", BucketStats())

    if buy_rate < 0.25 and offer_bucket.count > 20:
        offer_rows = [r for r in rows if r.decision == "OFFER"]
        near_misses = sum(
            1 for r in offer_rows
            if r.expected_profit > 0 and r.expected_profit >= current_target * 0.70
        )
        near_miss_pct = near_misses / len(offer_rows) if offer_rows else 0

        if near_miss_pct > 0.30:
            suggested = round(current_target * 0.90, -1)
            recs.append(ThresholdRecommendation(
                parameter="target_margin",
                current=current_target,
                recommended=suggested,
                direction="loosen",
                confidence="medium",
                reason=(
                    f"BUY rate is only {buy_rate:.0%}. {near_miss_pct:.0%} of "
                    f"OFFERs are within 30% of the £{current_target:.0f} target. "
                    f"Reducing to £{suggested:.0f} would capture more deals."
                ),
            ))
        else:
            recs.append(ThresholdRecommendation(
                parameter="target_margin",
                current=current_target,
                recommended=current_target,
                direction="keep",
                confidence="medium",
                reason=(
                    f"BUY rate is {buy_rate:.0%} — low but OFFERs aren't "
                    f"clustered near the threshold. Market may just be tight."
                ),
            ))
    elif buy_rate > 0.50:
        suggested = round(current_target * 1.10, -1)
        recs.append(ThresholdRecommendation(
            parameter="target_margin",
            current=current_target,
            recommended=suggested,
            direction="tighten",
            confidence="medium",
            reason=(
                f"BUY rate is {buy_rate:.0%} — over half of listings score "
                f"as BUY. Raising target to £{suggested:.0f} would increase "
                f"selectivity and average deal quality."
            ),
        ))
    else:
        recs.append(ThresholdRecommendation(
            parameter="target_margin",
            current=current_target,
            recommended=current_target,
            direction="keep",
            confidence="high",
            reason=(
                f"BUY rate is {buy_rate:.0%} — healthy balance between "
                f"selectivity and opportunity capture."
            ),
        ))

    return recs


def _analyse_near_miss(
    rows: List[_LogRow],
    capital: float,
) -> List[ThresholdRecommendation]:
    """
    Check whether the near-miss band (OFFER negotiation range) is well-sized.
    """
    recs: List[ThresholdRecommendation] = []
    offer_rows = [r for r in rows if r.decision == "OFFER"]
    if len(offer_rows) < 10:
        return recs

    gaps = [r.buy_price - r.max_bid for r in offer_rows if r.max_bid > 0]
    if not gaps:
        return recs

    median_gap = _median(gaps)
    current = DEFAULT_NEAR_MISS_BAND

    if median_gap > current * 1.5:
        suggested = round(min(median_gap * 0.8, 500), -1)
        recs.append(ThresholdRecommendation(
            parameter="near_miss_band",
            current=current,
            recommended=suggested,
            direction="loosen",
            confidence="low",
            reason=(
                f"Median OFFER negotiation gap is £{median_gap:.0f} vs "
                f"current band £{current:.0f}. Widening to £{suggested:.0f} "
                f"would better reflect actual negotiation room."
            ),
        ))
    elif median_gap < current * 0.5:
        suggested = round(max(median_gap * 1.2, 100), -1)
        recs.append(ThresholdRecommendation(
            parameter="near_miss_band",
            current=current,
            recommended=suggested,
            direction="tighten",
            confidence="low",
            reason=(
                f"Median OFFER gap is only £{median_gap:.0f} — most OFFERs "
                f"are very close to listing price. Band of £{suggested:.0f} "
                f"is more realistic."
            ),
        ))
    else:
        recs.append(ThresholdRecommendation(
            parameter="near_miss_band",
            current=current,
            recommended=current,
            direction="keep",
            confidence="medium",
            reason=(
                f"Median OFFER gap (£{median_gap:.0f}) aligns with current "
                f"band (£{current:.0f}). No change needed."
            ),
        ))

    return recs


def _analyse_resale_accuracy(rows: List[_LogRow]) -> Tuple[float, float]:
    """
    Detect systematic bias in resale estimates.

    Uses profit/resale ratio across BUY+OFFER rows as a proxy.
    Without actual sold prices, checks for suspicious skew.

    Returns: (bias_£, bias_%)
    """
    actionable = [
        r for r in rows
        if r.decision in ("BUY", "OFFER") and r.expected_resale > 0
    ]
    if len(actionable) < 20:
        return 0.0, 0.0

    margin_pcts = [r.expected_profit / r.expected_resale for r in actionable]
    avg_margin = _mean(margin_pcts)

    # Healthy range: 8-20% margin on sub-£3k cars.
    if avg_margin > 0.22:
        bias_pct = avg_margin - 0.15
        bias_gbp = bias_pct * _mean([r.expected_resale for r in actionable])
        return bias_gbp, bias_pct
    elif avg_margin < 0.05:
        bias_pct = avg_margin - 0.15
        bias_gbp = bias_pct * _mean([r.expected_resale for r in actionable])
        return bias_gbp, bias_pct

    return 0.0, 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calibrate(
    capital: float = 3000.0,
    log_path: Optional[Path] = None,
) -> CalibrationResult:
    """
    Run full calibration analysis on the deal log.

    Args:
        capital:  current working capital (for threshold context)
        log_path: override deal log path (default: config.DEAL_LOG_PATH)

    Returns:
        CalibrationResult with statistics, recommendations, and
        optional override values for scoring.
    """
    path = log_path or DEAL_LOG_PATH
    rows = _parse_log(path)
    result = CalibrationResult(total_rows=len(rows))

    if len(rows) < _MIN_ROWS_FOR_CALIBRATION:
        result.recommendations.append(ThresholdRecommendation(
            parameter="general",
            current=0, recommended=0,
            direction="keep", confidence="low",
            reason=(
                f"Only {len(rows)} rows in deal log — need at least "
                f"{_MIN_ROWS_FOR_CALIBRATION} for meaningful calibration."
            ),
        ))
        return result

    # -- Bucket stats --
    by_decision: Dict[str, List[_LogRow]] = {}
    for d in _DECISIONS:
        by_decision[d] = [r for r in rows if r.decision == d]
        result.buckets[d] = _compute_bucket(by_decision[d])

    # -- Decision distribution --
    total = len(rows)
    result.buy_rate = len(by_decision["BUY"]) / total
    result.offer_rate = len(by_decision["OFFER"]) / total
    result.avoid_rate = len(by_decision["AVOID"]) / total

    # -- Enrichment health --
    vrm_rows = [r for r in rows if r.vrm]
    result.vrm_hit_rate = len(vrm_rows) / total
    conf_values = [r.vrm_confidence for r in vrm_rows if r.vrm_confidence > 0]
    result.avg_vrm_confidence = _mean(conf_values)

    # -- Resale accuracy --
    result.resale_bias, result.resale_bias_pct = _analyse_resale_accuracy(rows)

    # -- Threshold recommendations --
    result.recommendations.extend(_analyse_shock(result.buckets, rows, capital))
    result.recommendations.extend(_analyse_margin(result.buckets, rows, capital))
    result.recommendations.extend(_analyse_near_miss(rows, capital))

    # -- Resale bias recommendation --
    if abs(result.resale_bias_pct) > 0.03:
        direction = "overestimating" if result.resale_bias_pct > 0 else "underestimating"
        result.recommendations.append(ThresholdRecommendation(
            parameter="resale_discount",
            current=DEFAULT_RESALE_DISCOUNT,
            recommended=round(DEFAULT_RESALE_DISCOUNT - result.resale_bias_pct, 3),
            direction="tighten" if result.resale_bias_pct > 0 else "loosen",
            confidence="low",
            reason=(
                f"Resale estimates appear to be {direction} by "
                f"~{abs(result.resale_bias_pct):.1%} (£{abs(result.resale_bias):.0f}). "
                f"Consider adjusting resale_discount accordingly. "
                f"Note: without actual sold prices this is approximate."
            ),
        ))

    # -- Set suggested overrides --
    for rec in result.recommendations:
        if rec.direction == "keep":
            continue
        if rec.parameter == "shock_threshold_base":
            result.suggested_shock_base = rec.recommended
        elif rec.parameter == "target_margin":
            result.suggested_margin_pct = rec.recommended
        elif rec.parameter == "near_miss_band":
            result.suggested_near_miss = rec.recommended

    return result


# ---------------------------------------------------------------------------
# Human-readable summaries
# ---------------------------------------------------------------------------

def format_calibration_summary(cal: CalibrationResult, capital: float = 3000.0) -> str:
    """Render a plain-text calibration summary for CLI / log output."""
    lines: List[str] = []
    lines.append(f"=== Dealerly Calibration Report ({cal.total_rows} rows) ===\n")

    lines.append("Decision distribution:")
    for d in _DECISIONS:
        b = cal.buckets.get(d, BucketStats())
        pct = b.count / cal.total_rows * 100 if cal.total_rows else 0
        lines.append(
            f"  {d:6s}  {b.count:3d} ({pct:4.1f}%)  "
            f"avg profit £{b.avg_profit:6.0f}  "
            f"avg shock {b.avg_shock:.3f}  "
            f"avg velocity {b.avg_velocity:5.1f}"
        )

    lines.append(
        f"\nVRM enrichment: {cal.vrm_hit_rate:.0%} hit rate, "
        f"avg confidence {cal.avg_vrm_confidence:.0%}"
    )

    if abs(cal.resale_bias_pct) > 0.01:
        direction = "over" if cal.resale_bias_pct > 0 else "under"
        lines.append(
            f"Resale bias: {direction}-estimating by ~{abs(cal.resale_bias_pct):.1%} "
            f"(£{abs(cal.resale_bias):.0f})"
        )
    else:
        lines.append("Resale bias: within tolerance")

    lines.append("\nRecommendations:")
    for rec in cal.recommendations:
        icon = {"tighten": "-", "loosen": "+", "keep": "="}.get(rec.direction, "?")
        lines.append(
            f"  {icon} [{rec.confidence:6s}] {rec.parameter}: {rec.reason}"
        )

    return "\n".join(lines)


def format_calibration_html(cal: CalibrationResult) -> str:
    """Render a compact HTML card for embedding in flip reports."""
    rows_html = ""
    for d in _DECISIONS:
        b = cal.buckets.get(d, BucketStats())
        pct = b.count / cal.total_rows * 100 if cal.total_rows else 0
        rows_html += (
            f"<tr>"
            f"<td style='font-weight:700'>{d}</td>"
            f"<td>{b.count}</td>"
            f"<td>{pct:.1f}%</td>"
            f"<td>£{b.avg_profit:.0f}</td>"
            f"<td>{b.avg_shock:.3f}</td>"
            f"<td>{b.p95_shock:.3f}</td>"
            f"<td>{b.avg_velocity:.1f}</td>"
            f"</tr>"
        )

    recs_html = ""
    for rec in cal.recommendations:
        colour = {"tighten": "#ef4444", "loosen": "#22c55e", "keep": "#94a3b8"}
        dot = colour.get(rec.direction, "#94a3b8")
        recs_html += (
            f"<li style='margin-bottom:6px'>"
            f"<span style='color:{dot};font-weight:700'>"
            f"{rec.direction.upper()}</span> "
            f"<code>{rec.parameter}</code> "
            f"<small>[{rec.confidence}]</small><br>"
            f"<span style='color:#555'>{rec.reason}</span>"
            f"</li>"
        )

    return f"""
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                padding:16px;margin:16px 0;font-family:system-ui,sans-serif;
                font-size:0.9em">
      <h3 style="margin:0 0 12px">
        Calibration ({cal.total_rows} historical rows)
      </h3>
      <table style="width:100%;border-collapse:collapse;margin-bottom:12px">
        <thead>
          <tr style="border-bottom:2px solid #cbd5e1;text-align:left">
            <th>Decision</th><th>Count</th><th>%</th>
            <th>Avg Profit</th><th>Avg Shock</th>
            <th>P95 Shock</th><th>Avg Vel.</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <div style="display:flex;gap:24px;margin-bottom:12px">
        <div>VRM hit rate: <strong>{cal.vrm_hit_rate:.0%}</strong></div>
        <div>Avg VRM conf: <strong>{cal.avg_vrm_confidence:.0%}</strong></div>
        <div>Resale bias: <strong>{cal.resale_bias_pct:+.1%}</strong></div>
      </div>
      <h4 style="margin:8px 0 4px">Recommendations</h4>
      <ul style="padding-left:18px;margin:0">{recs_html}</ul>
    </div>
    """


# ---------------------------------------------------------------------------
# Sprint 2: real-outcome calibration from completed_trades
# ---------------------------------------------------------------------------

def calibrate_from_trades(conn: Any, capital: float = 3000.0) -> Dict[str, Any]:
    """
    Compute calibration insights from actual completed flip outcomes.

    Uses the completed_trades table (real buy/sell/repair data) rather than
    the predicted deal log.  Returns a plain dict — lightweight, no
    CalibrationResult dependency.

    Keys returned:
      rows_analysed     int    — number of completed trades found
      avg_realised      float  — mean realised profit across all trades
      avg_days_to_sell  float  — mean days to sell (trades where known)
      best_trade        float  — highest single realised profit
      worst_trade       float  — lowest single realised profit
      avg_abs_error     float  — mean |prediction − realised| (trades w/ pred)
      avg_error         float  — mean (realised − predicted) (+ = underestimated)
      beat_forecast_pct float  — % of predicted trades that beat the model
      resale_margin_pct float  — mean (sell_price / buy_price − 1) as a guide
                                 to whether the margin target is realistic
    """
    from dealerly.db import list_completed_trades

    trades = list_completed_trades(conn)
    if not trades:
        return {"rows_analysed": 0}

    profits = [float(t.get("realised_profit") or 0.0) for t in trades]
    sell_prices = [float(t.get("sell_price") or 0.0) for t in trades]
    buy_prices = [float(t.get("buy_price") or 0.0) for t in trades]
    days_list = [int(t["days_to_sell"]) for t in trades if t.get("days_to_sell")]

    margins = [
        (s / b) - 1.0
        for s, b in zip(sell_prices, buy_prices)
        if b > 0
    ]

    pred_rows = [
        t for t in trades
        if t.get("predicted_profit") is not None and t.get("prediction_error") is not None
    ]
    abs_errors = [abs(float(t["prediction_error"])) for t in pred_rows]
    errors = [float(t["prediction_error"]) for t in pred_rows]
    beat = sum(1 for e in errors if e >= 0)

    return {
        "rows_analysed": len(trades),
        "avg_realised": _mean(profits),
        "best_trade": max(profits) if profits else 0.0,
        "worst_trade": min(profits) if profits else 0.0,
        "avg_days_to_sell": _mean([float(d) for d in days_list]) if days_list else 0.0,
        "avg_abs_error": _mean(abs_errors) if abs_errors else 0.0,
        "avg_error": _mean(errors) if errors else 0.0,
        "beat_forecast_pct": beat / len(pred_rows) if pred_rows else 0.0,
        "resale_margin_pct": _mean(margins) if margins else 0.0,
    }


# ---------------------------------------------------------------------------
# Sprint 6: prediction vs outcome — cross-reference deal log with trades DB
# ---------------------------------------------------------------------------

def prediction_vs_outcome(conn: Any) -> List[Dict[str, Any]]:
    """
    Join the deal log CSV with completed_trades on VRM to produce a
    "predicted profit vs realised profit" comparison table.

    For each completed trade whose VRM appears in the deal log, returns a
    dict with:
      vrm             str   — normalised plate
      title           str   — listing title from the log (best match)
      predicted       float — expected_profit from the pipeline score
      realised        float — actual realised profit from the trade
      delta           float — realised - predicted (positive = exceeded forecast)
      platform        str   — eBay / facebook / motors
      decision        str   — pipeline decision (BUY / OFFER / PASS / AVOID)

    Matching logic: normalise VRM (strip spaces, uppercase) and match on
    any log row with the same plate. If multiple log rows exist for the
    same VRM (re-listed), picks the row with the highest expected_profit.

    Returns:
        List of match dicts sorted by |delta| descending.
        Empty list if no matches or deal log is missing.
    """
    from dealerly.db import list_completed_trades

    trades = list_completed_trades(conn)
    if not trades:
        return []

    log_rows = _parse_log(DEAL_LOG_PATH)
    if not log_rows:
        return []

    # Build VRM → best log row map (highest expected_profit wins for duplicates)
    log_by_vrm: Dict[str, _LogRow] = {}
    for row in log_rows:
        vrm = row.vrm.upper().replace(" ", "")
        if not vrm:
            continue
        if vrm not in log_by_vrm or row.expected_profit > log_by_vrm[vrm].expected_profit:
            log_by_vrm[vrm] = row

    results: List[Dict[str, Any]] = []
    for trade in trades:
        t_vrm = str(trade.get("vrm") or "").upper().replace(" ", "")
        if not t_vrm or t_vrm not in log_by_vrm:
            continue
        log_row = log_by_vrm[t_vrm]
        realised = float(trade.get("realised_profit") or 0.0)
        predicted = log_row.expected_profit
        results.append({
            "vrm": t_vrm,
            "title": log_row.title,
            "predicted": predicted,
            "realised": realised,
            "delta": realised - predicted,
            "platform": log_row.platform,
            "decision": log_row.decision,
        })

    results.sort(key=lambda r: -abs(r["delta"]))
    return results


def format_prediction_vs_outcome(matches: List[Dict[str, Any]]) -> str:
    """
    Render a plain-text prediction-vs-outcome table for CLI --trades output.

    Args:
        matches: output of prediction_vs_outcome()

    Returns:
        Human-readable string; empty string if no matches.
    """
    if not matches:
        return ""

    lines: List[str] = []
    lines.append(f"\n  --- Prediction vs Outcome ({len(matches)} matched) ---")
    lines.append(
        f"  {'VRM':<10s}  {'Decision':<7s}  "
        f"{'Predicted':>10s}  {'Realised':>10s}  {'Delta':>10s}  Title"
    )
    lines.append("  " + "-" * 78)

    deltas: List[float] = []
    for r in matches:
        sign = "+" if r["delta"] >= 0 else ""
        lines.append(
            f"  {r['vrm']:<10s}  {r['decision']:<7s}  "
            f"  £{r['predicted']:>7.0f}    £{r['realised']:>7.0f}  "
            f"  {sign}£{r['delta']:>6.0f}  {r['title'][:35]}"
        )
        deltas.append(r["delta"])

    if len(deltas) >= 2:
        mean_d = sum(deltas) / len(deltas)
        beat = sum(1 for d in deltas if d >= 0)
        sign = "+" if mean_d >= 0 else ""
        direction = "underestimated" if mean_d > 0 else "overestimated"
        lines.append("  " + "-" * 78)
        lines.append(
            f"  Avg delta {sign}£{mean_d:.0f} - model {direction} by that amount.  "
            f"Beat forecast: {beat}/{len(deltas)}"
        )
    return "\n".join(lines)

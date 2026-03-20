"""
dealerly/models.py
==================
Pure data models for the Dealerly pipeline.

v0.9.0 additions:
  - Lead: CRM deal pipeline lead tracking
  - PriceObservation: historical price data point for analytics
  - DealAnalytics: predictive analytics results per deal
  - PostingDraft: multi-platform listing draft
  - DealerInventoryItem: B2B dealer network listing
  - ContactRecord: seller/dealer contact info

No logic. No I/O. No imports from other Dealerly modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Core pipeline models (existing)
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    """
    A single car listing ingested from eBay, Facebook CSV/paste, or
    dealer network.

    Populated incrementally across pipeline phases:
      Phase 1: core fields
      Phase 3: vrm_*, ulez
      Phase 4: mot_history
      Phase 5: offer_message
      Phase 6: analytics (attached to DealOutput notes)
    """
    platform: str               # "ebay" | "facebook" | "dealer_network"
    item_id: str
    title: str
    price_gbp: float
    url: str
    location: str
    condition: str
    vrm: str
    raw: Dict[str, Any]

    # VRM provenance
    vrm_source: str = ""
    vrm_confidence: float = 0.0
    vrm_evidence: str = ""

    # Vehicle attributes
    ulez_compliant: Optional[bool] = None
    year: Optional[int] = None
    mileage: Optional[int] = None
    fuel_type: str = ""

    # MOT data
    mot_history: Optional[Dict[str, Any]] = None

    # AI offer draft
    offer_message: str = ""

    # CSV overrides
    csv_expected_resale: Optional[float] = None
    csv_expected_days: Optional[int] = None

    # Image (Sprint 1)
    first_image_url: str = ""

    # Write-off category (v0.9.10)
    writeoff_category: str = ""         # "Cat S" | "Cat N" | "" (none detected)

    # DVLA enrichment (v0.9.0)
    dvla_data: Optional[Dict[str, Any]] = None
    colour: str = ""
    engine_cc: Optional[int] = None
    tax_status: str = ""
    tax_due_date: str = ""
    first_registered: str = ""


@dataclass
class VehicleGuess:
    """Best-effort make/model/year/mileage from a listing title."""
    make: str
    model: str
    year: Optional[int]
    mileage: Optional[int]
    fuel_type: str = ""


@dataclass
class DealInput:
    """Fully-resolved inputs for a single deal evaluation."""
    reg: str
    capital_available: float
    buy_price: float
    expected_resale: float
    base_repair_estimate: float
    worst_case_repair: float
    expected_days_to_sell: int
    holding_cost: float
    target_margin: float
    fees_total: float
    admin_buffer: float
    transport_buffer: float
    repair_profile_notes: str = ""


@dataclass
class DealOutput:
    """Results of evaluating a DealInput."""
    expected_profit: float
    risk_buffer: float
    shock_impact_ratio: float
    max_bid: float
    velocity_score: float
    decision: str           # "BUY" | "OFFER" | "PASS" | "AVOID"
    reason: str
    p_mot: float
    notes: str


# ---------------------------------------------------------------------------
# CRM / Workflow models (v0.9.0)
# ---------------------------------------------------------------------------

@dataclass
class Lead:
    """
    A CRM lead in the deal pipeline.

    Status flow:
      sourced → contacted → inspecting → bidding → bought →
        preparing → listed → sold → closed
      Any stage can also transition to: lost | withdrawn
    """
    id: Optional[int] = None
    item_id: str = ""
    platform: str = ""
    title: str = ""
    vrm: str = ""
    url: str = ""

    # Financial
    buy_price: float = 0.0
    max_bid: float = 0.0
    expected_profit: float = 0.0
    actual_buy_price: Optional[float] = None
    actual_sale_price: Optional[float] = None
    actual_repairs: Optional[float] = None
    actual_days_to_sell: Optional[int] = None
    realised_profit: Optional[float] = None

    # Status tracking
    status: str = "sourced"
    decision: str = ""
    notes: str = ""
    seller_name: str = ""
    seller_contact: str = ""

    # Timestamps
    created_at: str = ""
    updated_at: str = ""
    contacted_at: Optional[str] = None
    bought_at: Optional[str] = None
    listed_at: Optional[str] = None
    sold_at: Optional[str] = None

    # Reminders
    next_action: str = ""
    next_action_due: Optional[str] = None

    # Tags for filtering
    tags: str = ""          # comma-separated: "urgent,hot-deal,needs-mot"

    # Linked data
    offer_message: str = ""
    listing_draft_id: Optional[int] = None


# Valid lead status transitions
LEAD_STATUSES = [
    "sourced", "contacted", "inspecting", "bidding", "bought",
    "preparing", "listed", "sold", "closed", "lost", "withdrawn",
]

LEAD_STATUS_TRANSITIONS = {
    "sourced":    ["contacted", "lost", "withdrawn"],
    "contacted":  ["inspecting", "bidding", "lost", "withdrawn"],
    "inspecting": ["bidding", "bought", "lost", "withdrawn"],
    "bidding":    ["bought", "lost", "withdrawn"],
    "bought":     ["preparing", "withdrawn"],
    "preparing":  ["listed", "withdrawn"],
    "listed":     ["sold", "withdrawn"],
    "sold":       ["closed"],
    "closed":     [],
    "lost":       ["sourced"],      # can re-open a lost lead
    "withdrawn":  ["sourced"],      # can re-open a withdrawn lead
}


# ---------------------------------------------------------------------------
# Analytics models (v0.9.0)
# ---------------------------------------------------------------------------

@dataclass
class PriceTrend:
    """Price trend data for a vehicle key over time."""
    vehicle_key: str
    direction: str          # "rising" | "falling" | "stable"
    pct_change_7d: float    # % change over last 7 days
    pct_change_30d: float   # % change over last 30 days
    current_median: float
    sample_size: int
    confidence: float       # 0-1, based on sample size


@dataclass
class DemandSignal:
    """Demand signal for a vehicle key."""
    vehicle_key: str
    level: str              # "high" | "medium" | "low"
    listings_per_day: float
    avg_days_to_sell: float
    seasonal_factor: float  # 1.0 = normal, >1 = above seasonal average
    competition_count: int  # active listings in same category


@dataclass
class DealPrediction:
    """Predicted success probability and optimal pricing for a deal."""
    success_probability: float  # 0-1
    optimal_buy_price: float
    optimal_sell_price: float
    expected_days_to_sell: int
    confidence: float


# ---------------------------------------------------------------------------
# Posting models (v0.9.0)
# ---------------------------------------------------------------------------

@dataclass
class PostingDraft:
    """
    A draft listing for multi-platform posting.
    Generated by AI, edited by dealer, then pushed to platforms.
    """
    id: Optional[int] = None
    lead_id: Optional[int] = None
    vrm: str = ""
    title: str = ""
    description: str = ""
    price_gbp: float = 0.0
    suggested_price: float = 0.0

    # AI-generated content
    ai_description: str = ""
    ai_bullet_points: str = ""      # JSON list of selling points
    ai_price_suggestion: float = 0.0
    ai_image_tags: str = ""         # JSON list of image analysis results

    # Platform targets
    platforms: str = ""              # comma-separated: "ebay,facebook,autotrader"
    ebay_posted: bool = False
    facebook_posted: bool = False
    autotrader_posted: bool = False

    # Metadata
    created_at: str = ""
    updated_at: str = ""
    status: str = "draft"           # "draft" | "ready" | "posted" | "active" | "sold"

    # Image paths (local)
    image_paths: str = ""           # comma-separated file paths


# ---------------------------------------------------------------------------
# Dealer Network models (v0.9.0)
# ---------------------------------------------------------------------------

@dataclass
class DealerProfile:
    """A dealer in the B2B network."""
    id: Optional[int] = None
    name: str = ""
    location: str = ""
    postcode: str = ""
    phone: str = ""
    email: str = ""
    specialties: str = ""           # comma-separated: "japanese,german,budget"
    rating: float = 0.0
    trade_count: int = 0
    created_at: str = ""
    is_active: bool = True


@dataclass
class DealerInventoryItem:
    """A vehicle listed on the dealer-to-dealer B2B network."""
    id: Optional[int] = None
    dealer_id: int = 0
    vrm: str = ""
    title: str = ""
    description: str = ""
    price_gbp: float = 0.0
    trade_price: float = 0.0        # wholesale / trade price
    condition_notes: str = ""
    mot_expiry: str = ""
    mileage: Optional[int] = None
    year: Optional[int] = None
    fuel_type: str = ""
    colour: str = ""
    image_urls: str = ""            # comma-separated
    created_at: str = ""
    status: str = "available"       # "available" | "reserved" | "sold"
    views: int = 0


@dataclass
class DealerMessage:
    """A message between dealers in the B2B network."""
    id: Optional[int] = None
    from_dealer_id: int = 0
    to_dealer_id: int = 0
    inventory_item_id: Optional[int] = None
    subject: str = ""
    body: str = ""
    created_at: str = ""
    read: bool = False

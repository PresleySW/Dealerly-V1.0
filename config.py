"""
dealerly/config.py
==================
v0.9.4 changes:
  - default_target_margin() now capital-tiered: 10% under £5k, 12% £5-10k, 14% over
  - Admin buffer reduced to £30 (local pickup with postcode bias)
  - Transport buffer reduced to £40
  - Version bump

Recent:
  - UK_STATS_MAP_HTML — path for the 3D UK run-history page (see pipeline + report).

No I/O. No imports from other Dealerly modules.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERE: Path = Path(__file__).resolve().parent
DATA_DIR: Path = HERE.parent
DB_PATH: Path = DATA_DIR / (os.environ.get("DEALERLY_DB_FILE", "dealerly.db"))
MOT_SAMPLES_DIR: Path = DATA_DIR / "mot_samples"
REPORTS_DIR: Path = DATA_DIR / "reports"
# UK 3D stats board (pipeline run history); rewritten after each successful Phase 6
UK_STATS_MAP_HTML: Path = REPORTS_DIR / "uk_stats_map.html"
DEAL_LOG_PATH: Path = DATA_DIR / "dealerly_log.csv"
INPUT_CSV: Path = DATA_DIR / "dealerly_input.csv"
IMAGES_DIR: Path = DATA_DIR / "images"
# Optional PNG marks for HTML report (platform badges + footer); base64-embedded at generate time
LOGOS_DIR: Path = DATA_DIR / "Logos"

# ---------------------------------------------------------------------------
# Search / pagination
# ---------------------------------------------------------------------------

DEFAULT_PRICE_MIN: int = 800
DEFAULT_PRICE_MAX: int = 2500
DEFAULT_EXPECTED_DAYS: int = 12
DEFAULT_TOP_N: int = 15
DEFAULT_PAGES: int = 4
PAGE_SIZE: int = 50
REQUEST_SLEEP_S: float = 0.25

# ---------------------------------------------------------------------------
# Enrichment / shortlisting
# ---------------------------------------------------------------------------

DEFAULT_SHORTLIST_ENRICH_N: int = 28
DEFAULT_NEAR_MISS_BAND: float = 300.0

# ---------------------------------------------------------------------------
# Fees & cost buffers
# ---------------------------------------------------------------------------
# v0.9.4: reduced from £50/£60 — with postcode-biased search returning
# local cars, typical pickup is <30 miles from Egham base.

DEFAULT_EBAY_FEE_RATE: float = 0.06
DEFAULT_PAYMENT_FEE_RATE: float = 0.02
DEFAULT_ADMIN_BUFFER: float = 30.0
DEFAULT_TRANSPORT_BUFFER: float = 40.0

# ---------------------------------------------------------------------------
# Comps / valuation
# ---------------------------------------------------------------------------

DEFAULT_COMPS_LOOKUP_LIMIT: int = 40
DEFAULT_COMPS_TTL_HOURS: float = 12.0
# Slightly conservative vs median comps — reduces overstated profit when market is soft.
DEFAULT_RESALE_DISCOUNT: float = 0.95
DEFAULT_MISPRICE_RATIO: float = 0.90

# ---------------------------------------------------------------------------
# AI / API
# ---------------------------------------------------------------------------

DEFAULT_OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
DEFAULT_CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
OPENAI_TIMEOUT_S: int = 40
CLAUDE_TIMEOUT_S: int = 60
OPENAI_BASE_URL: str = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
CLAUDE_BASE_URL: str = "https://api.anthropic.com/v1"
CLAUDE_ANTHROPIC_VERSION: str = "2023-06-01"

DEFAULT_OPENAI_ENRICH_IMAGES: int = 4

# ---------------------------------------------------------------------------
# VRM confidence thresholds
# ---------------------------------------------------------------------------

MIN_VRM_CONFIDENCE: float = 0.72
MIN_VRM_CONFIDENCE_FOR_BUY: float = 0.86
MIN_VRM_DISPLAY_CONFIDENCE: float = 0.74  # v0.9.5.9: lowered from 0.80 so DVLA-penalised scrape VRMs (floor 0.75) still display
# v1.0: lower bar for the MOT-pending BUY exception — intentionally below MIN_VRM_CONFIDENCE_FOR_BUY
# because this only gates the *exception* path (high-profit + confirmed plate), not the full BUY gate.
MIN_VRM_CONFIDENCE_MOT_EXCEPTION: float = 0.80

# Low buy-price flips: relax BUY vs OFFER threshold (risk.py classify_decision)
LOW_FLIP_CAPITAL_MAX: float = 5_000.0
LOW_FLIP_BUY_PRICE_MAX: float = 2_500.0
# ~0.45× margin target on sub-£2.5k buys — e.g. £240 → £108 so £111 profit clears as BUY
LOW_FLIP_EFFECTIVE_TARGET_MULT: float = 0.45
LOW_FLIP_EFFECTIVE_TARGET_FLOOR: float = 90.0

# ---------------------------------------------------------------------------
# AutoTrader
# ---------------------------------------------------------------------------

AUTOTRADER_TTL_HOURS: float = 6.0
AUTOTRADER_SLEEP_S: float = 2.5

# ---------------------------------------------------------------------------
# DVLA Vehicle Enquiry
# ---------------------------------------------------------------------------

DVLA_ENQUIRY_URL: str = "https://driver-vehicle-licensing.api.gov.uk/vehicle-enquiry/v1/vehicles"
DVLA_ENQUIRY_SLEEP_S: float = 1.0
DVLA_CACHE_TTL_HOURS: float = 168.0

# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

ANALYTICS_TREND_WINDOW_DAYS: int = 30
ANALYTICS_MIN_OBSERVATIONS: int = 5
ANALYTICS_DEMAND_HIGH_THRESHOLD: float = 3.0
ANALYTICS_DEMAND_LOW_THRESHOLD: float = 0.5

SEASONAL_FACTORS: list = [
    0.85, 0.90, 1.05, 1.10, 1.05, 1.00,
    0.95, 0.90, 1.10, 1.05, 0.95, 0.80,
]

# ---------------------------------------------------------------------------
# Workflow / CRM
# ---------------------------------------------------------------------------

DEFAULT_FOLLOW_UP_HOURS: int = 48


def obsidian_vault_path() -> Path:
    """
    Local Obsidian vault root (not in git). Set DEALERLY_OBSIDIAN_VAULT to
    override when the repo lives on another drive or machine.
    """
    raw = os.environ.get("DEALERLY_OBSIDIAN_VAULT", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path("D:/RHUL/Dealerly/Dealerly_Vault")
DEFAULT_INSPECTION_DEADLINE_DAYS: int = 7
AUTO_CREATE_LEADS: bool = True

# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

POSTING_PLATFORMS: list = ["ebay", "facebook", "autotrader"]
POSTING_AI_MODEL: str = DEFAULT_CLAUDE_MODEL
POSTING_MAX_DESCRIPTION_WORDS: int = 250
POSTING_MAX_IMAGES: int = 12

# ---------------------------------------------------------------------------
# Dealer Network
# ---------------------------------------------------------------------------

NETWORK_DEFAULT_RADIUS_MILES: int = 50
NETWORK_MAX_LISTINGS: int = 100

# ---------------------------------------------------------------------------
# Location / search radius (v0.9.3)
# ---------------------------------------------------------------------------

DEFAULT_BUYER_POSTCODE: str = "TW200AY"
DEFAULT_SEARCH_RADIUS_MILES: int = 75

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

USER_AGENT: str = os.environ.get(
    "DEALERLY_USER_AGENT",
    "Dealerly/1.0.0-rc.1 (+https://swautosuk.com)",
)
VERSION: str = "1.0.0-rc.1"

# ---------------------------------------------------------------------------
# Sprint 3: ingestion platform selection
# ---------------------------------------------------------------------------
# Controls which platform adapters are instantiated during Phase 1.
# Valid values: "ebay", "facebook", "motors"
# Extend this list to enable additional adapters in "all" / "multi" modes.

ENABLED_PLATFORMS: list = ["ebay", "motors", "facebook", "pistonheads"]  # Facebook self-checks Playwright; PistonHeads is always available

# ---------------------------------------------------------------------------
# Plate Recognizer ANPR
# ---------------------------------------------------------------------------

PLATE_RECOGNIZER_URL: str = "https://api.platerecognizer.com/v1/plate-reader/"
PLATE_RECOGNIZER_SLEEP_S: float = 0.3
ANPR_MIN_SCORE_THRESHOLD: float = 50.0  # min expected_profit (£) from prelim scoring to warrant ANPR call


def anpr_max_images() -> int:
    """
    Max listing photos sent to Plate Recognizer per ANPR attempt.
    Lower = fewer API calls (e.g. 2–3 when quota is tight). Env: DEALERLY_ANPR_MAX_IMAGES (1–6).
    """
    raw = os.environ.get("DEALERLY_ANPR_MAX_IMAGES", "").strip()
    if raw:
        try:
            return max(1, min(6, int(raw)))
        except ValueError:
            pass
    return 6


DEFAULT_PRIORITY_ENRICH_N: int = 5  # top-N candidates guaranteed ANPR/DVLA before general loop


def priority_enrich_n() -> int:
    """
    Number of highest-profit eBay candidates guaranteed ANPR/DVLA enrichment
    before the general Phase 3 loop, regardless of idx-slice caps.
    Env: DEALERLY_PRIORITY_ENRICH_N (default 5).
    """
    raw = os.environ.get("DEALERLY_PRIORITY_ENRICH_N", "").strip()
    if raw:
        try:
            return max(1, min(20, int(raw)))
        except ValueError:
            pass
    return DEFAULT_PRIORITY_ENRICH_N


def anpr_min_profit_gbp() -> float:
    """
    Min prelim expected_profit (£) to spend ANPR on a listing in Phase 3 / 4.5 gates.
    Env: DEALERLY_ANPR_MIN_PROFIT_GBP (overrides ANPR_MIN_SCORE_THRESHOLD default).
    """
    raw = os.environ.get("DEALERLY_ANPR_MIN_PROFIT_GBP", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(ANPR_MIN_SCORE_THRESHOLD)


DEFAULT_ANPR_PROFIT_WEIGHT: float = 1.5


def anpr_profit_weight() -> float:
    """
    Profit multiplier for ANPR budget gating (Sprint 12 profit-weighted budget).
    ANPR is allocated to listings where expected_profit >= multiplier x anpr_min_profit.
    Env: DEALERLY_ANPR_PROFIT_WEIGHT (default 1.5).
    """
    raw = os.environ.get("DEALERLY_ANPR_PROFIT_WEIGHT", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_ANPR_PROFIT_WEIGHT


def fb_max_listings() -> int:
    """
    Hard cap on Facebook Marketplace listings gathered per run.
    Stops scroll cycles once this many unique URLs have been collected.
    Env: DEALERLY_FB_MAX_LISTINGS (default 400, min 10).
    """
    raw = os.environ.get("DEALERLY_FB_MAX_LISTINGS", "").strip()
    if raw:
        try:
            return max(10, int(raw))
        except ValueError:
            pass
    return 400


def dealerly_runtime_banner() -> str:
    """
    One-line summary for HTML report (no secrets). Surfaces local AI + ANPR tuning.
    """
    parts: list[str] = []
    base = OPENAI_BASE_URL.rstrip("/").lower()
    default_oai = "https://api.openai.com/v1"
    if base and base != default_oai:
        host = base.replace("https://", "").replace("http://", "").split("/")[0]
        parts.append(f"AI: OpenAI-compatible @ {host}")
    parts.append(
        f"ANPR: ≤{anpr_max_images()} photos/listing · profit gate £{anpr_min_profit_gbp():.0f}+"
    )
    return " · ".join(parts)

# ---------------------------------------------------------------------------
# Google Sheets export (Sprint 2)
# ---------------------------------------------------------------------------

GOOGLE_SHEET_ID: str = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON_PATH: str = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON", ""
)

# ---------------------------------------------------------------------------
# Model reliability tiers (v0.9.3)
# ---------------------------------------------------------------------------

MODEL_RELIABILITY_TIERS: dict[tuple[str, str], int] = {
    ("honda",      "jazz"):     1,
    ("toyota",     "yaris"):    1,
    ("toyota",     "auris"):    1,
    ("toyota",     "corolla"):  1,
    ("dacia",      "sandero"):  1,
    ("suzuki",     "swift"):    1,
    ("honda",      "civic"):    2,
    ("skoda",      "fabia"):    2,
    ("hyundai",    "i20"):      2,
    ("kia",        "ceed"):     2,
    ("ford",       "fiesta"):   2,
    ("vauxhall",   "corsa"):    2,
    ("volkswagen", "polo"):     2,
    ("seat",       "ibiza"):    2,
    ("ford",       "focus"):    3,
    ("seat",       "leon"):     3,
    ("vauxhall",   "astra"):    3,
    ("nissan",     "qashqai"):  3,
    ("peugeot",    "208"):      3,
    ("volkswagen", "golf"):     4,
    ("mini",       "mini"):     4,
    ("nissan",     "juke"):     4,
    ("renault",    "clio"):     4,
    ("fiat",       "500"):      4,
    ("volvo",      "v40"):      4,
}

# ---------------------------------------------------------------------------
# Query presets (v0.9.3 trimmed)
# ---------------------------------------------------------------------------

QUERY_PRESETS: dict[str, dict] = {
    "1": {"mode": "single", "qs": ["used car"], "desc": "broad single-query (slow)"},
    "2": {"mode": "multi", "qs": ["ford fiesta", "vauxhall corsa", "volkswagen polo"], "desc": "budget trio"},
    "3": {"mode": "multi", "qs": ["ford fiesta", "vauxhall corsa", "volkswagen polo", "ford focus", "toyota yaris"], "desc": "budget five"},
    "4": {"mode": "multi", "qs": ["honda jazz", "toyota yaris", "toyota auris", "skoda fabia", "skoda octavia"], "desc": "reliable Japanese/Czech pool"},
    "5": {"mode": "multi", "qs": ["honda jazz", "toyota yaris", "skoda fabia", "seat leon", "seat ibiza", "honda civic"], "desc": "reliable Japanese/Euro pool"},
    "6": {"mode": "multi", "qs": ["ford fiesta", "vauxhall corsa", "volkswagen polo", "ford focus", "honda jazz", "toyota yaris"], "desc": "volume flip pool (top-6 profitable)"},
    "7": {"mode": "multi", "qs": ["honda jazz", "honda civic", "toyota yaris", "toyota auris", "skoda fabia", "skoda octavia", "seat ibiza", "seat leon", "hyundai i20", "kia ceed", "ford fiesta", "vauxhall corsa"], "desc": "broad reliable pool (12 models)"},
    "8": {"mode": "multi", "qs": [
        # Japanese reliability tier
        "honda jazz", "toyota yaris", "toyota auris", "honda civic",
        # Czech/Euro reliable
        "skoda fabia", "skoda octavia", "seat ibiza", "volkswagen polo",
        # Korean value
        "hyundai i20", "hyundai i10", "kia picanto", "kia ceed",
        # Budget hatchbacks with broad comps
        "ford fiesta", "vauxhall corsa", "peugeot 208", "renault clio",
        # Low-cost reliable newcomers
        "suzuki swift", "dacia sandero", "nissan micra", "nissan note",
    ], "desc": "maximum variety — 20 models, deepest comps pool"},
    "9": {"mode": "multi", "qs": [
        "honda jazz", "toyota yaris", "ford fiesta", "vauxhall corsa",
        "hyundai i10", "kia picanto", "suzuki swift", "dacia sandero",
    ], "desc": "flipper preset — low-risk, fast-turn budget set"},
    "10": {"mode": "multi", "qs": [
        "honda civic", "toyota auris", "skoda octavia", "seat leon",
        "volkswagen polo", "ford focus", "kia ceed", "hyundai i20",
        "nissan qashqai", "renault clio", "peugeot 208", "vauxhall astra",
    ], "desc": "dealer preset — broader batch inventory pool"},
}

# Typical UK used-price bands (cheapest first). Used to filter preset search
# terms so low-capital runs focus on affordable stock; higher capital adds
# larger hatch / crossover queries. (Not exhaustive — intersects with preset.)
VEHICLE_QUERY_TIERS: tuple[tuple[str, ...], ...] = (
    (
        "hyundai i10",
        "kia picanto",
        "dacia sandero",
        "nissan micra",
        "suzuki swift",
        "fiat 500",
        "toyota aygo",
    ),
    (
        "ford fiesta",
        "vauxhall corsa",
        "volkswagen polo",
        "ford focus",
        "toyota yaris",
        "skoda fabia",
        "seat ibiza",
        "seat leon",
        "peugeot 208",
        "renault clio",
        "nissan note",
        "honda jazz",
        "mini cooper",
    ),
    (
        "honda civic",
        "toyota auris",
        "skoda octavia",
        "kia ceed",
        "hyundai i20",
        "volkswagen golf",
        "ford mondeo",
    ),
    (
        "nissan qashqai",
        "nissan juke",
        "vauxhall astra",
        "bmw 1 series",
        "volkswagen passat",
    ),
)


def _buying_power_gbp(capital: float, price_max: int) -> float:
    """Effective budget for model-mix decisions (capital vs search ceiling)."""
    c = max(500.0, float(capital))
    p = max(500, int(price_max))
    return min(c, float(p))


def _vehicle_tier_count_for_capital(capital: float, price_max: int) -> int:
    """
    How many VEHICLE_QUERY_TIERS to include (1–4) from buying power.
    Low capital → micro + supermini (tiers 1–2); mid → add compacts; high → SUVs / premium.
    """
    b = _buying_power_gbp(capital, price_max)
    if b < 4_000:
        return 2
    if b < 8_000:
        return 3
    if b < 15_000:
        return 4
    return 4


def scale_vehicle_queries_for_capital(
    base_queries: list[str],
    capital: float,
    price_max: int,
) -> list[str]:
    """
    Filter (and lightly cap) multi-query preset terms to match capital / price_max.

    Preserves order of *base_queries* where a term appears in the allowed tier set.
    If too few matches (sparse preset), returns *base_queries* unchanged.
    Set ``DEALERLY_SKIP_QUERY_SCALING=1`` to disable.
    """
    if os.environ.get("DEALERLY_SKIP_QUERY_SCALING", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return list(base_queries)
    if not base_queries:
        return []
    n_tiers = _vehicle_tier_count_for_capital(capital, price_max)
    allowed: set[str] = set()
    for tier in VEHICLE_QUERY_TIERS[:n_tiers]:
        allowed.update(s.lower().strip() for s in tier)

    seen: set[str] = set()
    out: list[str] = []
    for q in base_queries:
        key = q.lower().strip()
        if key in allowed and key not in seen:
            seen.add(key)
            out.append(q)

    if len(out) < 3:
        return list(base_queries)

    b = _buying_power_gbp(capital, price_max)
    if b < 4_000:
        max_q = 6
    elif b < 8_000:
        max_q = 10
    elif b < 15_000:
        max_q = 14
    else:
        max_q = 20

    return out[: max_q]


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class Config:
    capital: float
    price_min: int
    price_max: int
    target_margin: float
    holding_cost: float
    ebay_fee_rate: float
    pay_fee_rate: float
    admin_buffer: float
    transport_buffer: float
    mot_mode: str
    category_ids: str
    pages: int
    near_miss_band: float
    auction_only: bool
    store_comps: bool
    comps_ttl: float
    resale_discount: float
    preset: str
    enrich_mode: str
    enrich_n: int
    sort: str
    misprice_ratio: float
    require_comps: bool
    open_html_report: bool = True
    ai_backend: str = "claude"
    use_autotrader: bool = True
    generate_offer_msgs: bool = True
    autotrader_postcode: str = "TW200AY"
    input_mode: str = "all"
    enable_dvla: bool = True
    enable_analytics: bool = True
    enable_workflow: bool = True
    enable_posting: bool = False
    enable_network: bool = False
    buyer_postcode: str = DEFAULT_BUYER_POSTCODE
    search_radius_miles: int = DEFAULT_SEARCH_RADIUS_MILES
    debug_mode: bool = False   # v0.9.9: write JSON debug log to reports/
    agent_mode: bool = False   # Sprint 16: Claude-driven adaptive search loop
    # 0 = full pipeline; 2 = stop after Phase 2 (prelim scoring); 3 = stop after Phase 3 (VRM enrich)
    stop_after_phase: int = 0


# ---------------------------------------------------------------------------
# Derived defaults
# ---------------------------------------------------------------------------

def default_target_margin(capital: float) -> float:
    """
    Capital-tiered margin target (v0.9.7).

    Under £5k:  8% x capital, floor £150  (£3k -> £240)
    £5k-£10k:   10% x capital             (£8k -> £800, capped £600)
    Over £10k:  12% x capital             (capped at £600)

    v0.9.7: lowered sub-£5k rate from 10% to 8%. At £3k capital
    buying £800-£2500 cars, 10% (£300) required finding 35-40%
    discounts vs comps — almost never achieved. 8% (£240) is
    achievable for genuine 25-30% undervalued listings.
    """
    if capital < 5_000:
        return max(150.0, min(600.0, 0.08 * capital))
    if capital < 10_000:
        return max(250.0, min(600.0, 0.10 * capital))
    return max(250.0, min(600.0, 0.12 * capital))


def default_holding_cost(capital: float) -> float:
    """
    3% of capital, capped 80-200 (v0.9.7).

    v0.9.7: reduced from 5% (£150 at £3k) to 3% (£90 at £3k).
    Holding cost scaled with capital rather than deal size, making
    it too punishing for the £800-£2500 price range with 14-day
    average hold periods.
    """
    return max(80.0, min(200.0, 0.03 * capital))


# ---------------------------------------------------------------------------
# Mode profiles (Sprint 2)
# ---------------------------------------------------------------------------

@dataclass
class ModeProfile:
    """
    Operational mode profile — controls economics, ANPR budget,
    enrichment depth, report style, and offer tone.

    Values here *override* Config defaults at runtime when --mode is set.
    Only fields that differ between modes need to be specified; the rest
    fall back to Config defaults set in cli.py.
    """
    name: str                           # "flipper" | "dealer"
    capital_default: float              # suggested starting capital
    price_min: int                      # lower bound of search range
    price_max: int                      # upper bound of search range
    target_margin_pct: float            # target margin as fraction (0.15 = 15%)
    enrich_n: int                       # how many listings to VRM-enrich
    anpr_budget: int                    # max ANPR calls per run (0 = unlimited)
    top_n: int                          # listings shown in report
    offer_tone: str                     # "casual" | "professional"
    ulez_hard_filter: bool              # if True, exclude ULEZ-fail listings
    use_sheets: bool                    # push to Google Sheets after run
    report_style: str                   # "cards" | "table" (reserved for future)
    description: str = ""


FLIPPER_PROFILE = ModeProfile(
    name="flipper",
    capital_default=2_000.0,
    price_min=500,
    price_max=2_500,
    target_margin_pct=0.15,     # 15% — higher % on cheaper cars
    enrich_n=10,                # focus on top-10 only
    anpr_budget=20,             # tight credit budget
    top_n=5,                    # "show me the best one"
    offer_tone="casual",
    ulez_hard_filter=True,      # can't absorb ULEZ risk on small capital
    use_sheets=False,
    report_style="cards",
    description="Personal flipping — small capital, conservative, top-5 focus",
)

DEALER_PROFILE = ModeProfile(
    name="dealer",
    capital_default=8_000.0,
    price_min=800,
    price_max=4_000,
    target_margin_pct=0.12,     # 12% volume play
    enrich_n=25,                # enrich full shortlist
    anpr_budget=0,              # no budget cap
    top_n=15,                   # full pipeline view
    offer_tone="professional",
    ulez_hard_filter=False,     # can sell outside ULEZ zone
    use_sheets=True,            # Sheets is essential for dealer pipeline
    report_style="cards",
    description="SWAutos dealer mode — batch sourcing, full pipeline, Sheets export",
)

MODE_PROFILES: dict[str, ModeProfile] = {
    "flipper": FLIPPER_PROFILE,
    "dealer":  DEALER_PROFILE,
}

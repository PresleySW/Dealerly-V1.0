"""
dealerly/repair.py
==================
Repair cost estimation and MOT risk uplift.

Responsibilities:
  - REPAIR_PROFILES: per-make/model known failure costs and notes
  - BAD_WORDS / GOOD_WORDS: title-keyword cost adjustments
  - MOT_RISK_KEYWORDS: MOT advisory/failure cost uplift signals
  - estimate_repairs(): combine title scan + profile → (base, worst, notes)
  - mot_uplift_and_confidence(): parse DVSA payload → cost uplift + p_mot

Depends on:
  - dealerly.utils (clamp)

No I/O. No DB. Pure logic only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from dealerly.config import MODEL_RELIABILITY_TIERS
from dealerly.utils import clamp


# ---------------------------------------------------------------------------
# Per-make/model repair profiles
# ---------------------------------------------------------------------------
# Each entry: (base_add £, worst_add £, notes, risk_keywords)
# These are added ON TOP of the title-keyword estimate.
# Source: observed auction outcomes + trade knowledge (SWAutos, Mar 2026).

RepairProfile = Tuple[float, float, str, List[str]]

REPAIR_PROFILES: Dict[Tuple[str, str], RepairProfile] = {
    # Honda
    ("honda",      "jazz"):      (  0,  250, "CVT gearbox slip: £800–1500. Rust sills/rear arches. Generally very reliable.", ["cvt", "gearbox slip"]),
    ("honda",      "civic"):     ( 50,  350, "i-CTDI diesel: injector issues £600+. EGR £300. 1.8 petrol very solid.", ["injector", "egr"]),
    ("honda",      "crv"):       ( 75,  400, "Diesel: timing chain £800+. PAS rack on older cars.", ["timing chain", "steering"]),
    # Volkswagen Group
    ("volkswagen", "golf"):      (120,  700, "TDI: DPF £900, timing chain £1200+. DSG service £350. 1.4 TSI: timing chain.", ["dpf", "timing chain", "dsg"]),
    ("volkswagen", "polo"):      ( 60,  400, "1.2 TSI: timing chain £800–1500. Coil packs. Cambelt on older 1.4.", ["timing chain", "coil pack"]),
    ("volkswagen", "passat"):    (150,  800, "DSG/timing chain. Diesel: DPF/EGR. Suspension bushes.", ["dsg", "dpf", "suspension"]),
    ("skoda",      "fabia"):     ( 50,  350, "1.9 TDI PD: injector seals £300–600. Timing belt every 60k.", ["injector", "timing belt"]),
    ("skoda",      "octavia"):   (100,  550, "TDI: DSG/DQ200 issues, DPF. vRS: clutch £800. Timing belt.", ["dpf", "dsg", "clutch"]),
    ("seat",       "ibiza"):     ( 50,  300, "1.2 TSI: timing chain. FR: front splitter. 1.4 TDI: timing belt.", ["timing chain"]),
    ("seat",       "leon"):      ( 80,  450, "TDI: DPF common. 1.4 TSI: timing chain. FR: clutch.", ["dpf", "timing chain"]),
    # Toyota
    ("toyota",     "yaris"):     (  0,  100, "Extremely reliable. D-4D diesel: EGR but rare. Hybrid: battery age.", []),
    ("toyota",     "auris"):     (  0,  200, "Hybrid: check battery condition. 1.6 petrol very reliable.", ["hybrid battery"]),
    ("toyota",     "corolla"):   (  0,  150, "Very reliable. Check hybrid battery on hybrid models.", ["hybrid battery"]),
    # Ford Fiesta — engine-variant sub-profiles selected by get_repair_profile()
    ("ford",       "fiesta"):    ( 30,  300, "General: check for rust, clutch, suspension. Overall reliable.", ["clutch"]),
    # Ford Focus — v0.9.6: base lowered; variant profiles handle specific engines
    ("ford",       "focus"):     ( 50,  350, "Check gearbox type (manual=OK, Powershift=avoid). Rear beam bushes.", ["clutch"]),
    ("ford",       "kuga"):      (100,  600, "1.5 EcoBoost: coolant issue like Fiesta. Sync3 issues.", ["coolant"]),
    # Vauxhall
    ("vauxhall",   "corsa"):     ( 50,  350, "1.0–1.2: timing chain stretch £600+. Smoky diesels common.", ["timing chain"]),
    ("vauxhall",   "astra"):     ( 80,  450, "Diesel: DPF/EGR. 1.4T: timing chain. Estate tailgate struts.", ["dpf", "timing chain"]),
    # Hyundai / Kia
    ("hyundai",    "i20"):       (  0,  150, "Very reliable. Check for previous accident damage.", []),
    ("hyundai",    "i30"):       ( 25,  250, "Diesel: DPF if city driven. 1.6 GDi: carbon build-up.", ["dpf"]),
    ("kia",        "ceed"):      ( 25,  250, "Generally reliable. Diesel DPF if city use. Check service history.", ["dpf"]),
    ("kia",        "sportage"):  ( 50,  350, "Diesel: DPF. Check for chain noise on 1.7 CRDi.", ["dpf", "timing chain"]),
    # Nissan
    ("nissan",     "micra"):     (  0,  150, "Very reliable. Check timing chain on 1.2 (pre-2017).", ["timing chain"]),
    ("nissan",     "juke"):      ( 60,  450, "CVT: £1200–2000 if failed. 1.6T: intercooler hose split.", ["cvt", "intercooler"]),
    ("nissan",     "qashqai"):   ( 75,  500, "DPF on diesel. CVT issues. Timing chain 1.6 petrol.", ["dpf", "cvt", "timing chain"]),
    # Other
    ("mazda",      "mx5"):       ( 80,  500, "Rust: sills, floor, chassis rails. Budget £300–1500 for bodywork.", ["rust", "corrosion"]),
    ("renault",    "clio"):      ( 60,  400, "EDC auto: avoid unless serviced. 1.2 TCe: timing chain. Rust.", ["timing chain", "edc"]),
    ("peugeot",    "208"):       ( 50,  350, "EAT6 auto: ongoing reliability concerns. 1.2 PureTech: timing belt.", ["timing belt", "eat"]),
    ("citroen",    "c3"):        ( 50,  300, "EAT6 gearbox. 1.2 PureTech: timing belt at 5yr/80k.", ["timing belt"]),
    ("fiat",       "500"):       ( 75,  450, "Twin-air: timing chain/belt hybrid issue. Bodywork rust.", ["timing chain", "rust"]),
    ("mini",       "mini"):      (100,  600, "N14 engine: oil pump failure risk. Chain tensioner. Expensive parts.", ["oil pump", "timing chain"]),
    ("dacia",      "sandero"):   (  0,  150, "Very robust. 1.0 SCe is the most reliable.", []),
    ("suzuki",     "swift"):     (  0,  150, "Very reliable. Check for warranty work on older 1.2.", []),
    ("volvo",      "v40"):       (100,  600, "D2/D4 diesel: DPF, timing belt. Expensive dealer bills.", ["dpf", "timing belt"]),
}


# ---------------------------------------------------------------------------
# Engine-variant sub-profiles
# ---------------------------------------------------------------------------
# For models with significantly different risk profiles per engine/fuel,
# these override the base profile when title/fuel keywords match.
# Checked in order — first match wins.
# Format: (title_keywords, fuel_keywords, (base_add, worst_add, notes, risk_kw))

_VARIANT_PROFILES: Dict[Tuple[str, str], List] = {
    ("ford", "fiesta"): [
        # 1.0 EcoBoost — coolant-to-cylinder is the big risk
        (["ecoboost", "1.0t", "1.0 t", "125ps", "100ps", "140ps"],
         [],
         (80, 600, "1.0 EcoBoost: coolant-to-cylinder head issue £600–1200. Check for mayo/overheating.", ["coolant", "ecoboost", "overheating"])),
        # 1.4 TDCi / 1.6 TDCi diesel
        (["tdci", "1.4 diesel", "1.6 diesel", "1.4tdci", "1.6tdci"],
         ["diesel"],
         (60, 450, "TDCi diesel: DPF £600–900, injectors £400–800. Timing belt due every 100k. ULEZ fail if pre-2015.", ["dpf", "injector", "timing belt"])),
        # 1.25 / 1.4 / 1.6 Duratec petrol — very reliable
        (["1.25", "1.4 petrol", "1.6 petrol", "duratec", "zetec"],
         ["petrol"],
         (20, 200, "Duratec petrol: very reliable. Clutch at 80k+ £350. Minor rust on pre-2012.", ["clutch"])),
        # Powershift auto — avoid
        (["powershift", "auto", "automatic"],
         [],
         (200, 1500, "Powershift DCT auto: known £1200–2000 gearbox failure. AVOID unless manual.", ["powershift", "gearbox"])),
    ],
    ("ford", "focus"): [
        (["tdci", "diesel"],
         ["diesel"],
         (80, 500, "TDCi diesel: timing belt, DPF, injectors. Budget £400–800 for servicing.", ["dpf", "timing belt", "injector"])),
        (["ecoboost", "1.0"],
         [],
         (80, 600, "1.0 EcoBoost: coolant-to-cylinder head issue. Same as Fiesta.", ["coolant", "ecoboost"])),
        (["powershift", "auto"],
         [],
         (200, 1500, "Powershift DCT auto: known gearbox failure £1200–2000. AVOID.", ["powershift", "gearbox"])),
        # v0.9.6: manual petrol fallback — catches "1.6 Zetec", "2.0 Sport", "Ti-VCT" etc.
        ([],
         [],
         (30, 250, "Duratec/Ti-VCT petrol: reliable. Clutch at 80k+ £350. Minor rust on pre-2012.", ["clutch"])),
    ],
    ("vauxhall", "corsa"): [
        (["1.0", "1.2"],
         ["petrol"],
         (40, 350, "1.0/1.2 petrol: timing chain stretch £600+. Coil packs. Otherwise OK.", ["timing chain", "coil pack"])),
        (["1.4"],
         ["petrol"],
         (30, 250, "1.4 petrol: more reliable than 1.0/1.2. Check timing chain on pre-2014.", ["timing chain"])),
        (["cdti", "diesel", "1.3"],
         ["diesel"],
         (70, 500, "Diesel: DPF issues, smoky exhausts. Timing chain on 1.3 CDTi.", ["dpf", "timing chain"])),
        # v0.9.6: generic petrol fallback when engine size not in title
        ([],
         [],
         (35, 280, "Petrol: generally reliable. Check timing chain on 1.0/1.2 models.", ["timing chain"])),
    ],
    ("volkswagen", "golf"): [
        (["tdi", "diesel"],
         ["diesel"],
         (140, 800, "TDI diesel: DPF £900, timing chain £1200+. DSG service £350.", ["dpf", "timing chain", "dsg"])),
        (["tsi", "1.4 tsi"],
         [],
         (100, 700, "1.4 TSI: timing chain tensioner failure £800–1500. Oil consumption.", ["timing chain"])),
        (["gti", "r"],
         [],
         (150, 900, "GTI/R: high-performance wear. Clutch, brakes, suspension. Check for mods.", ["clutch", "suspension"])),
    ],
}


def get_repair_profile(
    make: str, model: str, title: str = "", fuel_type: str = "",
) -> Optional[RepairProfile]:
    """
    Look up a repair profile by make, model, and optionally title/fuel keywords.

    Tiered fallback (v0.9.6):
      1. Make + model + variant (most specific — engine/fuel sub-profiles)
      2. Make + model (base profile from REPAIR_PROFILES)
      3. Make only (averaged from known models for that make)
      4. None (caller uses generic base estimate)
    """
    key = (make.lower().strip(), model.lower().strip())
    t = (title or "").lower()
    f = (fuel_type or "").lower()

    # 1. Check for engine-variant sub-profiles
    variants = _VARIANT_PROFILES.get(key)
    if variants:
        for title_kws, fuel_kws, profile in variants:
            title_match = any(kw in t for kw in title_kws) if title_kws else True
            fuel_match = any(kw in f for kw in fuel_kws) if fuel_kws else True
            if title_match and fuel_match:
                return profile

    # 2. Base make+model profile
    if key in REPAIR_PROFILES:
        return REPAIR_PROFILES[key]

    # 3. Make-only fallback — average across known models for this make
    make_l = make.lower().strip()
    if make_l:
        make_profiles = [v for k, v in REPAIR_PROFILES.items() if k[0] == make_l]
        if make_profiles:
            avg_base = sum(p[0] for p in make_profiles) / len(make_profiles)
            avg_worst = sum(p[1] for p in make_profiles) / len(make_profiles)
            return (
                round(avg_base),
                round(avg_worst),
                f"Unknown {make.title()} model — estimate based on {len(make_profiles)} known {make.title()} profiles.",
                [],
            )

    return None


# ---------------------------------------------------------------------------
# Title-keyword cost signals
# ---------------------------------------------------------------------------

BAD_WORDS: Dict[str, int] = {
    "spares": 900, "repair": 800, "misfire": 700, "engine light": 600, "eml": 600,
    "gearbox": 1200, "clutch": 650, "overheating": 1100, "smoke": 900,
    "dpf": 900, "timing chain": 1200, "turbo": 1000, "injector": 900,
    "adblue": 900, "no start": 1200, "won't start": 1200, "wont start": 1200,
    "cat s": 1200, "cat n": 800, "category s": 1200, "category n": 800,
    "salvage": 1200, "damage": 900, "damaged": 900,
    "no mot": 700, "needs mot": 600, "outstanding finance": 1100,
    "oil burning": 700, "head gasket": 1800, "water in oil": 2000,
    "abs light": 350, "airbag light": 500, "oil light": 400,
    "rust": 500, "corrosion": 600, "sills": 700,
    "timing belt": 400, "cambelt": 400,
    "hpi": 400, "cvt": 600,
    "needs new engine": 3500, "new engine needed": 3500, "engine blown": 3000,
    "engine gone": 2800, "engine fault": 1800, "major engine": 2200,
    # Cosmetic / bodywork signals (v0.9.9+)
    # base = 0.30×cost, worst = 1.00×cost — costs reflect PDR/respray quotes
    "scuff": 150, "scuffed": 150,           # bumper/panel scuff — PDR/touch-up £50-150
    "stone chip": 100, "paint chip": 100,   # minor chips — touch-up £30-100
    "kerb": 180,                            # kerbed alloys — refurb £60-180/corner
    "paintwork": 200,                       # generic paintwork mention — £60-200
    "slight dent": 200, "minor dent": 200,  # PDR £80-200
    "small dent": 200, "has a dent": 250,
    "slight ding": 180, "minor ding": 180,  # PDR £60-180
    "small ding": 180,  "has a ding": 180,
    "ding on": 180,                         # "ding on door/bonnet/etc"
    "scratch on": 180, "scratched": 180,    # "scratch on X" — £60-180 touch-up
}

GOOD_WORDS: Dict[str, int] = {
    "full service history": -140, "fsh": -120, "recent service": -100,
    "new clutch": -240, "new cambelt": -320, "new timing belt": -320,
    "long mot": -120, "fresh mot": -120, "mot": -40,
    "hpi clear": -80, "starts and drives": -120, "starts & drives": -120,
    "starts": -60, "drives": -80, "just serviced": -120,
    "new battery": -40, "new exhaust": -80, "cambelt done": -280,
    "new tyres": -60, "new brakes": -80,
}


# ---------------------------------------------------------------------------
# MOT advisory/failure keyword → cost uplift + confidence penalty
# ---------------------------------------------------------------------------
# Each entry: (base_uplift £, worst_uplift £, confidence_penalty_points)

MOT_RISK_KEYWORDS: Dict[str, Tuple[float, float, int]] = {
    "corrosion":   (300, 1100, 3), "subframe":    (350, 1300, 4),
    "oil leak":    (200,  700, 2), "brake pipe":  (200,  750, 2),
    "suspension":  (250,  900, 2), "coil spring": (220,  700, 2),
    "steering":    (300, 1100, 3), "emissions":   (300, 1100, 3),
    "dpf":         (500, 1500, 4),
    # v0.9.9 fix: "abs" was matching "absorbers" in "shock absorbers" advisories,
    # adding £900 worst-case to every car with suspension issues and pushing
    # worst_up to the £1500 cap. Changed to "abs warning" to match real ABS
    # failure text ("ABS warning lamp inoperative") without substring false-positives.
    "abs warning": (250,  900, 2),
    "airbag":      (350, 1200, 3), "tyre":        (120,  450, 1),
    "brake pad":   (150,  400, 1), "wiper":       ( 40,  120, 0),
    "bulb":        ( 30,   80, 0),
}


# ---------------------------------------------------------------------------
# Estimation functions
# ---------------------------------------------------------------------------

def estimate_repairs(
    title: str,
    make: str = "",
    model: str = "",
    fuel_type: str = "",
    condition_notes: str = "",
) -> Tuple[float, float, str]:
    """
    Estimate repair costs from listing title text plus make/model profile.

    Returns:
        (base_estimate £, worst_case £, profile_notes str)

    Base starts at £180 (minor cosmetics assumed on any used car).
    BAD_WORDS add 30% to base, 100% to worst.
    GOOD_WORDS reduce both.
    Profile adds a fixed premium on top for known failure modes.
    Engine-variant sub-profiles checked via title + fuel_type keywords.
    """
    # Combine title + condition notes (seller description / eBay condition text).
    # condition_notes captures "slight Ding on passenger door" etc. that never
    # appear in the listing title but affect true repair cost.
    t = ((title or "") + " " + (condition_notes or "")).lower()
    # v0.9.5.6: base floor lowered £180→£120. The old floor assumed visible cosmetic
    # work on every car, which over-penalised clean "FSH / Long MOT / recent service"
    # listings and pushed many into PASS. £120 still covers minor consumables (tyres,
    # wiper blades, bulbs) without hard-coding a £60 pessimism premium.
    base, worst = 120.0, 350.0

    for keyword, cost in BAD_WORDS.items():
        if keyword in t:
            base  += 0.30 * cost
            worst += 1.00 * cost

    for keyword, saving in GOOD_WORDS.items():
        if keyword in t:
            base  += saving
            # v0.9.9+: raised from 0.25 → 0.60. If a seller confirms an item
            # is newly replaced ("new clutch", "cambelt done"), the worst-case
            # should reflect that — 0.25 was too weak and left most of the
            # BAD_WORD penalty intact even when the item was confirmed fixed.
            worst += 0.60 * saving

    base  = max(0.0, min(base,  2500.0))
    worst = max(base, min(worst, 6000.0))

    profile = get_repair_profile(make, model, title=title, fuel_type=fuel_type)
    profile_notes = ""
    if profile:
        b_add, w_add, notes, _ = profile
        base  += b_add
        worst += w_add
        profile_notes = notes

    return base, worst, profile_notes


def mot_uplift_and_confidence(
    mot_payload: Optional[Dict[str, Any]],
) -> Tuple[float, float, float, str]:
    """
    Parse a DVSA MOT history payload and return additional repair cost estimates
    plus a MOT pass probability.

    Returns:
        (base_uplift £, worst_uplift £, p_mot 0.65–1.0, diagnostic_notes str)

    v0.9.9 fix: base_uplift uses ONLY the most recent 2 tests (actionable repairs
    the buyer is likely to face now). worst_uplift uses the recent 4 tests
    (current risk window, not whole-life history). p_mot uses recent 6 tests.

    Previously worst_up scanned ALL tests, causing old resolved advisories
    (e.g. emissions fixed in 2006, steering fixed in 2010) to stack and push
    shock_ratio far above the AVOID threshold on any car with 10+ tests.
    A Honda Jazz with 26 tests was getting worst_up > £3000 from stacked
    historical keywords, making shock_ratio = 1.3+ vs threshold 0.41 → AVOID.

    worst_up is also capped at £1500 to prevent single-keyword stacking
    from exceeding the shock threshold at typical £3k capital levels.

    p_mot starts at 1.0 and decreases by 3% per confidence point of MOT keyword hits.
    Returns conservative defaults (0 uplift, p_mot=0.92) when no payload is supplied.
    """
    if not mot_payload:
        return 0.0, 0.0, 0.92, "MOT: no data (p_mot=0.92)."

    tests = mot_payload.get("motTests") or []

    def _collect_blobs(test_list: list) -> str:
        """Extract all advisory/failure text from a list of MOT test dicts."""
        blobs: List[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    lk = str(k).lower()
                    if isinstance(v, str) and any(
                        s in lk for s in ("advis", "defect", "failure", "comment", "text", "reason")
                    ):
                        blobs.append(v.lower())
                    else:
                        _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        for t in test_list:
            _walk(t)
        return " | ".join(blobs)

    # Recent 2 tests → base repair estimate (what buyer faces NOW)
    recent_blob = _collect_blobs(tests[:2])
    # Recent 4 tests → worst-case estimate (current risk window, not whole-life)
    worst_blob  = _collect_blobs(tests[:4])
    # Recent 6 tests → p_mot confidence (broader but still recent pattern)
    pmot_blob   = _collect_blobs(tests[:6])

    base_up, worst_up, pts = 0.0, 0.0, 0
    recent_hits: List[str] = []
    worst_hits:  List[str] = []

    for keyword, (b, w, p) in MOT_RISK_KEYWORDS.items():
        if keyword in recent_blob:
            base_up += b
            recent_hits.append(keyword)
        if keyword in worst_blob:
            worst_up += w
            worst_hits.append(keyword)
        if keyword in pmot_blob:
            pts += p

    # worst_up must be at least as large as base_up
    worst_up = max(worst_up, base_up)

    # Cap worst_up to prevent extreme shock ratios on cars with many
    # concurrent advisories. £1500 keeps shock_ratio ≤ 0.50 at £3k capital
    # even with profile worst_add on top — within AVOID threshold range.
    _WORST_UP_CAP = 1500.0
    worst_up = min(worst_up, _WORST_UP_CAP)

    p_mot = clamp(1.0 - 0.03 * pts, 0.65, 1.00)
    notes = (
        f"MOT recent_hits={sorted(set(recent_hits)) or []}, "
        f"worst_hits={sorted(set(worst_hits)) or []}, p_mot={p_mot:.2f}"
    )
    return base_up, worst_up, p_mot, notes


# ---------------------------------------------------------------------------
# Signal-based p_MOT estimation (v0.9.5)
# ---------------------------------------------------------------------------
# When no VRM is available for a DVSA lookup, estimate p_MOT from listing
# signals instead of the flat 0.92 default. Produces a realistic spread
# that improves ranking differentiation even without MOT history.

_MOT_TITLE_POSITIVE = {
    "12 months mot":  0.06,   "12 month mot":   0.06,
    "long mot":       0.04,   "full mot":       0.04,
    "mot till":       0.03,   "mot until":      0.03,
    "mot to":         0.02,   "recent mot":     0.03,
    "fresh mot":      0.04,   "new mot":        0.04,
    "full service":   0.02,   "full history":   0.02,
    "fsh":            0.02,   "hpi clear":      0.01,
}

_MOT_TITLE_NEGATIVE = {
    "mot failure":   -0.20,   "no mot":        -0.18,
    "mot expired":   -0.15,   "spares":        -0.20,
    "repair":        -0.12,   "non runner":    -0.25,
    "not running":   -0.25,   "won't start":   -0.20,
    "engine light":  -0.10,   "eml":           -0.08,
    "overheating":   -0.12,   "smoke":         -0.08,
    "needs work":    -0.10,   "project":       -0.08,
}


def estimate_p_mot_from_signals(
    title: str,
    year: Optional[int] = None,
    mileage: Optional[int] = None,
    make: str = "",
    model: str = "",
) -> Tuple[float, str]:
    """
    Estimate MOT pass probability from listing signals when no DVSA data
    is available (no VRM found).

    Combines:
      1. Title keyword signals (positive: "long mot", negative: "no mot")
      2. Age penalty: older cars have higher MOT failure rates
      3. Mileage penalty: higher mileage = more wear items
      4. Model reliability tier: Honda Jazz != Fiat 500

    Returns:
        (p_mot 0.55–0.98, diagnostic_notes str)
    """
    t = (title or "").lower()

    # Base: 0.88 (slightly below the old 0.92 default — reflects uncertainty)
    p = 0.88
    signals: List[str] = []

    # 1. Title keyword signals
    for kw, adj in _MOT_TITLE_POSITIVE.items():
        if kw in t:
            p += adj
            signals.append(f"+{kw}")
    for kw, adj in _MOT_TITLE_NEGATIVE.items():
        if kw in t:
            p += adj  # adj is already negative
            signals.append(f"{kw}")

    # 2. Age penalty — MOT failure rate rises with age
    #    DVSA data: ~20% failure rate at 3yr, ~40% at 10yr, ~50% at 15yr
    if year:
        age = 2026 - year
        if age <= 3:
            p += 0.04
        elif age <= 6:
            pass  # neutral — base already accounts for this range
        elif age <= 10:
            p -= 0.03
        elif age <= 15:
            p -= 0.06
        else:
            p -= 0.10
        signals.append(f"age={age}y")

    # 3. Mileage penalty — higher mileage = more worn consumables
    if mileage:
        if mileage < 40_000:
            p += 0.02
        elif mileage < 80_000:
            pass  # neutral
        elif mileage < 120_000:
            p -= 0.03
        elif mileage < 160_000:
            p -= 0.06
        else:
            p -= 0.10
        signals.append(f"mi={mileage//1000}k")

    # 4. Model reliability tier
    tier = MODEL_RELIABILITY_TIERS.get(
        (make.lower().strip(), model.lower().strip())
    )
    if tier is not None:
        # Tier 1 = most reliable, tier 4 = least
        tier_adj = {1: 0.04, 2: 0.01, 3: -0.02, 4: -0.05}
        p += tier_adj.get(tier, 0)
        signals.append(f"tier={tier}")

    p = clamp(p, 0.55, 0.98)
    notes = f"MOT: signal-est p_mot={p:.2f} [{', '.join(signals) or 'no signals'}]"
    return p, notes

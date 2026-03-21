"""
dealerly/vrm.py
===============
UK Vehicle Registration Mark (VRM / number plate) extraction pipeline.

Responsibilities:
  - UK plate regex patterns (new, prefix, suffix, dateless)
  - Blocklist of strings that look like plates but aren't
  - VRM normalisation and plausibility checks
  - Extraction from eBay item specifics, seller description, and title
  - ULEZ compliance inference from year + fuel type

Strategy (v0.9.6):
  Regex-only pipeline — AI vision removed. Three cascading steps:
    1. Item specifics  (highest confidence — seller filled in "Registration" field)
    2. Seller description:
       a. Labelled contexts ("Reg: ...", "Plate: ...") — ALL patterns incl. dateless
       b. Body text scan — SAFE patterns only (no dateless)   ← v0.9.6 fix
    3. Listing title (safe patterns only — no dateless)

  v0.9.6 changes:
    - Description body scan restricted to SAFE patterns (63% FP rate eliminated)
    - Engine CC range rejection (500-6500) for dateless_rev matches
    - Expanded _DIGIT_SPEC_SUFFIXES with common short words (NO, MK, IN, IS, etc.)

Depends on:
  - dealerly.config (MIN_VRM_DISPLAY_CONFIDENCE)

No I/O. No DB. Pure logic only.
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from dealerly.config import MIN_VRM_DISPLAY_CONFIDENCE


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
# Each entry: (compiled_pattern, format_name, confidence)

# For labelled fields ("Reg: ...", item specifics with "registration" key)
# Includes dateless formats — acceptable when the field is explicitly registration-labelled.
ALL_VRM_PATTERNS: List[Tuple[re.Pattern, str, float]] = [
    (re.compile(r"\b([A-Z]{2}[0-9]{2}\s?[A-Z]{3})\b"),  "new_2001",     0.98),
    (re.compile(r"\b([A-Z][0-9]{1,3}\s?[A-Z]{3})\b"),   "prefix_1983",  0.95),
    (re.compile(r"\b([A-Z]{3}\s?[0-9]{1,3}[A-Z])\b"),   "suffix_1963",  0.93),
    (re.compile(r"\b([A-Z]{1,2}\s?[0-9]{3,4})\b"),       "dateless",     0.80),
    (re.compile(r"\b([0-9]{3,4}\s?[A-Z]{1,2})\b"),       "dateless_rev", 0.78),
]

# For general text (title, description body) — NO dateless patterns.
# Dateless formats (e.g. "A123", "123B") collide too frequently with engine
# codes, model numbers, and other non-plate strings in unstructured text.
SAFE_VRM_PATTERNS: List[Tuple[re.Pattern, str, float]] = [
    (re.compile(r"\b([A-Z]{2}[0-9]{2}\s?[A-Z]{3})\b"),  "new_2001",    0.98),
    (re.compile(r"\b([A-Z][0-9]{1,3}\s?[A-Z]{3})\b"),   "prefix_1983", 0.95),
    (re.compile(r"\b([A-Z]{3}\s?[0-9]{1,3}[A-Z])\b"),   "suffix_1963", 0.93),
]

# Salvage write-off fuzzy detection tokens used by scoring.
# Includes common typo variants observed in marketplace text.
CATEGORY_S_FUZZY_PATTERN = re.compile(
    r"(?:\bcategory[\s-]*s\b|\bcat[\s-]*s\b|\bs[\s-]*category\b|\bcategorys\b|\bstructural\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Blocklist — exact normalised strings that must never be accepted as VRMs
# ---------------------------------------------------------------------------

_VRM_BLOCKLIST: Set[str] = {
    # Document abbreviations that look like prefix plates
    "V5AND", "V5LOG", "V5MOT", "V5WITH", "V5OR", "V5REG", "V5ALL", "V5DOC",
    "V5DOCS", "V5BOOK", "V5C",
    # Engine / trim codes
    "PD130", "PD115", "PD100", "PD105", "GTD", "VRS", "TDI", "TSI", "FSI", "HDI",
    "CDTI", "TDCI", "CRDI", "GTDI", "MHEV", "PHEV", "EML", "ABS", "DPF", "EGR",
    "ECU", "SRS", "ESP", "VSC", "DSG", "CVT", "DCT", "EDT", "EAT", "SCE", "TCE",
    "TFSI", "BLUEHDI", "JTDM", "SKYACTIV", "VTEC", "IVTEC",
    # Trim levels that look like prefix plates (the most common false positives)
    "ST", "SRI", "SXI", "SRI", "VXR", "GTC", "CDX", "SRi", "EXC",
    "ST17", "ST18", "ST19", "ST20", "ST21", "ST22", "ST23", "ST24",
    "SE16", "SE17", "SE18", "SE19", "SE20", "SE21", "SE22", "SE23",
    "SR16", "SR17", "SR18", "SR19", "SR20",
    # Common spec abbreviations
    "GPS", "USB", "DAB", "SAT", "NAV", "LED", "HID", "HUD", "ACC", "LDW", "AEB",
    # Generic refusals
    "UNKNOWN", "PRIVATE", "REG", "PLATE", "VRM", "REGISTRATION",
    "SOLD", "PARTS", "SPARES", "REPAIR", "BROKEN", "FULL", "GOOD",
    # Postcode fragments that look like plates
    "SW1A", "W1B", "EC1A", "SE1A", "N1C",
    # Year + suffix fragments — caught by _YEAR_ADJACENT_PATTERN below
}

# Vehicle body-type / spec words that form false suffix-format "plates"
# e.g. "VAN149K" from "1.4Tdci Van" listing, "CAB12D" from "Cab 1.2 D"
# These start with a common automotive word followed by digits+letter.
_BODY_TYPE_PREFIXES: Set[str] = {
    "VAN", "CAB", "SUV", "MPV", "SWB", "LWB", "MWB",
    "BHP", "MPG", "MPH", "KMH", "RPM",
    "MOT", "TAX", "REG", "HPI", "RAC",
    "RED", "NEW", "LOW", "TOP", "MAX", "ALL",
}

# Reject matches where the digit group is just a year (19xx or 20xx) — e.g. "HE2012"
# These look like prefix plates but are year references in listing text.
_YEAR_ADJACENT_PATTERN = re.compile(r"^[A-Z]{1,2}(19|20)\d{2}[A-Z]{0,3}$")

# Reject year-prefix patterns (e.g. "2013IN", "2015FO") — dateless_rev false positives
# where 4-digit year + 1-2 letter fragment from the next word gets concatenated.
_YEAR_PREFIX_PATTERN = re.compile(r"^(19|20)\d{2}[A-Z]{1,3}$")

# Patterns that indicate a match is a spec value, not a plate
_SPEC_PATTERN  = re.compile(r"^[0-9]{1,4}(KW|BHP|HP|PS|CC|MPG|MPH|KMH)$")
_MODEL_PATTERN = re.compile(r"^[A-Z]{1,3}[0-9]{2,4}[A-Z]?$")  # e.g. PD130, 1P1
_YEAR_PATTERN  = re.compile(r"^(19|20)\d{2}$")

# v0.9.6: Matches dateless_rev format where leading digits could be engine CC.
# Pattern: 3-4 digits followed by 1-3 letters (e.g. "1495NO", "2000GT", "1242CC")
_ENGINE_CC_PATTERN = re.compile(r"^(\d{3,4})[A-Z]{1,3}$")

# v0.9.6: Tech model codes — 2-3 letters + 4 digits, no trailing letter.
# Matches "RX6800", "GTX1080", "RTX4090" from page chrome. Never a real UK plate.
_TECH_MODEL_PATTERN = re.compile(r"^[A-Z]{2,3}\d{4}$")

# Digit-leading spec fragments: e.g. "1242CC" (caught by _SPEC_PATTERN above),
# "149VAN" from "1.4 9-speed Van", "100BHP" — reject digit-prefix + body/spec word
_DIGIT_SPEC_SUFFIXES: Set[str] = {
    "VAN", "CAB", "SUV", "MPV", "BHP", "MPG", "MPH", "RPM", "KMH",
    "DR", "DRS", "DOOR", "SPEED", "SEAT", "LITRE",
    "HDI", "TDI", "TSI", "CDI", "TDCI", "DCI",
    # v0.9.6: common short words that follow digits in automotive text
    # e.g. "1495NO" from "1495 no MOT", "1242MK" from "1242cc Mk7"
    "NO", "MK", "IN", "IS", "AT", "ON", "OR", "TO", "OF",
    "MI", "KM", "HP", "PS", "KW",
    # v0.9.6: storage/memory units from page chrome (e.g. "256GB", "512MB")
    "GB", "TB", "MB",
}

# Field names in eBay item specifics that indicate a registration field
_REG_FIELD_NAMES = (
    "reg", "registr", "plate", "vrm", "numberplate",
    "number plate", "v5", "vehicle reg", "licence", "license",
    "reg no", "reg num", "reg mark", "reg number",
)

# Label prefixes in free text that precede a plate value
_LABEL_PATTERN = re.compile(
    r"(?:"
    # Standard colon/dash delimited labels
    r"reg(?:istration)?|number\s*plate|vrm|plate|reg\s*no|reg\s*num|"
    r"vehicle\s*reg|licence\s*plate|license\s*plate|reg\s*number|reg\s*mark|"
    r"registration\s*number|registration\s*mark|v5\s*reg|number\s*plate\s*is|"
    # "reg is" / "registration is" / "plate is" / "current reg" forms
    r"reg(?:istration)?\s+is|plate\s+is|current\s+reg(?:istration)?|"
    # Sprint 3: additional Facebook / free-text variants
    # "reg plate", "private reg", "private plate", "my reg", "the reg"
    # "comes with reg", "sold with reg" — common in casual UK listings
    r"reg\s+plate|private\s+(?:reg(?:istration)?|plate)|"
    r"(?:my|the|its|with|comes\s+with|sold\s+with|has\s+its|selling\s+with)\s+reg(?:istration)?|"
    # Sprint 7: additional UK casual-listing label variants
    # "personal plate", "cherished reg/plate/number", "private number",
    # "has a reg", "own reg", "includes reg"
    r"personal\s+(?:reg(?:istration)?|plate|number)|"
    r"cherished\s+(?:reg(?:istration)?|plate|number)|"
    r"(?:has\s+a|own|includes)\s+reg(?:istration)?"
    r")"
    r"[\s:.\-/#=]+([A-Z0-9\s]{4,10})",  # window: 4–10 chars (was 4–9)
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Positive format validation (v0.9.9)
# ---------------------------------------------------------------------------
# The VRM must structurally match at least one known UK plate format.
# This catches strings like "ISDH10K" (4 letters + 2 digits + 1 letter)
# that pass all negative filters but aren't a real UK plate format.
_POSITIVE_FORMAT_RE = re.compile(
    r'^(?:'
    r'[A-Z]{2}[0-9]{2}[A-Z]{3}'    # new 2001+ e.g. AB12CDE (7 chars)
    r'|[A-Z][0-9]{1,3}[A-Z]{3}'    # prefix 1983-2001 e.g. Y123ABC (5-7 chars)
    r'|[A-Z]{3}[0-9]{1,3}[A-Z]'    # suffix 1963-1983 e.g. ABC123R (5-7 chars)
    r'|[A-Z]{1,3}[0-9]{3,4}'       # dateless fwd e.g. A1234, AB123 (4-7 chars)
    r'|[0-9]{3,4}[A-Z]{1,3}'       # dateless rev e.g. 1234A, 1234AB (4-7 chars)
    r')$'
)

# ---------------------------------------------------------------------------
# Year-plate cross-validation (v0.9.9)
# ---------------------------------------------------------------------------
# Maps the year-encoding letter to a registration year for prefix and suffix formats.

_PREFIX_YEAR: dict = {
    'A': 1983, 'B': 1984, 'C': 1985, 'D': 1986, 'E': 1987,
    'F': 1988, 'G': 1989, 'H': 1990, 'J': 1991, 'K': 1992,
    'L': 1993, 'M': 1994, 'N': 1995, 'P': 1996, 'R': 1997,
    'S': 1998, 'T': 1999, 'V': 2000, 'W': 2000, 'X': 2001, 'Y': 2001,
}
_SUFFIX_YEAR: dict = {
    'A': 1963, 'B': 1964, 'C': 1965, 'D': 1966, 'E': 1967,
    'F': 1968, 'G': 1969, 'H': 1970, 'J': 1971, 'K': 1972,
    'L': 1973, 'M': 1974, 'N': 1975, 'P': 1976, 'R': 1977,
    'S': 1978, 'T': 1979, 'U': 1980, 'V': 1981, 'W': 1982,
}

_NEW_2001_RE   = re.compile(r'^[A-Z]{2}([0-9]{2})[A-Z]{3}$')
_PREFIX_1983_RE = re.compile(r'^([A-Z])[0-9]{1,3}[A-Z]{3}$')
_SUFFIX_1963_RE = re.compile(r'^[A-Z]{3}[0-9]{1,3}([A-Z])$')


def vrm_implied_year(vrm: str) -> Optional[int]:
    """
    Extract the implied registration year from a UK plate.

    Returns an integer year, or None if the format is dateless / unrecognised.

    new 2001+:   digits 3-4 encode year half
                 01-50 → 2000+YY (Jan), 51-99 → 1950+YY (Sep)
    prefix 1983: first letter encodes year
    suffix 1963: last letter encodes year
    """
    vrm = normalise_vrm(vrm)
    m = _NEW_2001_RE.match(vrm)
    if m:
        yy = int(m.group(1))
        return (2000 + yy) if yy <= 50 else (1950 + yy)
    m = _PREFIX_1983_RE.match(vrm)
    if m:
        return _PREFIX_YEAR.get(m.group(1))
    m = _SUFFIX_1963_RE.match(vrm)
    if m:
        return _SUFFIX_YEAR.get(m.group(1))
    return None


def vrm_year_plausible(vrm: str, listing_year: Optional[int], tolerance: int = 6) -> bool:
    """
    Return True if the plate's implied registration era is compatible with the
    listing's stated year.

    Returns True (permissive) when:
      - listing_year is unknown (None or 0)
      - the plate format is dateless (vrm_implied_year returns None)
      - the mismatch is within tolerance
    UK cherished / transfer plates: an older plate may legally be assigned to a
    newer car — if listing_year >= plate_year, we do not reject (prefix/suffix
    implied year is not the vehicle's build year in that case).
    Returns False when the car appears implausibly older than the plate era by
    more than tolerance (strong signal of wrong-car plate).
    """
    if not listing_year or listing_year < 1960:
        return True  # can't cross-check
    plate_year = vrm_implied_year(vrm)
    if plate_year is None:
        return True  # dateless or unknown format — allow through
    # Cherished plate on newer car: e.g. T-reg implied ~1999 on a 2018 listing
    if listing_year >= plate_year:
        return True
    return abs(plate_year - listing_year) <= tolerance


# ---------------------------------------------------------------------------
# Normalisation and plausibility
# ---------------------------------------------------------------------------

def normalise_vrm(vrm: str) -> str:
    """Remove whitespace and uppercase a VRM string."""
    return re.sub(r"\s+", "", (vrm or "").upper())


def looks_plausible_uk_vrm(vrm: str) -> bool:
    """
    Reject strings that cannot be a valid UK plate.

    v0.9.9: Added positive format check (_POSITIVE_FORMAT_RE) — the VRM must
    structurally match at least one known UK plate format. Previously the function
    only had negative reject patterns, allowing strings like "ISDH10K" (4 letters
    + 2 digits + 1 letter) to slip through.

    Checks: positive format match, length, blocklist, must contain both digits
    and letters, must not match spec/year/model-code/year-adjacent/body-type patterns.
    """
    vrm = normalise_vrm(vrm)
    if not vrm or len(vrm) < 5 or len(vrm) > 8:
        return False
    # Positive format check — must match at least one known UK plate structure
    if not _POSITIVE_FORMAT_RE.match(vrm):
        return False
    if vrm in _VRM_BLOCKLIST:
        return False
    if not any(c.isdigit() for c in vrm):
        return False
    if not any(c.isalpha() for c in vrm):
        return False
    if _SPEC_PATTERN.match(vrm):
        return False
    if _YEAR_PATTERN.match(vrm):
        return False
    if _MODEL_PATTERN.match(vrm) and len(vrm) <= 5:
        return False
    # v0.9.6: Tech model codes (RX6800, GTX1080) — never real plates
    if _TECH_MODEL_PATTERN.match(vrm):
        return False
    # Reject e.g. "HE2012", "ST2016" — letter prefix + 4-digit year
    if _YEAR_ADJACENT_PATTERN.match(vrm):
        return False
    # Reject e.g. "2013IN", "2015FO" — year prefix + letter fragment
    if _YEAR_PREFIX_PATTERN.match(vrm):
        return False
    # Reject body-type / spec word false positives:
    # e.g. "VAN149K" from suffix_1963 pattern matching "Van 1.4 9K miles"
    # Only check prefix — suffix "VAN" could be a real plate ending
    if len(vrm) >= 5 and vrm[:3] in _BODY_TYPE_PREFIXES:
        return False
    # Reject digit-leading + spec suffix: e.g. "149VAN", "100BHP", "14TDCI"
    for sfx in _DIGIT_SPEC_SUFFIXES:
        if vrm.endswith(sfx) and vrm[: len(vrm) - len(sfx)].isdigit():
            return False
    # v0.9.6: Reject dateless_rev matches where digit portion falls in common
    # engine CC range (500-6500). Catches "1495NO", "1242CC", "2000GT" etc.
    # Real dateless plates with 3-4 digit numbers in this range do exist, but
    # the collision rate with engine specs in automotive text is too high.
    _cc_match = _ENGINE_CC_PATTERN.match(vrm)
    if _cc_match:
        cc_val = int(_cc_match.group(1))
        if 500 <= cc_val <= 6500:
            return False
    return True


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------

def _scan_patterns(
    text: str,
    patterns: List[Tuple[re.Pattern, str, float]],
) -> Optional[Tuple[str, str, float]]:
    """
    Scan text against a list of VRM patterns and return the highest-confidence
    plausible match as (vrm, pattern_name, confidence), or None.
    """
    t = text.upper()
    best: Optional[Tuple[str, str, float]] = None
    for pat, name, conf in patterns:
        for m in pat.finditer(t):
            vrm = normalise_vrm(m.group(1))
            if looks_plausible_uk_vrm(vrm):
                if best is None or conf > best[2]:
                    best = (vrm, name, conf)
    return best


# ---------------------------------------------------------------------------
# Extraction functions
# ---------------------------------------------------------------------------

def find_vrm_in_item_specifics(
    item: Dict[str, Any],
) -> Optional[Tuple[str, float]]:
    """
    Scan eBay itemSpecifics / localizedAspects for a VRM.

    Uses ALL patterns (including dateless) for fields labelled as
    registration-related; SAFE patterns otherwise.

    Returns (vrm, confidence) or None.
    """
    specs = item.get("itemSpecifics") or item.get("localizedAspects") or []
    if not isinstance(specs, list):
        return None

    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or spec.get("localizedName") or "").lower()
        vals = spec.get("values") or spec.get("value") or []
        if isinstance(vals, str):
            vals = [vals]
        if not isinstance(vals, list):
            continue

        is_reg_field = any(k in name for k in _REG_FIELD_NAMES)
        patterns = ALL_VRM_PATTERNS if is_reg_field else SAFE_VRM_PATTERNS

        for val in vals:
            result = _scan_patterns(str(val), patterns)
            if result:
                vrm, _, conf = result
                # Boost confidence to near-certain if the field was registration-labelled
                return vrm, (0.99 if is_reg_field else conf)

    return None


def find_vrm_in_description(
    item: Dict[str, Any],
    prestripped_text: str = "",
) -> Optional[Tuple[str, float]]:
    """
    Extract a VRM from the seller's description text.

    Accepts pre-stripped plain text (pass from the caller to avoid double HTML
    stripping). Falls back to stripping from the item dict if not supplied.

    v0.9.6: Body scan now uses SAFE patterns only (no dateless). Dateless
    formats (e.g. "A123", "123A") collide with engine CCs, trim codes, and
    spec fragments in unstructured text. Dateless plates are still found via
    the labelled-context scan (step 1: "Reg: ...", "Plate: ...", etc.) which
    uses ALL patterns. This eliminates the 63% false positive rate observed
    on regex_description source in v0.9.5.

    Returns (vrm, confidence) or None.
    """
    if prestripped_text:
        text = prestripped_text
    else:
        # Scan ALL available text fields — don't stop at first non-empty.
        # Some items have the plate only in shortDescription when description
        # contains boilerplate HTML, or vice versa.
        parts: List[str] = []
        for key in ("description", "shortDescription", "itemDescription",
                    "descriptionText", "fullText"):
            raw = item.get(key)
            if raw:
                stripped = _strip_html(str(raw))
                if stripped and stripped not in parts:
                    parts.append(stripped)
        if not parts:
            return None
        text = " ".join(parts)

    if not text:
        return None

    # 1) Explicit label: "Reg: AB12CDE", "Registration: ...", "Plate: ...", etc.
    #    Uses ALL patterns (including dateless) — labelled context is trusted.
    #    Scan a small trailing window so punctuation/extra tokens after the
    #    label do not hide a valid VRM token.
    for m in _LABEL_PATTERN.finditer(text):
        start = m.start(1)
        end = min(len(text), m.end(1) + 20)  # Sprint 3: 20-char trail (was 14)
        label_chunk = text[start:end]
        labelled = _scan_patterns(label_chunk, ALL_VRM_PATTERNS)
        if labelled:
            vrm, _, conf = labelled
            return vrm, max(0.96, conf)

    # 2) Body text scan — SAFE patterns only (no dateless). v0.9.6 fix.
    result = _scan_patterns(text, SAFE_VRM_PATTERNS)
    if result:
        vrm, _, conf = result
        return vrm, min(conf, 0.88)

    return None


def regex_find_vrm(text: str) -> Optional[str]:
    """
    Convenience wrapper: find the best VRM in arbitrary text using safe
    (non-dateless) patterns. Returns the normalised VRM string or None.
    """
    result = _scan_patterns(text, SAFE_VRM_PATTERNS)
    return result[0] if result else None


def extract_vrm_from_text(
    text: str,
    year: Optional[int] = None,
) -> List[Tuple[str, float]]:
    """
    Sprint 6: scan free text (title, description, or combined) for UK VRM
    candidates. Returns all unique (vrm, confidence) pairs sorted by
    confidence descending.

    Strategy (mirrors find_vrm_in_description but returns ALL candidates):
      1. Label scan — "Reg: ...", "Plate: ...", etc. → ALL patterns, conf ≥ 0.96
      2. Body text scan — SAFE patterns only, conf ≤ 0.88

    Year plausibility is NOT enforced here; callers should call
    vrm_year_plausible() to gate on listing year if required.

    Args:
        text: Plain text (HTML-stripped by caller, or we strip inline).
        year: Optional listing year for informational use by the caller.

    Returns:
        List of (vrm, confidence) tuples, highest confidence first.
        Empty list when nothing found.
    """
    if not text:
        return []

    # Inline strip in case caller passed raw HTML
    if "<" in text:
        text = _strip_html(text)

    seen: dict[str, float] = {}  # vrm -> best confidence seen

    def _accept(vrm: str, conf: float) -> None:
        if vrm and conf > seen.get(vrm, -1.0):
            seen[vrm] = conf

    # 1) Label scan — high confidence
    for m in _LABEL_PATTERN.finditer(text):
        start = m.start(1)
        end = min(len(text), m.end(1) + 20)
        labelled = _scan_patterns(text[start:end], ALL_VRM_PATTERNS)
        if labelled:
            _accept(labelled[0], max(0.96, labelled[2]))

    # 2) Body text scan — SAFE patterns only
    # _scan_patterns returns only the first match; iterate via finditer for all
    upper = text.upper()
    for pat, _fmt, base_conf in SAFE_VRM_PATTERNS:
        for m in pat.finditer(upper):
            raw = m.group(1).replace(" ", "")
            vrm = normalise_vrm(raw)
            if vrm and looks_plausible_uk_vrm(vrm):
                _accept(vrm, min(base_conf, 0.88))

    return sorted(seen.items(), key=lambda kv: -kv[1])


def contains_category_s_signal(text: str) -> bool:
    """
    Return True when text contains Category S fuzzy indicators.

    Signal terms: "Category S", "Cat S", "S category", "categorys", "structural".
    """
    return bool(CATEGORY_S_FUZZY_PATTERN.search(text or ""))


# ---------------------------------------------------------------------------
# Display / ULEZ gates
# ---------------------------------------------------------------------------

def listing_text_blob_for_vrm(listing: Any) -> str:
    """
    Concatenate title + common raw description fields for regex VRM recovery
    (report cards when pipeline vrm is empty or below display confidence).
    """
    parts: List[str] = [str(getattr(listing, "title", "") or "")]
    raw = getattr(listing, "raw", None) or {}
    if isinstance(raw, dict):
        for k in (
            "description",
            "descriptionText",
            "enriched_description_text",
            "subtitle",
            "shortDescription",
            "itemDescription",
            "conditionDescription",
        ):
            parts.append(str(raw.get(k) or ""))
    return "\n".join(parts)


def resolve_vrm_for_report(listing: Any) -> Optional[Tuple[str, str, float]]:
    """
    Best VRM to show on the HTML report: pipeline fields first, then
    title/description scan (Motors often has the plate in copy but not in vrm).
    Returns (vrm, source_label, confidence) or None.
    """
    v = (getattr(listing, "vrm", "") or "").strip()
    c = float(getattr(listing, "vrm_confidence", 0.0) or 0.0)
    src = (getattr(listing, "vrm_source", "") or "").strip()
    if v and looks_plausible_uk_vrm(v):
        return (v, src or "pipeline", c)
    blob = listing_text_blob_for_vrm(listing)
    cand = extract_vrm_from_text(blob)
    if cand:
        best_v, best_c = cand[0]
        if looks_plausible_uk_vrm(best_v):
            return (best_v, "title/description", best_c)
    return None


def is_vrm_displayable(vrm: str, confidence: float) -> bool:
    """
    True only if the VRM is plausible AND confidence meets the display threshold.
    Below MIN_VRM_DISPLAY_CONFIDENCE the report shows "no VRM" instead.
    """
    if not vrm or not looks_plausible_uk_vrm(vrm):
        return False
    return confidence >= MIN_VRM_DISPLAY_CONFIDENCE


def is_ulez_compliant(year: Optional[int], fuel: str) -> Optional[bool]:
    """
    Infer ULEZ compliance from registration year and fuel type.
    Returns None when insufficient data to determine.

    TfL thresholds (as of 2024):
      Petrol / hybrid: first registered 2006 or later
      Diesel:          first registered 2015 or later
    """
    if not year:
        return None
    fuel = (fuel or "").lower()
    if "petrol" in fuel or "hybrid" in fuel:
        return year >= 2006
    if "diesel" in fuel:
        return year >= 2015
    # Unknown fuel type — assume compliant if recent enough
    if year >= 2015:
        return True
    return None


# Explicit ULEZ non-compliance phrases found in listing titles/descriptions
_ULEZ_NON_COMPLIANT_PHRASES: tuple = (
    "non ulez", "non-ulez", "not ulez", "ulez non", "ulez fail",
    "fails ulez", "failed ulez", "not ulez compliant", "ulez non-compliant",
    "ulez noncompliant", "not compliant with ulez", "ulez charge",
    "will incur ulez", "subject to ulez", "ulez surcharge",
    "ulez exempt zone",  # "exempt zone" means outside ULEZ = car fails if driven inside
)

# Explicit ULEZ compliance phrases
_ULEZ_COMPLIANT_PHRASES: tuple = (
    "ulez free", "ulez-free", "ulez compliant", "ulez-compliant",
    "passes ulez", "ulez pass", "ulez exempt", "ulez ok",
    "meets ulez", "ulez friendly", "ulez complies",
    "compliant with ulez", "within ulez", "ulez approved",
)


def detect_ulez_from_text(text: str) -> Optional[bool]:
    """
    Scan listing title/description for explicit ULEZ compliance mentions.

    Returns True (compliant), False (non-compliant), or None (not mentioned).
    Takes priority over year-based inference when a phrase is found.

    Sprint 15: fixes ULEZ not flagged despite appearing in listing title.
    """
    t = (text or "").lower()
    # Check non-compliant first (stronger signal — seller disclosing a problem)
    for phrase in _ULEZ_NON_COMPLIANT_PHRASES:
        if phrase in t:
            return False
    for phrase in _ULEZ_COMPLIANT_PHRASES:
        if phrase in t:
            return True
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Strip HTML tags, unescape entities, and collapse whitespace."""
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()

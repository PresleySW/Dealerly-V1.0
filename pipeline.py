"""
dealerly/pipeline.py
====================
Seven-phase pipeline orchestration. No user interaction — all prompts in cli.py.

v1.0.0-rc.1 changes (Sprint 1):
  - Priority enrichment pass (Phase 3): eBay candidates sorted by descending prelim
    expected_profit before ANPR/DVLA loop. Top-N (DEALERLY_PRIORITY_ENRICH_N, default 5)
    run first, bypassing idx-slice caps, so highest-value rows are never credit-starved.
  - Phase 4.5 sort preserved (BUY/OFFER priority + profit desc).

UK stats map:
  - After Phase 6 price observations: persist pipeline_runs, write UK_STATS_MAP_HTML,
    embed 3D board in the HTML report (see dealerly.geo, dealerly.report).
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time, webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from dealerly.autotrader import AutoTraderComps
from dealerly.config import (
    DB_PATH, DEAL_LOG_PATH, DEFAULT_COMPS_LOOKUP_LIMIT, DEFAULT_TOP_N,
    ENABLED_PLATFORMS, PAGE_SIZE, QUERY_PRESETS, REPORTS_DIR, REQUEST_SLEEP_S,
    Config, anpr_max_images, anpr_min_profit_gbp, anpr_profit_weight, priority_enrich_n,
    dealerly_runtime_banner, obsidian_vault_path, fb_max_listings,
    scale_vehicle_queries_for_capital,
)
from dealerly.db import (
    db_connect,
    get_item_vrm,
    get_listing_seen_info,
    get_verified_vehicle,
    init_db,
    insert_pipeline_run,
    list_pipeline_runs,
    upsert_item_vrm,
    watchlist_add,
)
from dealerly.ebay import (
    comps_query_from_key, ebay_app_token, ebay_get_item, ebay_search,
    guess_make_model, hard_price_filter, is_strict_whole_car,
    merge_dedupe, normalise_ebay_items, rank_images_for_display, resolve_ebay_env,
)
from dealerly.ingestion import (
    EbayIngestionAdapter,
)
# Sprint 3: platform adapters (imported here; gracefully unavailable if deps missing)
from dealerly.facebook import FacebookAdapter
from dealerly.motors import MotorsAdapter
from dealerly.pistonheads import PistonHeadsAdapter
from dealerly.models import DealInput, DealOutput, Listing
from dealerly.mot import build_mot_provider
from dealerly.offers import (
    _ai_request,
    claude_api_key,
    generate_offer_message,
    openai_api_key,
)
from dealerly.report import (
    append_deal_log,
    generate_html_report,
    print_report,
    write_loading_screen,
    write_uk_stats_map_page,
)
from dealerly.geo import resolve_postcode_coords
from dealerly.scoring import score_listings
from dealerly.utils import now_utc_iso, round_to_nearest
from dealerly.vrm import (
    SAFE_VRM_PATTERNS, _scan_patterns, _strip_html,
    find_vrm_in_description, find_vrm_in_item_specifics,
    is_ulez_compliant, is_vrm_displayable, looks_plausible_uk_vrm,
    vrm_year_plausible, extract_vrm_from_text, detect_ulez_from_text,
)
from dealerly.vision import extract_vrm_via_vision, is_vision_available, rank_images_for_anpr
from dealerly.ebay import collect_image_urls, mileage_from_item, year_from_item

# New v0.9.0 modules
from dealerly.dvla import dvla_vrm_from_listing, is_dvla_available
from dealerly.analytics import (
    record_price_observations, compute_analytics_for_rows,
)
from dealerly.workflow import (
    auto_create_leads,
    backfill_obsidian_graph_from_vrm_scans,
    export_buy_leads_to_obsidian,
    export_vrm_scans_to_obsidian,
)
from dealerly.sheets import export_to_sheets, is_sheets_available
from dealerly.obsidian_brain import ObsidianBrain, load_obsidian_brain


def _antigravity_cli_path() -> str:
    """Best-effort Antigravity CLI path."""
    p_env = os.environ.get("ANTIGRAVITY_CLI_PATH", "").strip()
    if p_env:
        return p_env
    return "D:/RHUL/Dealerly/Antigravity/bin/antigravity.cmd"


def _should_use_antigravity() -> bool:
    """Antigravity is opt-in. Set DEALERLY_USE_ANTIGRAVITY=1 to enable."""
    enable = os.environ.get("DEALERLY_USE_ANTIGRAVITY", "").strip().lower()
    if enable not in {"1", "true", "yes"}:
        return False
    return os.path.exists(_antigravity_cli_path())


def _open_path_preferred(path: str, *, line: int = 1) -> bool:
    """Open file path via Antigravity if available, else default browser."""
    if _should_use_antigravity():
        try:
            subprocess.Popen(
                [_antigravity_cli_path(), "-r", "-g", f"{path}:{line}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            pass
    try:
        webbrowser.open(f"file://{path}")
        return True
    except Exception:
        return False


def _open_postrun_dashboard(report_path) -> None:
    """
    Open post-run .md artifacts in preferred editor/viewer.

    report_path is accepted for backwards compatibility but ignored (caller
    opens the HTML report directly to avoid opening two browser windows).
    Disable entirely with DEALERLY_DISABLE_POSTRUN_DASHBOARD=1.
    """
    disable = os.environ.get("DEALERLY_DISABLE_POSTRUN_DASHBOARD", "").strip().lower()
    if disable in {"1", "true", "yes"}:
        return
    paths = [
        "prompts/PIPELINE_REPORT.md",
        "prompts/RELEVANT.md",
        "prompts/NEXT_VERSION.md",
    ]
    opened: set[str] = set()
    for p in paths:
        try:
            abs_p = str(Path(p).resolve())
        except Exception:
            abs_p = p
        if abs_p in opened:
            continue
        _open_path_preferred(abs_p, line=1)
        opened.add(abs_p)


def _phase_timing_breakdown(debug_log: Dict[str, Any]) -> Dict[str, float]:
    """Convert cumulative phase elapsed markers into per-phase timing deltas."""
    phase_keys = ["phase1", "phase2", "phase3", "phase4"]
    out: Dict[str, float] = {}
    prev_elapsed = 0.0
    for key in phase_keys:
        phase = debug_log.get("phases", {}).get(key) or {}
        elapsed = float(phase.get("elapsed_s", prev_elapsed) or prev_elapsed)
        delta = max(0.0, elapsed - prev_elapsed)
        out[key] = round(delta, 1)
        prev_elapsed = max(prev_elapsed, elapsed)
    return out


def _build_heuristic_next_steps(
    *,
    phase_timing: Dict[str, float],
    platform_results: Dict[str, int],
    vrm_found_pool: int,
    candidate_limit: int,
    mot_verified: int,
    buy_n: int,
    offer_n: int,
    obsidian_cache_hits: int,
    anpr_skips: int,
) -> List[str]:
    """
    Build deterministic next-version directives from the latest run metrics.

    This runs on every pipeline execution so the markdown backlog is always
    refreshed even when no external AI backend is available.
    """
    steps: List[str] = []
    blocked = [p for p, n in platform_results.items() if n == 0]
    if blocked:
        steps.append(
            f"Stabilize blocked sources first ({', '.join(sorted(blocked))} returned 0 listings) "
            "before new feature work."
        )
    if phase_timing.get("phase3", 0.0) >= 150.0:
        steps.append(
            "Reduce Phase 3 enrichment cost: prioritize top profit rows, tighten ANPR/page-scrape gates, "
            "and cap late-round enrichments by elapsed time."
        )
    if phase_timing.get("phase4", 0.0) >= 95.0:
        steps.append(
            "Cut Phase 4 score latency: lower candidate expansion churn and avoid re-score passes unless "
            "new VRM/MOT signal is material."
        )
    vrm_ratio = (vrm_found_pool / max(candidate_limit, 1))
    if vrm_ratio < 0.60:
        steps.append(
            "Improve VRM hit-rate in the scored pool via stronger labelled-reg extraction and earlier "
            "cache reuse before ANPR calls."
        )
    mot_ratio = (mot_verified / max(candidate_limit, 1))
    if mot_ratio < 0.45:
        steps.append(
            "Increase DVSA-verified coverage in top candidates so BUY/OFFER confidence is based on verified MOT history."
        )
    if obsidian_cache_hits == 0 and vrm_found_pool > 0:
        steps.append(
            "Raise Obsidian cache utility by expanding item-id alias normalization and write-through consistency."
        )
    if (buy_n + offer_n) <= 6:
        steps.append(
            "Increase actionable yield by tuning near-miss conversion and Cat-S risk separation "
            "to keep true opportunities visible."
        )
    if anpr_skips <= 1:
        steps.append(
            "Increase ANPR credit efficiency with stronger early exits and confidence thresholds on low-upside listings."
        )
    if not steps:
        steps.append(
            "Maintain current thresholds; focus on incremental extraction quality and runtime predictability."
        )
    return steps[:6]


def _ai_refine_next_steps(heuristic_steps: List[str]) -> List[str]:
    """
    Optionally rephrase heuristics with AI when keys are available.

    Falls back to deterministic steps if the AI request fails or is unavailable.
    """
    if not openai_api_key() and not claude_api_key():
        return heuristic_steps
    prompt = (
        "Rewrite the following software sprint directives into 4-6 concise, "
        "implementation-ready bullet points for the next version of Dealerly. "
        "Keep each bullet under 24 words and outcome-focused.\n\n"
        + "\n".join(f"- {x}" for x in heuristic_steps)
    )
    text, _backend = _ai_request(
        [{"role": "user", "content": prompt}],
        system=(
            "You are a senior engineering planner. Output only plain bullet lines "
            "starting with '- '. No intro text."
        ),
        max_tokens=220,
        preferred_backend=os.environ.get("DEALERLY_AI_BACKEND", "openai"),
    )
    if not text:
        return heuristic_steps
    out = []
    for line in str(text).splitlines():
        s = line.strip()
        if not s:
            continue
        if not s.startswith("- "):
            s = "- " + s.lstrip("- ").strip()
        out.append(s[2:].strip())
    return out[:6] or heuristic_steps


def _render_next_version_md(
    *,
    phase_timing: Dict[str, float],
    platform_results: Dict[str, int],
    candidate_limit: int,
    vrm_found_pool: int,
    mot_verified: int,
    decisions: Dict[str, int],
    next_steps: List[str],
) -> str:
    """Render the persistent next-version directive file."""
    platform_line = ", ".join(
        f"{p}: {n}" for p, n in sorted(platform_results.items())
    ) or "none"
    timing_line = ", ".join(
        f"{k} {v:.1f}s" for k, v in phase_timing.items()
    ) or "unavailable"
    return (
        f"# Dealerly Next Version Directives\n\n"
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"## Run Snapshot\n"
        f"- Platforms: {platform_line}\n"
        f"- Timings: {timing_line}\n"
        f"- Candidate pool: {candidate_limit}\n"
        f"- VRMs found in pool: {vrm_found_pool}\n"
        f"- DVSA verified in pool: {mot_verified}\n"
        f"- Decisions: BUY {decisions.get('buy', 0)}, OFFER {decisions.get('offer', 0)}, "
        f"PASS {decisions.get('pass', 0)}, AVOID {decisions.get('avoid_shock', 0)}\n\n"
        f"## Build Directives For Next Version\n"
        + "\n".join(f"- {s}" for s in next_steps)
        + "\n"
    )


# -------------------------------------------------------------------
# ULEZ inference helper (Sprint 15: text takes priority over year)
# -------------------------------------------------------------------

def _resolve_ulez(listing) -> None:
    """
    Set listing.ulez_compliant via year+fuel, then override if listing
    title/description contains an explicit ULEZ statement.
    Text detection takes priority: if a seller writes "Non-ULEZ" or
    "ULEZ free" we trust that over the year-based inference.
    """
    listing.ulez_compliant = is_ulez_compliant(listing.year, listing.fuel_type)
    text_blob = " ".join([
        listing.title or "",
        str((listing.raw or {}).get("shortDescription", "") or ""),
        str((listing.raw or {}).get("description", "") or ""),
        str((listing.raw or {}).get("itemDescription", "") or ""),
        str((listing.raw or {}).get("subtitle", "") or ""),
    ])
    text_result = detect_ulez_from_text(text_blob)
    if text_result is not None:
        listing.ulez_compliant = text_result


# -------------------------------------------------------------------
# Mileage when VRM came from Obsidian / item_vrm cache (short-circuit paths)
# -------------------------------------------------------------------


def _parse_mileage_from_text_blob(text: str) -> Optional[int]:
    """Extract odometer miles from free text (title, description, raw fields)."""
    t = (text or "").lower().replace(",", "")
    if not t:
        return None
    m = re.search(r"\b(\d{1,3})(?:\.(\d))?\s*k\s*(?:miles?|mi)?\b", t)
    if m:
        whole = int(m.group(1))
        frac = int(m.group(2) or "0")
        v = whole * 1000 + frac * 100
        if 1_000 <= v <= 400_000:
            return v
    m = re.search(r"\b(\d{3,6})\s*(?:miles?|mi)\b", t)
    if m:
        v = int(m.group(1))
        if 1_000 <= v <= 400_000:
            return v
    return None


def _fill_mileage_non_ebay_from_raw(listing: Listing) -> None:
    if listing.mileage and listing.mileage > 0:
        return
    parts = [str(listing.title or "")]
    raw = listing.raw or {}
    if isinstance(raw, dict):
        parts.extend(str(v) for v in raw.values() if isinstance(v, str))
    m = _parse_mileage_from_text_blob(" ".join(parts))
    if m:
        listing.mileage = m


def _fill_mileage_year_from_ebay_item(listing: Listing, item: dict) -> None:
    """Fill mileage/year from eBay Browse API item when cache skipped full enrich."""
    if not item:
        return
    item_mileage = mileage_from_item(item)
    if item_mileage and (not listing.mileage or listing.mileage == 0):
        listing.mileage = item_mileage
    if not listing.mileage or listing.mileage == 0:
        tm = _parse_mileage_from_text_blob(
            " ".join(
                [
                    str(item.get("title", "") or ""),
                    str(item.get("shortDescription", "") or ""),
                    str(item.get("description", "") or ""),
                    str(listing.title or ""),
                ]
            )
        )
        if tm:
            listing.mileage = tm
    _cy = datetime.now().year
    item_year = year_from_item(item)
    if item_year and item_year > _cy:
        item_year = None
    if item_year and not listing.year:
        listing.year = item_year
    if listing.year and listing.year > _cy:
        listing.year = None


def _fill_mileage_when_vrm_cached(
    listing: Listing,
    ebay_env: str,
    token: str,
) -> None:
    """
    Obsidian / item_vrm paths return before ebay_get_item; still pull mileage
    so priority-pass logs do not show '?k' when eBay item specifics have miles.
    """
    if listing.mileage and listing.mileage > 0:
        return
    if (listing.platform or "").lower() == "ebay":
        _item = ebay_get_item(env=ebay_env, token=token, item_id=listing.item_id)
        if _item:
            _fill_mileage_year_from_ebay_item(listing, _item)
    else:
        _fill_mileage_non_ebay_from_raw(listing)


# -------------------------------------------------------------------
# Shared enrichment logic (used by Phase 3 and Phase 4.5)
# -------------------------------------------------------------------

def _anpr_non_ebay_from_listing(
    listing: Listing,
    conn,
    *,
    budget_counters: Optional[Dict[str, int]] = None,
) -> bool:
    """
    Run Plate Recognizer on hero + gallery URLs for Facebook/Motors listings.
    eBay uses `_enrich_single_listing` + Browse API image payloads; other
    platforms only had regex/text VRM until this pass (Sprint 11).
    """
    if listing.vrm or not is_vision_available():
        return False
    urls: List[str] = []
    u = (listing.first_image_url or "").strip()
    if u:
        urls.append(u)
    for x in (listing.extra_image_urls or "").split(","):
        x = x.strip()
        if x and x not in urls:
            urls.append(x)
    if not urls:
        return False
    urls = rank_images_for_anpr(urls)
    _mimg = anpr_max_images()
    if budget_counters is not None:
        budget_counters["anpr_calls"] = budget_counters.get("anpr_calls", 0) + 1
    r = extract_vrm_via_vision(listing.item_id, urls, conn, max_images=_mimg)
    if not r:
        return False
    vrm, conf = r[0], r[1]
    if not vrm_year_plausible(vrm, listing.year, tolerance=8):
        return False
    listing.vrm = vrm
    listing.vrm_confidence = conf
    listing.vrm_source = "plate_recognizer"
    listing.vrm_evidence = "listing photos (ANPR, non-eBay)"
    _resolve_ulez(listing)
    try:
        upsert_item_vrm(
            conn, listing.item_id, listing.vrm,
            listing.vrm_source or "unknown", listing.vrm_confidence,
        )
    except Exception:
        pass
    return True


def _enrich_single_listing(
    listing: Listing,
    *,
    ebay_env: str,
    token: str,
    dvla_enabled: bool,
    dvla_allowed: bool,
    anpr_enabled: bool,
    allow_page_scrape: bool,
    conn,
    obsidian_brain: Optional[ObsidianBrain] = None,
    budget_counters: Optional[Dict[str, int]] = None,
) -> List[str]:
    """
    Run the full VRM enrichment cascade on a single eBay listing.
    Returns a list of step tags for logging (e.g. ["specifics-", "desc+", ...]).
    Mutates listing in place (vrm, vrm_confidence, vrm_source, etc.).
    """
    if obsidian_brain:
        hint = obsidian_brain.get_vrm_hint(listing.item_id)
        if hint:
            vrm_h, src_h, conf_h = hint
            if looks_plausible_uk_vrm(vrm_h):
                listing.vrm = vrm_h
                listing.vrm_confidence = max(listing.vrm_confidence, conf_h or 0.80)
                listing.vrm_source = (src_h or "obsidian_scan") + "_cached"
                listing.vrm_evidence = "obsidian_vault_vrm_scans"
                _resolve_ulez(listing)
                _fill_mileage_when_vrm_cached(listing, ebay_env, token)
                return ["obsidian-cache+"]

    # Sprint 7: item_vrm DB cache checked BEFORE ebay_get_item() — avoids spending
    # an eBay item API call on listings already verified in a previous run (14-day TTL).
    # Previously this check happened after the fetch (wasting one API call per hit).
    _pre_cached = get_item_vrm(conn, listing.item_id, max_age_days=14)
    if _pre_cached:
        vrm_c, src_c, conf_c = _pre_cached
        listing.vrm, listing.vrm_confidence = vrm_c, conf_c
        listing.vrm_source   = src_c + "_cached"
        listing.vrm_evidence = "item_vrm DB cache (pre-fetch)"
        _resolve_ulez(listing)
        if budget_counters is not None:
            budget_counters["item_vrm_cache_hits"] = budget_counters.get("item_vrm_cache_hits", 0) + 1
            budget_counters["anpr_skipped_cache"] = budget_counters.get("anpr_skipped_cache", 0) + 1
        _fill_mileage_when_vrm_cached(listing, ebay_env, token)
        return ["db-cache+"]

    item = ebay_get_item(env=ebay_env, token=token, item_id=listing.item_id)
    if not item:
        return ["fetch-fail"]

    raw_desc  = (item.get("description")
                 or item.get("shortDescription")
                 or item.get("itemDescription") or "")
    desc_text = _strip_html(raw_desc) if raw_desc else ""
    # Persist enriched text fields so downstream scoring/risk checks can inspect
    # full seller wording (e.g. Cat S / structural write-off mentions).
    item["enriched_description_text"] = desc_text
    item["enriched_text_blob"] = " ".join(
        [
            str(item.get("title", "") or ""),
            str(item.get("shortDescription", "") or ""),
            str(item.get("description", "") or ""),
            str(item.get("itemDescription", "") or ""),
            str(item.get("subtitle", "") or ""),
            str(item.get("conditionDescription", "") or ""),
        ]
    ).strip()
    steps: List[str] = []

    # Enrich mileage + year from item specifics (done first so year-check works)
    item_mileage = mileage_from_item(item)
    if item_mileage and (not listing.mileage or listing.mileage == 0):
        listing.mileage = item_mileage
    if not listing.mileage:
        text_mileage = _parse_mileage_from_text_blob(
            " ".join(
                [
                    str(item.get("title", "") or ""),
                    str(item.get("shortDescription", "") or ""),
                    str(item.get("description", "") or ""),
                    str(listing.title or ""),
                ]
            )
        )
        if text_mileage:
            listing.mileage = text_mileage
    _current_year = datetime.now().year
    item_year = year_from_item(item)
    # Guard: reject future years (e.g. "MOT: March 2027" extracted as listing year)
    if item_year and item_year > _current_year:
        item_year = None
    if item_year and not listing.year:
        listing.year = item_year
    # Cap any existing listing.year that slipped through as a future year
    if listing.year and listing.year > _current_year:
        listing.year = None

    # Sprint 2: persist hero image URL for report thumbnails — done here so
    # ALL enriched listings get a thumbnail, not only those that reach ANPR.
    # Also replace known placeholder/no-image URLs with best available enriched
    # image candidate when possible.
    _img_cur = (listing.first_image_url or "").strip().lower()
    _img_placeholder = (
        (not _img_cur)
        or ("noimage" in _img_cur)
        or ("placeholder" in _img_cur)
        or ("/no-image" in _img_cur)
    )
    # Sprint 5: collect all display images once; use for hero update + gallery carousel
    _all_imgs = collect_image_urls(item, limit=6, rank_fn=rank_images_for_display)
    _all_imgs = [
        u for u in (_all_imgs or [])
        if u and "noimage" not in u.lower() and "placeholder" not in u.lower()
    ]
    if _img_placeholder and _all_imgs:
        listing.first_image_url = _all_imgs[0]
    # Populate gallery extras (images beyond the hero, up to 4)
    _gallery_extras = [u for u in _all_imgs if u != listing.first_image_url][:4]
    if _gallery_extras:
        listing.extra_image_urls = ",".join(_gallery_extras)

    def _year_ok(vrm: str) -> bool:
        """True if VRM year matches listing year (or year unknown / dateless plate)."""
        return vrm_year_plausible(vrm, listing.year, tolerance=8)

    def _set_vrm(vrm: str, conf: float, source: str, evidence: str) -> bool:
        """
        Apply a VRM candidate. Returns True if accepted (year plausible),
        False if rejected due to year mismatch (logs warning).
        """
        if not _year_ok(vrm):
            from dealerly.vrm import vrm_implied_year
            plate_yr = vrm_implied_year(vrm)
            steps.append(f"yr-mismatch({vrm}:{plate_yr}!={listing.year})")
            return False
        listing.vrm, listing.vrm_confidence = vrm, conf
        listing.vrm_source, listing.vrm_evidence = source, evidence
        return True

    # Step 1: item specifics
    r1 = find_vrm_in_item_specifics(item)
    if r1 and _set_vrm(r1[0], r1[1], "regex_item_specifics", "item specifics"):
        steps.append("specifics+")
    else:
        if r1:
            pass  # year mismatch already logged by _set_vrm
        else:
            steps.append("specifics-")

    # Step 2: seller description
    if not listing.vrm:
        r2 = find_vrm_in_description(item, prestripped_text=desc_text)
        if r2 and _set_vrm(r2[0], r2[1], "regex_description", "seller description"):
            steps.append("desc+")
        else:
            steps.append("desc-")

    # Step 3: title scan
    if not listing.vrm:
        r3 = _scan_patterns(listing.title, SAFE_VRM_PATTERNS)
        if r3 and _set_vrm(r3[0], r3[2], "regex_title", "listing title"):
            steps.append("title+")
        else:
            steps.append("title-")

    # Step 4: HTML page scrape (optional - slower fallback)
    if not listing.vrm and allow_page_scrape and listing.url:
        try:
            import requests as _req
            from dealerly.config import USER_AGENT
            r = _req.get(
                listing.url,
                headers={"User-Agent": USER_AGENT,
                         "Accept": "text/html,application/xhtml+xml"},
                timeout=10,
            )
            if r.status_code == 200:
                page_text = _strip_html(r.text)
                r4 = find_vrm_in_description(
                    {}, prestripped_text=page_text[:15000])
                if r4:
                    conf4 = min(r4[1], 0.85)
                    if _set_vrm(r4[0], conf4, "regex_page_scrape", "listing page HTML"):
                        listing.vrm_confidence = conf4
                        steps.append("scrape+")
                    else:
                        steps.append("scrape-")
                else:
                    steps.append("scrape-")
        except Exception:
            steps.append("scrape-")

    # Step 4.5: Plate Recognizer ANPR
    # Sprint 1: images ranked by rank_images_for_anpr before slicing to limit.
    # v0.9.9: Skip ANPR if regex VRM is cached in verified_vehicles.
    # v1.0 Sprint 3: Also skip ANPR when text extraction already produced a
    # high-confidence VRM — labelled fields ("Reg: AB12CDE") and clean
    # item-specifics are as reliable as ANPR at this confidence level.
    # Sprint 12: threshold lowered 0.92 → 0.88 (text-prepass quality improved).
    # Obsidian-cache and item_vrm-cache paths never reach this branch (they
    # returned early above), so confidence check is safe here.
    _anpr_skip_confident = (
        bool(listing.vrm)
        and (listing.vrm_confidence or 0.0) >= 0.88
    )
    _anpr_skip_verified = (
        bool(listing.vrm)
        and not _anpr_skip_confident
        and bool(get_verified_vehicle(conn, listing.vrm))
    )
    if _anpr_skip_confident or _anpr_skip_verified:
        _skip_tag = "vrm-confident" if _anpr_skip_confident else "vrm-verified"
        steps.append(f"anpr-skip({_skip_tag})")
        if budget_counters is not None:
            budget_counters["anpr_skipped_verified"] = budget_counters.get("anpr_skipped_verified", 0) + 1
    elif not listing.vrm and anpr_enabled:
        _mimg = anpr_max_images()
        image_urls = collect_image_urls(item, limit=_mimg, rank_fn=rank_images_for_anpr)
        if image_urls:
            if budget_counters is not None:
                budget_counters["anpr_calls"] = budget_counters.get("anpr_calls", 0) + 1
            r5 = extract_vrm_via_vision(
                listing.item_id, image_urls, conn, max_images=_mimg
            )
            if r5:
                if _set_vrm(r5[0], r5[1], "plate_recognizer", "listing photos (ANPR)"):
                    steps.append("anpr+")
                else:
                    steps.append("anpr-yr")  # ANPR found something but wrong year
            else:
                steps.append("anpr-")

    # Step 4.7: DVLA vehicle enquiry
    if not listing.vrm and dvla_enabled and dvla_allowed:
        if budget_counters is not None:
            budget_counters["dvla_calls"] = budget_counters.get("dvla_calls", 0) + 1
        dvla_result = dvla_vrm_from_listing(listing, conn)
        if dvla_result:
            listing.vrm, listing.vrm_confidence = dvla_result
            listing.vrm_source   = "dvla_enquiry"
            listing.vrm_evidence = "DVLA vehicle enquiry"
            steps.append("dvla+")
        else:
            steps.append("dvla-")
    elif not listing.vrm and dvla_enabled and not dvla_allowed:
        steps.append("dvla-skip(top-slice)")
        if budget_counters is not None:
            budget_counters["dvla_skipped_top_slice"] = budget_counters.get("dvla_skipped_top_slice", 0) + 1

    # FINAL GATE: DVLA cross-validation for VRMs found with confidence < 0.92
    if listing.vrm and listing.vrm_confidence < 0.92 and dvla_enabled and dvla_allowed:
        if budget_counters is not None:
            budget_counters["dvla_validation_calls"] = budget_counters.get("dvla_validation_calls", 0) + 1
        dvla_result = dvla_vrm_from_listing(listing, conn)
        if dvla_result:
            listing.vrm, listing.vrm_confidence = dvla_result
            listing.vrm_source += "+dvla_validated"
            steps.append("dvla-val+")
        else:
            # DVLA couldn't validate — penalise confidence
            listing.vrm_confidence = min(listing.vrm_confidence, 0.75)
            steps.append("dvla-val-")

    # v0.9.9: Persist VRM to item_vrm DB so next run skips ANPR for this item
    # Sprint 15: _resolve_ulez also applies text-based override
    if listing.vrm:
        _resolve_ulez(listing)
        try:
            upsert_item_vrm(
                conn, listing.item_id, listing.vrm,
                listing.vrm_source or "unknown", listing.vrm_confidence,
            )
        except Exception:
            pass  # non-critical — don't break enrichment if DB write fails

    listing.raw = item
    return steps


# -------------------------------------------------------------------
# Sprint 3: adapter factory
# -------------------------------------------------------------------

def _build_adapter_list(
    input_mode: str,
    enabled_platforms: list,
    fetch_paged_fn,
) -> list:
    """
    Build the ordered list of ingestion adapters to run in Phase 1.

    Selection logic:
      - "ebay"     -> [EbayIngestionAdapter]
      - "facebook" -> [FacebookAdapter]
      - "motors"   -> [MotorsAdapter]
      - "all"      -> all adapters in ENABLED_PLATFORMS order
      - "multi"    -> same as "all"
      - anything else that is a known platform slug -> that single adapter
      - otherwise  -> adapters for each platform in ENABLED_PLATFORMS

    The eBay adapter is always given the ``fetch_paged_fn`` closure so it
    can reuse the authenticated eBay session from the outer run() scope.

    Args:
        input_mode:       cfg.input_mode string from the CLI.
        enabled_platforms: ENABLED_PLATFORMS list from config.py.
        fetch_paged_fn:   Callable[[str], List[Listing]] — the fetch_paged
                          closure from run(), already bound to auth token.

    Returns:
        Ordered list of BaseIngestionAdapter instances (may be empty).
    """
    _platform_map = {
        "ebay":         lambda: EbayIngestionAdapter(fetch_paged_fn),
        "facebook":     lambda: FacebookAdapter(),
        "motors":       lambda: MotorsAdapter(),
        "pistonheads":  lambda: PistonHeadsAdapter(),
    }

    def _make(platform: str):
        factory = _platform_map.get(platform.lower())
        return factory() if factory else None

    # Explicit single-platform modes
    if input_mode in _platform_map:
        adapter = _make(input_mode)
        return [adapter] if adapter else []

    # "all" or "multi" — run every enabled platform
    if input_mode in ("all", "multi"):
        platforms = enabled_platforms
    else:
        # Default: use ENABLED_PLATFORMS
        platforms = enabled_platforms

    adapters = []
    for platform in platforms:
        a = _make(platform)
        if a is not None:
            adapters.append(a)
    return adapters


def _build_budget_basket(rows: list, capital: float) -> tuple[list, float, float]:
    """
    Pick an affordable portfolio of BUY/OFFER listings under available capital.

    Objective:
      1) maximize total expected profit
      2) then maximize number of cars
      3) then maximize capital utilization (higher spend)
    """
    if capital <= 0:
        return [], 0.0, 0.0
    cands = [
        (l, d, o) for (l, d, o) in rows
        if o.decision in ("BUY", "OFFER") and o.expected_profit > 0 and l.price_gbp > 0
    ]
    n = len(cands)
    if n == 0:
        return [], 0.0, 0.0

    best_mask = 0
    best_profit = -1.0
    best_spend = 0.0
    best_count = 0

    for mask in range(1, 1 << n):
        spend = 0.0
        profit = 0.0
        count = 0
        feasible = True
        for i in range(n):
            if (mask >> i) & 1:
                l, _, o = cands[i]
                spend += float(l.price_gbp)
                if spend > capital:
                    feasible = False
                    break
                profit += float(o.expected_profit)
                count += 1
        if not feasible:
            continue
        if (
            profit > best_profit
            or (profit == best_profit and count > best_count)
            or (profit == best_profit and count == best_count and spend > best_spend)
        ):
            best_mask = mask
            best_profit = profit
            best_spend = spend
            best_count = count

    if best_mask == 0:
        return [], 0.0, 0.0

    selected = [cands[i] for i in range(n) if (best_mask >> i) & 1]
    return selected, best_spend, best_profit


def _select_phase2_input(listings: list[Listing], cap: int) -> list[Listing]:
    """
    Select Phase 2 candidates with lightweight cross-platform balancing.
    """
    if not listings or cap <= 0:
        return []
    cap = min(cap, len(listings))
    selected: list[Listing] = []
    selected_ids: set[str] = set()
    non_ebay = [l for l in listings if (l.platform or "").lower() not in {"", "ebay"}]
    if non_ebay:
        reserve = min(max(0, cap // 4), len(non_ebay))
        by_platform: dict[str, list[Listing]] = {}
        for l in non_ebay:
            by_platform.setdefault((l.platform or "unknown").lower(), []).append(l)
        while reserve > 0:
            progressed = False
            for platform in sorted(by_platform):
                if reserve <= 0:
                    break
                queue = by_platform.get(platform) or []
                while queue and queue[0].item_id in selected_ids:
                    queue.pop(0)
                if not queue:
                    continue
                pick = queue.pop(0)
                selected.append(pick)
                selected_ids.add(pick.item_id)
                reserve -= 1
                progressed = True
            if not progressed:
                break
    for l in listings:
        if len(selected) >= cap:
            break
        if l.item_id in selected_ids:
            continue
        selected.append(l)
        selected_ids.add(l.item_id)
    return selected


def _select_display_rows(
    rows: list[tuple[Listing, DealInput, DealOutput]],
    *,
    limit: int,
    near_miss_band: float,
) -> list[tuple[Listing, DealInput, DealOutput]]:
    """
    Prioritize high-quality actionable rows for main display.

    When BUY/OFFER rows span multiple platforms, round-robin the best picks
    per platform so a single high-volume source (e.g. Facebook) does not
    fill the entire main grid before eBay/Motors appear.
    """
    actionable = [r for r in rows if r[2].decision in ("BUY", "OFFER")]

    def _diversify(cands: list[tuple[Listing, DealInput, DealOutput]], cap: int):
        if cap <= 0:
            return []
        if len(cands) <= cap:
            return sorted(cands, key=lambda r: -r[2].expected_profit)
        platforms = {r[0].platform for r in cands}
        if len(platforms) <= 1:
            return sorted(cands, key=lambda r: -r[2].expected_profit)[:cap]
        by_plat: Dict[str, list[tuple[Listing, DealInput, DealOutput]]] = {}
        for r in cands:
            by_plat.setdefault(r[0].platform, []).append(r)
        for bucket in by_plat.values():
            bucket.sort(key=lambda r: -r[2].expected_profit)
        plat_order = sorted(
            by_plat.keys(),
            key=lambda p: (
                -by_plat[p][0][2].expected_profit if by_plat[p] else 0.0,
                p,
            ),
        )
        picked: list[tuple[Listing, DealInput, DealOutput]] = []
        idx = 0
        while len(picked) < cap:
            progressed = False
            for p in plat_order:
                if len(picked) >= cap:
                    break
                b = by_plat[p]
                if idx < len(b):
                    picked.append(b[idx])
                    progressed = True
            if not progressed:
                break
            idx += 1
        if len(picked) < cap:
            keyfn = lambda r: (r[0].platform, r[0].item_id)
            chosen = {keyfn(r) for r in picked}
            rest = [r for r in cands if keyfn(r) not in chosen]
            rest.sort(key=lambda r: -r[2].expected_profit)
            picked.extend(rest[: cap - len(picked)])
        return picked[:cap]

    out = _diversify(actionable, limit)
    if len(out) >= limit:
        return out
    pass_floor = -max(80.0, near_miss_band * 0.6)
    useful_pass = [
        r for r in rows
        if r[2].decision == "PASS" and r[2].expected_profit >= pass_floor
    ]
    useful_pass.sort(key=lambda r: -r[2].expected_profit)
    out.extend(useful_pass[: max(0, limit - len(out))])
    return out[:limit]


def run(cfg: Config) -> None:
    _run_start = time.time()
    _fast_mode = os.environ.get("DEALERLY_FAST_MODE", "").strip().lower() in {"1", "true", "yes"}
    _debug_log: dict = {
        "version": "0.9.9",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "capital": cfg.capital, "price_min": cfg.price_min,
            "price_max": cfg.price_max, "target_margin": cfg.target_margin,
            "mot_mode": cfg.mot_mode, "enrich_n": cfg.enrich_n,
            "preset": cfg.preset, "input_mode": cfg.input_mode,
            "fast_mode": _fast_mode,
        },
        "phases": {},
    }
    _obsidian_vrm_cache_hits = 0
    _anpr_skips = 0
    _budget_counters: Dict[str, int] = {
        "autotrader_candidates_phase2": 0,
        "autotrader_candidates_phase4": 0,
        "anpr_calls": 0,
        "anpr_skipped_verified": 0,
        "anpr_skipped_cache": 0,
        "anpr_skipped_budget": 0,
        "item_vrm_cache_hits": 0,
        "dvla_calls": 0,
        "dvla_validation_calls": 0,
        "dvla_skipped_top_slice": 0,
        # Sprint 5: Facebook quality counters
        "fb_total": 0,
        "fb_titles_good": 0,
        "fb_mileage_found": 0,
        "fb_thumb_found": 0,
    }
    _loading_path = ""
    _loading_flag = os.environ.get("DEALERLY_LOADING_ALREADY_OPEN", "0") == "1"
    _loading_opened = _loading_flag and (REPORTS_DIR / "dealerly_loading.html").exists()

    def _set_loading(pct: int, stage: str, *, done: bool = False, report_path: str = "") -> None:
        nonlocal _loading_path, _loading_opened
        if not cfg.open_html_report:
            return
        try:
            _loading_path = write_loading_screen(
                progress_pct=pct,
                stage_text=stage,
                done=done,
                report_path=report_path,
            )
            if not _loading_opened:
                _open_path_preferred(_loading_path, line=1)
                _loading_opened = True
        except Exception:
            pass

    _set_loading(2, "Initializing Dealerly")

    init_db(DB_PATH)
    conn = db_connect(DB_PATH)
    _obsidian_root = obsidian_vault_path()
    _brain_t0 = time.time()
    obsidian_brain = load_obsidian_brain(_obsidian_root)
    print(
        f"[Obsidian] Vault: {_obsidian_root} | "
        f"VRM memory: {obsidian_brain.vrm_count} rows "
        f"({time.time() - _brain_t0:.2f}s)"
    )
    try:
        _wal_mode = conn.execute("PRAGMA journal_mode;").fetchone()
        _wal = str((_wal_mode or [""])[0]).lower() == "wal"
        print(f"[DB] WAL mode: {'ON' if _wal else 'OFF'}")
    except Exception as exc:
        print(f"[DB] WAL check failed: {type(exc).__name__}: {exc}")

    mot_provider = build_mot_provider(cfg.mot_mode)
    _mot_mode_labels = {"0": "disabled", "1": "mock-json", "2": "DVSA"}
    _mot_status = (
        f"{mot_provider.provider_name} (active)"
        if mot_provider else
        f"DISABLED (mot_mode={cfg.mot_mode} / {_mot_mode_labels.get(cfg.mot_mode, '?')})"
    )
    print(f"[MOT] Provider: {_mot_status}")

    at_comps: Optional[AutoTraderComps] = None
    if cfg.use_autotrader:
        at_comps = AutoTraderComps(postcode=cfg.autotrader_postcode)
        print(f"[AutoTrader] comps enabled (postcode={cfg.autotrader_postcode})")

    client_id     = os.environ.get("EBAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET in .env.")
    ebay_env_raw = os.environ.get("EBAY_ENV", "production").strip().lower()
    ebay_env     = resolve_ebay_env(ebay_env_raw, client_id)
    token        = ebay_app_token(env=ebay_env, client_id=client_id, client_secret=client_secret)

    anpr_enabled = is_vision_available()
    dvla_enabled = is_dvla_available()

    print(f"[eBay] auth OK ({ebay_env}) | AI={cfg.ai_backend}"
          f"{' | AutoTrader=ON' if at_comps else ''}"
          f" | ANPR={'ON' if anpr_enabled else 'OFF'}"
          f" | DVLA={'ON' if dvla_enabled else 'OFF'}"
          f" | Location={cfg.buyer_postcode} ({cfg.search_radius_miles}mi)")
    if anpr_enabled:
        print(
            f"  [efficiency] ANPR: up to {anpr_max_images()} photo(s)/listing; "
            f"profit gate >= \u00a3{anpr_min_profit_gbp():.0f} (env: DEALERLY_ANPR_MAX_IMAGES, "
            f"DEALERLY_ANPR_MIN_PROFIT_GBP)"
        )
    _oai_base = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    if openai_api_key() and ("localhost" in _oai_base.lower() or "127.0.0.1" in _oai_base.lower()):
        print(f"  [efficiency] Local OpenAI-compatible API: {_oai_base[:72]}")
    if _fast_mode:
        print("[Mode] Fast mode active (reduced candidate and enrichment budgets)")

    _stop_after = int(getattr(cfg, "stop_after_phase", 0) or 0)
    _env_stop = os.environ.get("DEALERLY_STOP_AFTER_PHASE", "").strip()
    if _stop_after == 0 and _env_stop in ("2", "3"):
        try:
            _stop_after = int(_env_stop)
        except ValueError:
            pass
    if _stop_after in (2, 3):
        print(
            f"  [Pipeline] Early-stop: exit after Phase {_stop_after} only "
            f"(Config.stop_after_phase or env DEALERLY_STOP_AFTER_PHASE=2|3)."
        )

    buying_filter_main = "buyingOptions:{AUCTION}" if cfg.auction_only else ""

    # -----------------------------------------------------------------
    # Helper: paged eBay search
    # -----------------------------------------------------------------
    def fetch_paged(term: str) -> List[Listing]:
        results: List[Listing] = []
        for page in range(cfg.pages):
            offset = page * PAGE_SIZE
            try:
                payload = ebay_search(
                    env=ebay_env, token=token,
                    price_min=cfg.price_min, price_max=cfg.price_max,
                    limit=PAGE_SIZE, offset=offset, keywords=term,
                    category_ids=cfg.category_ids,
                    buying_filter=buying_filter_main, sort=cfg.sort,
                    buyer_postcode=cfg.buyer_postcode,
                    search_radius_miles=cfg.search_radius_miles,
                )
            except requests.exceptions.RequestException as exc:
                _t = (term[:40] + "...") if len(term) > 40 else term
                print(
                    f"  [eBay] search failed ({type(exc).__name__}) for '{_t}' page {page} "
                    f"- returning {len(results)} listing(s) gathered so far"
                )
                break
            total = int(payload.get("total", 0) or 0)
            batch = normalise_ebay_items(payload)
            batch = hard_price_filter(batch, float(cfg.price_min), float(cfg.price_max))
            kept = [l for l in batch if is_strict_whole_car(l.title)]
            print(f"  [scan] '{term}' p{page} -> {len(kept)} cars "
                  f"({len(batch)-len(kept)} rejected)")
            results.extend(kept)
            if total and (offset + PAGE_SIZE) >= total:
                break
            if len(batch) < PAGE_SIZE:
                break
            time.sleep(REQUEST_SLEEP_S)
        return results

    # -----------------------------------------------------------------
    # Helper: eBay comps for a vehicle key
    # -----------------------------------------------------------------
    def fetch_ebay_comps(key: str) -> List[Tuple]:
        q = comps_query_from_key(key)
        if not q:
            return []
        rows: List[Tuple] = []
        comps_min = max(200, int(cfg.price_min * 0.6))
        comps_max = max(cfg.price_max, int(cfg.price_max * 1.8))
        try:
            for page in range(3):
                payload = ebay_search(
                    env=ebay_env, token=token,
                    price_min=comps_min, price_max=comps_max,
                    limit=PAGE_SIZE, offset=page * PAGE_SIZE,
                    keywords=q, category_ids=cfg.category_ids,
                    buying_filter="buyingOptions:{FIXED_PRICE}", sort="newlyListed",
                )
                batch = normalise_ebay_items(payload)
                batch = hard_price_filter(batch, float(comps_min), float(comps_max))
                for listing in batch:
                    if not is_strict_whole_car(listing.title):
                        continue
                    g = guess_make_model(listing.title)
                    rows.append((
                        float(listing.price_gbp), g.year, g.mileage,
                        listing.location, listing.url,
                    ))
                    if len(rows) >= DEFAULT_COMPS_LOOKUP_LIMIT:
                        return rows
                if len(batch) < PAGE_SIZE:
                    break
                time.sleep(0.2)
        except requests.exceptions.RequestException as exc:
            _frag = (key[:56] + "...") if len(key) > 56 else key
            print(
                f"  [eBay] comps fetch failed ({type(exc).__name__}: {exc}) for `{_frag}` "
                f"- using {len(rows)} partial row(s) / cached+fallback scoring"
            )
        return rows

    # =================================================================
    # PHASE 1: Gather listings
    # =================================================================
    print(f"\n[Phase 1] Gathering listings (mode={cfg.input_mode})...")
    if cfg.input_mode in ("ebay", "motors"):
        print(
            "  [Phase 1] Facebook Marketplace is not used in "
            f"{cfg.input_mode!r} mode — set input mode to "
            "'all' (All platforms) to include Facebook alongside other sources."
        )
    _set_loading(12, "Phase 1/7: Gathering listings")

    # ------------------------------------------------------------------
    # Adapter-based ingestion (eBay / Motors / Facebook Marketplace)
    # ------------------------------------------------------------------
    _platform_results: dict = {}  # platform_name -> count
    preset = QUERY_PRESETS.get(cfg.preset, QUERY_PRESETS["6"])
    queries: list[str]
    if preset.get("mode") == "multi":
        _preset_qs = list(preset["qs"])
        queries = scale_vehicle_queries_for_capital(
            _preset_qs, cfg.capital, cfg.price_max
        )
        if queries != _preset_qs:
            print(
                f"  [Phase 1] Vehicle queries scaled to capital £{cfg.capital:.0f} "
                f"(price_max £{cfg.price_max}): {len(_preset_qs)} → {len(queries)} terms"
            )
    else:
        default_kw = preset.get("q", "used car")
        _env_kw = (os.environ.get("DEALERLY_KEYWORDS", "") or "").strip()
        if _env_kw:
            kw = _env_kw
        elif not sys.stdin.isatty():
            kw = default_kw
        else:
            try:
                kw = input(f"Keywords [default '{default_kw}']: ").strip() or default_kw
            except EOFError:
                kw = default_kw
        queries = [kw]
    if _fast_mode and len(queries) > 10:
        queries = queries[:10]
        print(f"  [Fast mode] Query count capped to {len(queries)} terms")

    # Sprint 4: --no-facebook flag excludes FB adapter for faster eBay+Motors runs.
    _enabled_platforms = (
        [p for p in ENABLED_PLATFORMS if p != "facebook"]
        if os.environ.get("DEALERLY_NO_FACEBOOK", "").strip() == "1"
        else ENABLED_PLATFORMS
    )
    adapters_to_run = _build_adapter_list(
        input_mode=cfg.input_mode,
        enabled_platforms=_enabled_platforms,
        fetch_paged_fn=fetch_paged,
    )

    _p1_adapter_names = [a.platform_name() for a in adapters_to_run]
    print(
        f"  [Phase 1] input_mode={cfg.input_mode!r} → "
        f"adapters: {', '.join(_p1_adapter_names) if _p1_adapter_names else '(none)'}"
    )
    if os.environ.get("DEALERLY_NO_FACEBOOK", "").strip() == "1":
        print(
            "  [Phase 1] DEALERLY_NO_FACEBOOK=1 or --no-facebook — "
            "Facebook Marketplace adapter is disabled for this run."
        )
    elif _p1_adapter_names and "facebook" not in _p1_adapter_names:
        print(
            "  [Phase 1] Facebook Marketplace is not in this run — "
            "input_mode is single-source only (not 'all' / 'multi'). "
            "Use Input mode 'all' in web setup or CLI to run FB with eBay/Motors."
        )

    # Sprint 16: agent mode — Claude drives the search strategy adaptively.
    # The agent loop replaces the fixed Phase 1 concurrent block; the returned
    # listings feed Phase 2+ unchanged.
    if cfg.agent_mode:
        from dealerly.agent import run_dealerly_agent
        _agent_state = run_dealerly_agent(cfg=cfg, adapters=adapters_to_run)
        listings = _agent_state.listings
        for _adp in adapters_to_run:
            _platform_results[_adp.platform_name()] = sum(
                1 for l in listings if l.platform == _adp.platform_name()
            )
    elif adapters_to_run:
        # Sprint 15: run all platform adapters concurrently — eBay, Motors, FB,
        # and PistonHeads all blocked on network. Running in parallel cuts Phase 1
        # wall-clock time from ~200s to ~70-80s (2x+ speedup on typical presets).
        # Note: Facebook uses Playwright sync API — not thread-safe. facebook.py
        # automatically runs Marketplace scraping in a subprocess when invoked from
        # these worker threads (see dealerly/facebook.py fetch_listings).
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

        # Skip unavailable adapters upfront (before spawning threads)
        _runnable: list = []
        for _adp in adapters_to_run:
            if not _adp.is_available:
                _reason = ""
                try:
                    _reason = (getattr(_adp, "unavailable_reason", "") or "").strip()
                except Exception:
                    _reason = ""
                print(
                    f"  [Phase 1] Skipping {_adp.platform_name()} "
                    f"(adapter unavailable{': ' + _reason if _reason else ''})"
                )
                _platform_results[_adp.platform_name()] = -1
            else:
                _runnable.append(_adp)

        all_batches: List[List[Listing]] = []

        def _fetch_adapter(adp):
            """Thread worker: fetch all listings from one adapter."""
            name = adp.platform_name()
            print(f"  [Phase 1] Fetching from {name}...")
            return adp.fetch_listings(
                queries=queries,
                price_min=cfg.price_min,
                price_max=cfg.price_max,
                pages=cfg.pages,
                buyer_postcode=cfg.buyer_postcode,
                sort=cfg.sort,
            )

        # One thread per adapter — they are network-bound so GIL is not a bottleneck
        _max_p1_workers = max(1, min(len(_runnable), 4))
        with ThreadPoolExecutor(max_workers=_max_p1_workers) as _p1_pool:
            _p1_futs = {_p1_pool.submit(_fetch_adapter, adp): adp for adp in _runnable}
            for _fut in _as_completed(_p1_futs):
                adp = _p1_futs[_fut]
                name = adp.platform_name()
                try:
                    batch = _fut.result()
                    all_batches.append(batch)
                    _platform_results[name] = len(batch)
                    print(f"  [Phase 1] {name} -> {len(batch)} listings")
                    if name == "facebook":
                        try:
                            from dealerly.facebook import FB_QUALITY as _fb_q
                            for _k, _v in _fb_q.items():
                                _budget_counters[_k] = _budget_counters.get(_k, 0) + int(_v)
                            print(
                                f"  [Phase 1] FB quality: "
                                f"good_titles={_fb_q.get('fb_titles_good', 0)}"
                                f"/{_fb_q.get('fb_total', 0)} "
                                f"mileage_found={_fb_q.get('fb_mileage_found', 0)} "
                                f"thumb_found={_fb_q.get('fb_thumb_found', 0)}"
                            )
                        except Exception:
                            pass
                except Exception as exc:
                    print(f"  [Phase 1] {name} error: {exc}")
                    all_batches.append([])
                    _platform_results[name] = 0

        listings = merge_dedupe(all_batches) if all_batches else []

        if not listings and cfg.input_mode in ("all", "multi"):
            print("  [Phase 1] All adapters returned no results — falling back to direct eBay fetch")
            _MAX_PER_QUERY = 20
            if preset.get("mode") == "multi":
                listings = merge_dedupe([fetch_paged(t)[:_MAX_PER_QUERY] for t in queries])
            else:
                listings = fetch_paged(queries[0])
            _platform_results["ebay"] = len(listings)
    else:
        _MAX_PER_QUERY = 20
        if preset.get("mode") == "multi":
            listings = merge_dedupe([fetch_paged(t)[:_MAX_PER_QUERY] for t in queries])
        else:
            listings = fetch_paged(queries[0])

    print(f"\n[Phase 1 done] {len(listings)} whole-car listings")
    # Lightweight mileage inference pass for non-enriched rows so scoring has
    # fewer unknown-mileage candidates in early phases.
    inferred_mileage = 0
    for listing in listings:
        if listing.mileage:
            continue
        _raw = listing.raw or {}
        _ebay_fields = [
            str(_raw.get("shortDescription", "") or ""),
            str(_raw.get("description", "") or ""),
            str(_raw.get("itemDescription", "") or ""),
        ]
        # For non-eBay platforms scan all string values in the raw dict
        _extra_fields = (
            [] if listing.platform == "ebay"
            else [str(v) for v in _raw.values() if isinstance(v, str) and v]
        )
        src = " ".join(
            [str(listing.title or "")] + _ebay_fields + _extra_fields
        ).lower().replace(",", "")
        m = re.search(r"\b(\d{1,3})(?:\.(\d))?\s*k\s*(?:miles?|mi)?\b", src)
        if m:
            whole = int(m.group(1))
            frac = int(m.group(2) or "0")
            v = whole * 1000 + frac * 100
            if 1_000 <= v <= 400_000:
                listing.mileage = v
                inferred_mileage += 1
                continue
        m = re.search(r"\b(\d{3,6})\s*(?:miles?|mi)\b", src)
        if m:
            v = int(m.group(1))
            if 1_000 <= v <= 400_000:
                listing.mileage = v
                inferred_mileage += 1
    if inferred_mileage:
        print(f"  [Phase 1] Mileage inferred for {inferred_mileage} listing(s)")
    _debug_log["phases"]["phase1"] = {
        "listings_gathered": len(listings),
        "mileage_inferred": inferred_mileage,
        "elapsed_s": round(time.time() - _run_start, 1),
    }

    # =================================================================
    # PHASE 2: Preliminary score
    # =================================================================
    _phase2_comp_label = "AutoTrader + eBay comps" if at_comps else "eBay comps only"
    print(f"\n[Phase 2] Preliminary scoring ({_phase2_comp_label})...")
    _set_loading(26, "Phase 2/7: Preliminary scoring")
    # v0.9.9: raised Phase 2 top_n from 60 to 150 to prevent the candidate
    # pool being too shallow for Phase 4. With top_n=60, borderline OFFER
    # listings ranked 61-90 were never reaching Phase 4 scoring.
    # Runtime-focused cap: keep pool proportional to actual listing volume.
    _phase2_cap_floor = 50 if _fast_mode else 60
    _phase2_cap = min(len(listings), max(cfg.enrich_n * 2, _phase2_cap_floor))
    phase2_input = _select_phase2_input(listings, _phase2_cap)
    _PHASE2_TOP_N = min(len(phase2_input), max(DEFAULT_TOP_N, cfg.enrich_n, 45 if _fast_mode else 55))
    if at_comps:
        _budget_counters["autotrader_candidates_phase2"] = len(phase2_input)
    prelim_rows, prelim_stats = score_listings(
        phase2_input, conn=conn, capital=cfg.capital,
        target_margin=cfg.target_margin, holding_cost=cfg.holding_cost,
        mot_provider=None, ebay_fee_rate=cfg.ebay_fee_rate,
        pay_fee_rate=cfg.pay_fee_rate, admin_buffer=cfg.admin_buffer,
        transport_buffer=cfg.transport_buffer,
        fetch_ebay_comps_fn=fetch_ebay_comps, at_comps=at_comps,
        comps_ttl=cfg.comps_ttl, store_comps=cfg.store_comps,
        resale_discount=cfg.resale_discount, misprice_ratio=cfg.misprice_ratio,
        require_comps=cfg.require_comps,
        top_n=min(_PHASE2_TOP_N, len(phase2_input)),
    )
    print(f"  buy={prelim_stats['buy']} offer={prelim_stats['offer']}"
          f" pass={prelim_stats['pass']} avoid={prelim_stats['avoid_shock']}"
          f" total={prelim_stats['total']} (pool={_PHASE2_TOP_N})"
          f" | misprice={prelim_stats['filtered_misprice']}"
          f" nocomps={prelim_stats['filtered_nocomps']}"
          f" fraud={prelim_stats['filtered_fraud']}")
    if at_comps:
        _at_p2_total = at_comps._cache_hits + at_comps._cache_misses
        print(f"  AutoTrader comp cache: {at_comps._cache_hits} hits / {_at_p2_total} lookups"
              f" ({at_comps._cache_hits / max(_at_p2_total, 1) * 100:.0f}% hit rate)")
    _debug_log["phases"]["phase2"] = {
        "stats": dict(prelim_stats),
        "pool_size": _PHASE2_TOP_N,
        "elapsed_s": round(time.time() - _run_start, 1),
    }

    if _stop_after == 2:
        if cfg.debug_mode:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            _debug_log["total_elapsed_s"] = round(time.time() - _run_start, 1)
            _debug_log["api_budget"] = dict(_budget_counters)
            _debug_log["early_stop"] = "after_phase_2"
            _dts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _dp = REPORTS_DIR / f"debug_{_dts}.json"
            _dp.write_text(
                json.dumps(_debug_log, indent=2, default=str),
                encoding="utf-8",
            )
            print(f"[Debug] Log written: {_dp}")
        print(
            "\n[Pipeline] Early stop: stop_after_phase=2 "
            "(after preliminary scoring — no VRM / MOT / report)."
        )
        conn.close()
        print("\nDone.")
        return

    # =================================================================
    # PHASE 3: VRM enrichment (preliminary top-N)
    # =================================================================
    _p3_enrich_stats: dict = {}  # populated below if enrichment runs
    enriched_ids: set[str] = set()
    _set_loading(42, "Phase 3/7: VRM enrichment")
    if cfg.enrich_mode != "0":
        if cfg.enrich_mode == "1":
            # v0.9.7: Only enrich BUY/OFFER prelim candidates + any PASS within
            # £150 of breakeven (could become OFFER once MOT data adjusts repairs).
            # No point spending enrichment time on deep-negative listings.
            VRM_ENRICH_PROFIT_FLOOR = -150.0
            actionable = [
                x[0] for x in prelim_rows
                if x[2].decision in ("BUY", "OFFER")
                or x[2].expected_profit >= VRM_ENRICH_PROFIT_FLOOR
            ]
            to_enrich = actionable[:cfg.enrich_n]
        else:
            to_enrich = listings
        to_enrich_ebay = [l for l in to_enrich if l.platform == "ebay"]
        csv_with_vrm   = [l for l in to_enrich if l.platform != "ebay" and l.vrm]

        sources_str = "regex"
        if dvla_enabled:
            sources_str += " + DVLA"
        if anpr_enabled:
            sources_str += " + ANPR"
        # Sprint 12: profit-weighted ANPR budget replaces positional slice.
        # _anpr_budget_cap = hard ceiling on listings approved for ANPR in main loop.
        # _anpr_budget_used = running count of listings approved this loop.
        _anpr_budget_cap = 8 if _fast_mode else 12
        _anpr_budget_used = 0
        _anpr_profit_mult = anpr_profit_weight()  # default 1.5x
        _dvla_top_slice = 10 if _fast_mode else 14

        print(f"\n[Phase 3] VRM enrichment: {len(to_enrich_ebay)} eBay"
              f" + {len(csv_with_vrm)} non-eBay with VRM"
              f"  ({sources_str})")
        vrm_found = len(csv_with_vrm)
        _phase3_t0 = time.time()
        _PHASE3_TIME_BUDGET_S = 75.0 if _fast_mode else 120.0
        _attempted_ebay = 0

        # Sprint 1: score gate — only spend ANPR credits on listings whose
        # prelim expected_profit meets the threshold. Build lookup + gate once.
        prelim_score_map = {l.item_id: o for l, _d, o in prelim_rows}
        _anpr_min_profit = anpr_min_profit_gbp()  # cache — avoids per-listing env reads

        # Sprint 6: free-text title VRM pre-pass — runs on ALL to_enrich listings
        # before the ANPR loop. Catches VRMs in titles for non-eBay listings
        # (Facebook/Motors) that skip full eBay enrichment, and may save ANPR
        # credits for eBay listings with plates embedded in the title.
        # Confidence gate: 0.70 (labelled context scores 0.96+, body 0.88).
        _text_prepass_found = 0
        for _l in to_enrich:
            if _l.vrm:
                continue
            _raw_extra = ""
            if isinstance(_l.raw, dict):
                # Try to get any available description / aria / hint text from raw
                for _k in ("shortDescription", "description", "card"):
                    _v = _l.raw.get(_k)
                    if isinstance(_v, str) and _v:
                        _raw_extra = _v
                        break
                    elif isinstance(_v, dict):
                        # Facebook card dict — grab texts list
                        _raw_extra = " ".join(str(x) for x in (_v.get("texts") or []))
                        break
            _combined = (_l.title + " " + _raw_extra).strip()
            _hits = extract_vrm_from_text(_combined, year=_l.year)
            if _hits and _hits[0][1] >= 0.70:
                _cand, _conf = _hits[0]
                if vrm_year_plausible(_cand, _l.year, tolerance=8):
                    _l.vrm = _cand
                    _l.vrm_confidence = _conf
                    _l.vrm_source = "text_prepass"
                    _l.vrm_evidence = "title/raw text pre-pass (Phase 3 S6)"
                    _text_prepass_found += 1
        if _text_prepass_found:
            print(f"  [Phase 3] Text pre-pass: {_text_prepass_found} VRM(s) extracted from title/text")

        # Sprint 11: ANPR on listing photos for non-eBay (FB/Motors) — same API as
        # eBay enrichment but without Browse API; relies on hero + gallery URLs.
        _non_ebay_anpr_found = 0
        if anpr_enabled:
            _ne_anpr_slice = 12 if _fast_mode else 16
            _ne_for_anpr = [l for l in to_enrich if l.platform != "ebay" and not l.vrm]
            _ne_for_anpr.sort(
                key=lambda x: float(
                    getattr(prelim_score_map.get(x.item_id), "expected_profit", 0.0) or 0.0
                ),
                reverse=True,
            )
            for _l in _ne_for_anpr[:_ne_anpr_slice]:
                po = prelim_score_map.get(_l.item_id)
                if po and po.decision == "AVOID":
                    continue
                if po and float(getattr(po, "expected_profit", 0.0) or 0.0) < _anpr_min_profit:
                    continue
                if _anpr_non_ebay_from_listing(_l, conn, budget_counters=_budget_counters):
                    _non_ebay_anpr_found += 1
                    vrm_found += 1
        if _non_ebay_anpr_found:
            print(
                f"  [Phase 3] Non-eBay ANPR: {_non_ebay_anpr_found} VRM(s) from listing photos"
            )

        # v1.0: Sort eBay enrichment candidates by descending prelim profit so
        # highest-value listings reach the front of the queue and are not starved
        # of ANPR/DVLA credits by lower-value rows that happen to appear first.
        to_enrich_ebay.sort(
            key=lambda l: float(
                getattr(prelim_score_map.get(l.item_id), "expected_profit", 0.0) or 0.0
            ),
            reverse=True,
        )

        # v1.0 Priority enrichment pass — top-N by prelim profit get ANPR/DVLA
        # unconditionally (bypasses idx-slice caps) before the general loop.
        # Controlled by DEALERLY_PRIORITY_ENRICH_N env var (default 5).
        _priority_n = priority_enrich_n()
        priority_candidates = to_enrich_ebay[:_priority_n]
        if priority_candidates:
            print(f"\n[Phase 3] Priority pass: top {len(priority_candidates)} profit candidates")
        for p_listing in priority_candidates:
            p_prelim = prelim_score_map.get(p_listing.item_id)
            p_profit = float(getattr(p_prelim, "expected_profit", 0.0) or 0.0)
            # Profit gate is intentionally omitted here: these are the top-N by
            # prelim profit, so they've already passed the profit ranking filter.
            # Gating on _anpr_min_profit again would be redundant and could starve
            # the highest-value row if its prelim score is conservative.
            p_anpr = anpr_enabled and not p_listing.vrm
            p_dvla = (
                dvla_enabled
                and (p_prelim is None
                     or p_prelim.decision in ("BUY", "OFFER")
                     or p_profit >= 50.0)
            )
            p_steps = _enrich_single_listing(
                p_listing,
                ebay_env=ebay_env, token=token,
                dvla_enabled=dvla_enabled, dvla_allowed=p_dvla,
                anpr_enabled=p_anpr,
                allow_page_scrape=(p_profit >= 120),
                conn=conn,
                obsidian_brain=obsidian_brain,
                budget_counters=_budget_counters,
            )
            enriched_ids.add(p_listing.item_id)
            _attempted_ebay += 1
            if "obsidian-cache+" in p_steps:
                _obsidian_vrm_cache_hits += 1
            if any(s.startswith("anpr-skip(") for s in p_steps):
                _anpr_skips += 1
            if p_listing.vrm:
                vrm_found += 1
            _displayable = is_vrm_displayable(p_listing.vrm, p_listing.vrm_confidence)
            _vrm_status = (
                f"+ {p_listing.vrm} ({p_listing.vrm_confidence:.0%})"
                if _displayable else "- none"
            )
            _miles_str = f"{p_listing.mileage/1000:.0f}k" if p_listing.mileage else "?k"
            print(
                f"  [P{p_listing.item_id[-4:]}] {_vrm_status:<22s}"
                f" {_miles_str:>5s}  [{' > '.join(p_steps)}]  {p_listing.title[:40]}"
            )

        # Denominator for progress bar excludes items already handled by the priority pass.
        _main_enrich_count = max(1, len(to_enrich_ebay) - len(priority_candidates))
        _main_loop_idx = 0
        for idx, listing in enumerate(to_enrich_ebay):
            # Skip listings already handled by the priority pass.
            if listing.item_id in enriched_ids:
                continue
            _phase3_idx_gate = 6 if _fast_mode else 8
            if idx >= _phase3_idx_gate and (time.time() - _phase3_t0) > _PHASE3_TIME_BUDGET_S:
                print(
                    f"  [Phase 3] Time budget reached after {_attempted_ebay} eBay enrichments; "
                    "continuing with current enriched pool."
                )
                break
            _set_loading(
                42 + int((_main_loop_idx / _main_enrich_count) * 14),
                f"Phase 3/7: VRM enrichment ({_main_loop_idx + 1}/{_main_enrich_count})",
            )
            enriched_ids.add(listing.item_id)
            prelim_out = prelim_score_map.get(listing.item_id)
            # Sprint 12: profit-weighted ANPR budget — replace positional idx slice.
            # Gate: profit >= 1.5× (or DEALERLY_ANPR_PROFIT_WEIGHT ×) anpr_min_profit,
            # AVOID excluded (Sprint 7 guardrail preserved), hard cap = _anpr_budget_cap.
            # Listings that already have a high-confidence VRM (>= 0.88) don't count
            # against the budget since _enrich_single_listing will skip ANPR for them.
            _profit_for_listing = float(
                getattr(prelim_out, "expected_profit", 0.0) or 0.0
            )
            _listing_needs_anpr = not (
                listing.vrm and (listing.vrm_confidence or 0.0) >= 0.88
            )
            anpr_for_listing = (
                anpr_enabled
                and _listing_needs_anpr
                and _anpr_budget_used < _anpr_budget_cap
                and (prelim_out is None
                     or (_profit_for_listing >= _anpr_profit_mult * _anpr_min_profit
                         and prelim_out.decision != "AVOID"))
            )
            if anpr_for_listing:
                _anpr_budget_used += 1
            if anpr_enabled and not anpr_for_listing:
                _budget_counters["anpr_skipped_budget"] += 1
            # Sprint 4: loosen DVLA top-slice by +4 for high-profit rows (≥ £200)
            # so they are not starved of DVLA enrichment by index position alone.
            _dvla_slice_cap = (
                _dvla_top_slice + 4
                if (prelim_out is not None and prelim_out.expected_profit >= 200.0)
                else _dvla_top_slice
            )
            dvla_for_listing = (
                dvla_enabled
                and idx < _dvla_slice_cap
                and (prelim_out is None or prelim_out.decision in ("BUY", "OFFER") or prelim_out.expected_profit >= 50.0)
            )
            steps = _enrich_single_listing(
                listing,
                ebay_env=ebay_env, token=token,
                dvla_enabled=dvla_enabled, dvla_allowed=dvla_for_listing,
                anpr_enabled=anpr_for_listing,
                allow_page_scrape=(prelim_out is not None and prelim_out.expected_profit >= 120),
                conn=conn,
                obsidian_brain=obsidian_brain,
                budget_counters=_budget_counters,
            )
            _attempted_ebay += 1
            _main_loop_idx += 1
            if "obsidian-cache+" in steps:
                _obsidian_vrm_cache_hits += 1
            if any(s.startswith("anpr-skip(") for s in steps):
                _anpr_skips += 1

            if listing.vrm:
                vrm_found += 1

            displayable = is_vrm_displayable(listing.vrm, listing.vrm_confidence)
            vrm_status = (f"+ {listing.vrm} ({listing.vrm_confidence:.0%})"
                          if displayable else "- none")
            miles_str = f"{listing.mileage/1000:.0f}k" if listing.mileage else "?k"
            print(f"  [{_main_loop_idx:02d}/{_main_enrich_count}] {vrm_status:<22s}"
                  f" {miles_str:>5s}"
                  f"  [{' > '.join(steps)}]  {listing.title[:40]}")

        total_enrich = _attempted_ebay + len(csv_with_vrm)
        pct = vrm_found / max(total_enrich, 1) * 100
        print(f"[Phase 3 done] VRM found {vrm_found}/{total_enrich} "
              f"({pct:.0f}%)  [{sources_str}]")
        print(f"  Obsidian VRM cache hits: {_obsidian_vrm_cache_hits}")
        print(f"  ANPR calls avoided (verified/cache paths): {_anpr_skips}")
        print(
            f"  ANPR calls: {_budget_counters['anpr_calls']} "
            f"(skipped budget={_budget_counters['anpr_skipped_budget']}, "
            f"verified={_budget_counters['anpr_skipped_verified']}, cache={_budget_counters['anpr_skipped_cache']})"
        )
        print(
            f"  DVLA calls: {_budget_counters['dvla_calls']} + "
            f"{_budget_counters['dvla_validation_calls']} validations "
            f"(skipped top-slice={_budget_counters['dvla_skipped_top_slice']})"
        )
        _p3_enrich_stats = {
            "enriched_total": total_enrich,
            "vrm_found_p3": vrm_found,
            "obsidian_cache_hits": _obsidian_vrm_cache_hits,
            "anpr_avoided": _anpr_skips,
            "anpr_calls": _budget_counters["anpr_calls"],
            "anpr_skipped_budget": _budget_counters["anpr_skipped_budget"],
            "anpr_skipped_verified": _budget_counters["anpr_skipped_verified"],
            "anpr_skipped_cache": _budget_counters["anpr_skipped_cache"],
            "item_vrm_cache_hits": _budget_counters["item_vrm_cache_hits"],
            "dvla_calls": _budget_counters["dvla_calls"],
            "dvla_validation_calls": _budget_counters["dvla_validation_calls"],
            "dvla_skipped_top_slice": _budget_counters["dvla_skipped_top_slice"],
        }
        _debug_log["phases"]["phase3"] = {
            "enriched": total_enrich,
            "vrm_found": vrm_found,
            "vrm_pct": round(pct, 1),
            "sources": sources_str,
            "elapsed_s": round(time.time() - _run_start, 1),
        }
    else:
        _debug_log["phases"]["phase3"] = {
            "skipped": True,
            "reason": "enrich_mode=0",
            "elapsed_s": round(time.time() - _run_start, 1),
        }

    if _stop_after == 3:
        if cfg.debug_mode:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            _debug_log["total_elapsed_s"] = round(time.time() - _run_start, 1)
            _debug_log["api_budget"] = dict(_budget_counters)
            _debug_log["early_stop"] = "after_phase_3"
            _dts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _dp = REPORTS_DIR / f"debug_{_dts}.json"
            _dp.write_text(
                json.dumps(_debug_log, indent=2, default=str),
                encoding="utf-8",
            )
            print(f"[Debug] Log written: {_dp}")
        print(
            "\n[Pipeline] Early stop: stop_after_phase=3 "
            "(after VRM enrichment — no final MOT scoring / HTML report / Phase 5+)."
        )
        conn.close()
        print("\nDone.")
        return

    # =================================================================
    # PHASE 4: Final score with MOT + AutoTrader
    # =================================================================
    # v0.9.9: raised Phase 4 candidate limit from max(80, enrich_n*3) to
    # max(120, enrich_n*4) to match the expanded Phase 2 pool. This ensures
    # borderline OFFER listings that scored just outside the top-80 in Phase 2
    # still get the full MOT + AutoTrader treatment in Phase 4.
    # Scale Phase 4 candidate limit with enrich_n so setup sliders have real effect,
    # while still enforcing a hard ceiling for runtime control.
    _phase4_floor = 40 if _fast_mode else 55
    _phase4_ceiling = 90 if _fast_mode else 120
    _phase4_target = max(_phase4_floor, int(cfg.enrich_n * 1.3))
    _PHASE4_CANDIDATE_LIMIT = min(len(prelim_rows), min(_phase4_target, _phase4_ceiling))
    prelim_scan_pool = prelim_rows[: max(_PHASE4_CANDIDATE_LIMIT, 70)]
    phase4_selected: List[Tuple[Listing, DealInput, DealOutput]] = []
    selected_ids: set[str] = set()

    # Keep score-first ranking, but reserve some capacity for non-eBay candidates
    # so cross-market opportunities are visible in final output.
    non_ebay_platforms = {
        l.platform for l, _, _ in prelim_scan_pool
        if l.platform and l.platform != "ebay"
    }
    reserve_per_platform = 6
    reserve_total = min(
        max(0, _PHASE4_CANDIDATE_LIMIT // 3),
        reserve_per_platform * len(non_ebay_platforms),
    )
    core_target = max(0, _PHASE4_CANDIDATE_LIMIT - reserve_total)

    for row in prelim_scan_pool:
        if len(phase4_selected) >= core_target:
            break
        listing = row[0]
        if listing.item_id in selected_ids:
            continue
        phase4_selected.append(row)
        selected_ids.add(listing.item_id)

    for platform in sorted(non_ebay_platforms):
        added = 0
        for row in prelim_scan_pool:
            if len(phase4_selected) >= _PHASE4_CANDIDATE_LIMIT:
                break
            listing = row[0]
            if listing.platform != platform or listing.item_id in selected_ids:
                continue
            phase4_selected.append(row)
            selected_ids.add(listing.item_id)
            added += 1
            if added >= reserve_per_platform:
                break

    if len(phase4_selected) < _PHASE4_CANDIDATE_LIMIT:
        for row in prelim_scan_pool:
            if len(phase4_selected) >= _PHASE4_CANDIDATE_LIMIT:
                break
            listing = row[0]
            if listing.item_id in selected_ids:
                continue
            phase4_selected.append(row)
            selected_ids.add(listing.item_id)

    phase4_input = [l for l, _, _ in phase4_selected]
    if at_comps:
        _budget_counters["autotrader_candidates_phase4"] = len(phase4_input)
    print(f"\n[Phase 4] Final scoring with MOT + AutoTrader "
          f"({len(phase4_input)} candidates from prelim top-{_PHASE4_CANDIDATE_LIMIT})...")
    _set_loading(58, "Phase 4/7: Final scoring")
    _all_rows, final_stats = score_listings(
        phase4_input, conn=conn, capital=cfg.capital,
        target_margin=cfg.target_margin, holding_cost=cfg.holding_cost,
        mot_provider=mot_provider, ebay_fee_rate=cfg.ebay_fee_rate,
        pay_fee_rate=cfg.pay_fee_rate, admin_buffer=cfg.admin_buffer,
        transport_buffer=cfg.transport_buffer,
        fetch_ebay_comps_fn=fetch_ebay_comps, at_comps=at_comps,
        comps_ttl=cfg.comps_ttl, store_comps=cfg.store_comps,
        resale_discount=cfg.resale_discount, misprice_ratio=cfg.misprice_ratio,
        require_comps=cfg.require_comps,
        top_n=max(DEFAULT_TOP_N * 4, 60),
    )
    # Split: main display excludes AVOID; separate AVOID section
    top_rows   = _select_display_rows(
        _all_rows,
        limit=DEFAULT_TOP_N,
        near_miss_band=cfg.near_miss_band,
    )
    avoid_rows = [r for r in _all_rows if r[2].decision == "AVOID"][:10]
    mot_verified = sum(1 for l, _, _ in _all_rows if l.mot_history)
    vrm_found_p4 = sum(1 for l, _, _ in _all_rows if l.vrm)
    print(f"  Display: {len(top_rows)} actionable | {len(avoid_rows)} AVOID (shown separately)")
    print(f"  MOT data: {mot_verified} verified | VRMs in pool: {vrm_found_p4}")
    if at_comps:
        _at_total = at_comps._cache_hits + at_comps._cache_misses
        _at_hit_rate = at_comps._cache_hits / max(_at_total, 1) * 100
        print(f"  AutoTrader comp cache: {at_comps._cache_hits} hits / {_at_total} lookups ({_at_hit_rate:.0f}% hit rate)")
    _debug_log["phases"]["phase4"] = {
        "candidates_scored": len(_all_rows),
        "candidate_limit": _PHASE4_CANDIDATE_LIMIT,
        "stats": dict(final_stats),
        "mot_verified": mot_verified,
        "vrm_found": vrm_found_p4,
        "top_rows": len(top_rows),
        "avoid_rows": len(avoid_rows),
        "elapsed_s": round(time.time() - _run_start, 1),
    }

    # =================================================================
    # PHASE 4.5/4.6: Iterative enrichment + re-score loop
    # =================================================================
    # After Phase 4 scoring, the top-N may contain unenriched listings.
    # Enriching them can reveal MOT problems → AVOID, which brings new
    # unenriched listings into the top-N. Loop until the top-N stabilises
    # (no new listings need enrichment) or MAX_ENRICH_ROUNDS is reached.
    MAX_ENRICH_ROUNDS = 1
    _PHASE45_TIME_BUDGET_S = 45.0
    _PHASE45_RE_SCORE_MIN_NEW_VRMS = 2
    _phase45_anpr_min_profit = anpr_min_profit_gbp()  # cache once for Phase 4.5 loop
    _phase45_t0 = time.time()
    _vrm_found_p45  = 0   # running total VRMs found across all Phase 4.5 rounds
    _enriched_p45   = 0   # running total listings attempted in Phase 4.5
    if cfg.enrich_mode != "0":
        for enrich_round in range(1, MAX_ENRICH_ROUNDS + 1):
            if (time.time() - _phase45_t0) >= _PHASE45_TIME_BUDGET_S:
                print(
                    f"  [Phase 4.5] Time budget reached ({_PHASE45_TIME_BUDGET_S:.0f}s) "
                    "— skipping late targeted enrichment."
                )
                break
            needs_enrichment: List[Tuple[Listing, DealOutput, float]] = []
            for row in top_rows:
                # Defensive guard: avoid scope/unpack issues if row shape drifts.
                if not isinstance(row, tuple) or len(row) != 3:
                    continue
                l, _d, out45 = row
                if l.platform != "ebay":
                    continue
                if l.vrm and l.vrm_confidence >= 0.76:
                    continue
                decision = str(getattr(out45, "decision", "") or "")
                if decision not in ("BUY", "OFFER") and l.item_id in enriched_ids:
                    continue
                exp_profit = float(getattr(out45, "expected_profit", 0.0) or 0.0)
                needs_enrichment.append((l, out45, exp_profit))
            # Prioritise highest-value unresolved rows.
            needs_enrichment.sort(
                key=lambda x: (x[1].decision in ("BUY", "OFFER"), x[2]),
                reverse=True,
            )
            if not needs_enrichment:
                if enrich_round > 1:
                    print(f"  [Enrich] Top-N stable after {enrich_round - 1} round(s)")
                break

            n_to_enrich = min(len(needs_enrichment), 4)
            print(f"\n[Phase 4.5 r{enrich_round}] Enriching {n_to_enrich} actionable "
                  f"listings still missing VRMs")
            vrm_found_r = 0
            attempted_r = 0
            for idx, (listing, out45, _exp_profit) in enumerate(needs_enrichment[:n_to_enrich]):
                if (time.time() - _phase45_t0) >= _PHASE45_TIME_BUDGET_S:
                    print(
                        f"  [Phase 4.5 r{enrich_round}] Round budget hit after {attempted_r} listing(s); "
                        "stopping enrichment loop."
                    )
                    break
                _set_loading(60 + int((idx / max(1, n_to_enrich)) * 10), f"Phase 4.5/7: targeted enrichment r{enrich_round} ({idx+1}/{n_to_enrich})")
                enriched_ids.add(listing.item_id)
                attempted_r += 1
                anpr_allowed_p45 = (
                    anpr_enabled
                    and idx < 3
                    and out45.expected_profit >= _phase45_anpr_min_profit
                )
                if anpr_enabled and not anpr_allowed_p45:
                    _budget_counters["anpr_skipped_budget"] += 1
                dvla_allowed_p45 = (
                    dvla_enabled
                    and idx < 3
                    and (out45.decision in ("BUY", "OFFER") or out45.expected_profit >= 50.0)
                )
                steps = _enrich_single_listing(
                    listing,
                    ebay_env=ebay_env, token=token,
                    dvla_enabled=dvla_enabled,
                    dvla_allowed=dvla_allowed_p45,
                    anpr_enabled=anpr_allowed_p45,
                    allow_page_scrape=False,
                    conn=conn,
                    obsidian_brain=obsidian_brain,
                    budget_counters=_budget_counters,
                )
                if "obsidian-cache+" in steps:
                    _obsidian_vrm_cache_hits += 1
                if any(s.startswith("anpr-skip(") for s in steps):
                    _anpr_skips += 1
                if listing.vrm:
                    vrm_found_r += 1
                displayable = is_vrm_displayable(listing.vrm, listing.vrm_confidence)
                vrm_status = (f"+ {listing.vrm} ({listing.vrm_confidence:.0%})"
                              if displayable else "- none")
                miles_str = f"{listing.mileage/1000:.0f}k" if listing.mileage else "?k"
                print(f"  [{idx+1:02d}/{n_to_enrich}] {vrm_status:<22s}"
                      f" {miles_str:>5s}"
                      f"  [{' > '.join(steps)}]  {listing.title[:40]}")
            _vrm_found_p45 += vrm_found_r
            _enriched_p45  += attempted_r
            if attempted_r == 0:
                print(f"[Phase 4.5 r{enrich_round}] No candidates attempted in budget window.")
                break
            print(f"[Phase 4.5 r{enrich_round} done] Found {vrm_found_r}/{attempted_r} new VRMs")
            if vrm_found_r == 0:
                print("  [Phase 4.5] No incremental VRM gain — skipping re-score.")
                break

            # Re-score if we found new VRMs — MOT data may push some to AVOID
            if vrm_found_r >= _PHASE45_RE_SCORE_MIN_NEW_VRMS and mot_provider:
                print(f"\n[Phase 4.6 r{enrich_round}] Re-scoring with MOT data...")
                _all_rows_r, final_stats = score_listings(
                    phase4_input, conn=conn, capital=cfg.capital,
                    target_margin=cfg.target_margin,
                    holding_cost=cfg.holding_cost,
                    mot_provider=mot_provider,
                    ebay_fee_rate=cfg.ebay_fee_rate,
                    pay_fee_rate=cfg.pay_fee_rate,
                    admin_buffer=cfg.admin_buffer,
                    transport_buffer=cfg.transport_buffer,
                    fetch_ebay_comps_fn=fetch_ebay_comps,
                    at_comps=at_comps, comps_ttl=cfg.comps_ttl,
                    store_comps=cfg.store_comps,
                    resale_discount=cfg.resale_discount,
                    misprice_ratio=cfg.misprice_ratio,
                    require_comps=cfg.require_comps,
                    top_n=max(DEFAULT_TOP_N * 4, 60),
                )
                top_rows   = _select_display_rows(
                    _all_rows_r,
                    limit=DEFAULT_TOP_N,
                    near_miss_band=cfg.near_miss_band,
                )
                avoid_rows = [r for r in _all_rows_r
                              if r[2].decision == "AVOID"][:10]
                print(f"  Re-scored: {len(top_rows)} actionable | "
                      f"{len(avoid_rows)} AVOID")
            else:
                # No new VRMs found or no MOT provider — top-N won't change
                if mot_provider and vrm_found_r > 0:
                    print(
                        f"  [Phase 4.6] Re-score skipped — only {vrm_found_r} new VRM(s); "
                        f"threshold is {_PHASE45_RE_SCORE_MIN_NEW_VRMS}."
                    )
                break

    # =================================================================
    # PHASE 5: AI offer messages
    # =================================================================
    _prod_mode = os.environ.get("DEALERLY_ENV", "").strip().lower() in {"prod", "production"}
    _offer_msgs_enabled = (
        cfg.generate_offer_msgs
        and cfg.ai_backend != "none"
        and (not _prod_mode or os.environ.get("DEALERLY_ENABLE_OFFER_MSGS", "").strip().lower() in {"1", "true", "yes"})
    )
    if _offer_msgs_enabled:
        _set_loading(72, "Phase 5/7: Offer message generation")
        buy_offer = [(l, d, o) for l, d, o in top_rows
                     if o.decision in ("BUY", "OFFER")][:6]
        if buy_offer:
            print(f"\n[Phase 5] Generating {len(buy_offer)} AI offer messages...")
            for l, d, o in buy_offer:
                l.offer_message = generate_offer_message(
                    l, d, o, conn, preferred_backend=cfg.ai_backend
                )
    elif _prod_mode and cfg.generate_offer_msgs and cfg.ai_backend != "none":
        print("\n[Phase 5] Offer messages skipped in production mode (set DEALERLY_ENABLE_OFFER_MSGS=1 to override).")

    # =================================================================
    # PHASE 6: Analytics — price trends + demand signals
    # =================================================================
    print("\n[Phase 6] Analytics...")
    _set_loading(82, "Phase 6/7: Analytics")
    try:
        obs_total, obs_new, obs_repeat = record_price_observations(conn, listings)
        print(
            f"  Price observations: {obs_total} row(s) — "
            f"{obs_new} new listing(s), {obs_repeat} repeat sighting(s)"
        )
        cur_po = conn.execute("SELECT COUNT(*) FROM price_observations")
        total_po = int(cur_po.fetchone()[0])
        lat, lon = resolve_postcode_coords(conn, cfg.buyer_postcode)
        insert_pipeline_run(
            conn,
            buyer_postcode=cfg.buyer_postcode,
            lat=lat,
            lon=lon,
            search_radius_miles=cfg.search_radius_miles,
            new_observations=obs_new,
            repeat_observations=obs_repeat,
            total_price_observations_in_db=total_po,
        )
        try:
            write_uk_stats_map_page(conn)
        except Exception as _map_exc:
            print(f"  [Stats map] {type(_map_exc).__name__}: {_map_exc}")
        compute_analytics_for_rows(conn, top_rows)
        print(f"  Trends + demand computed for {len(top_rows)} scored listings")
    except Exception as exc:
        print(f"  [Analytics] {type(exc).__name__}: {exc}")

    # =================================================================
    # PHASE 7: Workflow — auto-create CRM leads
    # =================================================================
    print("\n[Phase 7] Workflow...")
    _set_loading(90, "Phase 7/7: Workflow + Obsidian sync")
    try:
        leads_created = auto_create_leads(conn, top_rows)
        print(f"  Created {leads_created} new CRM leads")
        vault_leads_dir = _obsidian_root / "Leads"
        obsidian_exported = export_buy_leads_to_obsidian(top_rows, vault_leads_dir)
        print(f"  Obsidian BUY exports: {obsidian_exported} file(s) -> {vault_leads_dir}")
        vault_db_dir = _obsidian_root / "Database"
        vrm_exported = export_vrm_scans_to_obsidian(listings, vault_db_dir)
        print(f"  Obsidian VRM scans: {vrm_exported} row(s) -> {vault_db_dir / 'vrm_scans.md'}")
        # Backfill is expensive on every run. Only perform automatically if the
        # graph index does not yet exist, or explicitly requested via env flag.
        graph_idx = vault_db_dir / "_Graph_Index.md"
        backfill_requested = os.environ.get("DEALERLY_OBSIDIAN_BACKFILL", "").strip() in {"1", "true", "yes"}
        if backfill_requested or not graph_idx.exists():
            item_nodes, vrm_nodes = backfill_obsidian_graph_from_vrm_scans(vault_db_dir)
            print(f"  Obsidian graph backfill: {item_nodes} item node(s), {vrm_nodes} VRM node(s)")
        else:
            print("  Obsidian graph backfill: skipped (incremental mode)")
    except Exception as exc:
        print(f"  [Workflow] {type(exc).__name__}: {exc}")

    # =================================================================
    # Near-miss calculation
    # =================================================================
    all_scored = list(_all_rows)
    # v0.9.9+: near-miss uses EITHER the absolute band (£300) OR a
    # percentage threshold (max_bid >= 75% of listed price, i.e. ≤25%
    # discount needed).  This surfaces listings that are close to breakeven
    # but fall outside the narrow absolute band — most common in dead-market
    # runs where every listing is PASS.  Sorted best-ratio-first so the
    # most achievable offer appears at the top.
    near_miss = sorted(
        [(l, d, o) for l, d, o in all_scored
         if o.decision in ("PASS", "OFFER")
         and o.expected_profit < 0          # not profitable at listed price
         and o.max_bid > 0
         and (l.price_gbp - o.max_bid) > 0  # negotiation is needed
         and (
             (l.price_gbp - o.max_bid) <= cfg.near_miss_band  # absolute £300 band
             or o.max_bid / l.price_gbp >= 0.75               # OR ≤25% discount
         )],
        key=lambda x: x[2].max_bid / x[0].price_gbp,
        reverse=True,   # best ratio (smallest discount) first
    )[:8]

    # =================================================================
    # Output: console + HTML report + CSV log
    # =================================================================
    _mode_label = {
        "all": "All platforms",
        "multi": "All platforms",
        "ebay": "eBay",
        "motors": "Motors",
        "facebook": "Marketplace",
    }.get(cfg.input_mode, cfg.input_mode.capitalize())
    mode_str = f"{_mode_label} ({ebay_env}) | auction={cfg.auction_only} | AI={cfg.ai_backend}"
    auto_basket_enabled = os.environ.get("DEALERLY_ENABLE_AUTO_BASKET", "").strip().lower() in {"1", "true", "yes"}
    if auto_basket_enabled:
        basket_rows, basket_spend, basket_profit = _build_budget_basket(top_rows, cfg.capital)
        if basket_rows:
            print(
                f"\n[Budget Basket] {len(basket_rows)} listing(s) fit capital "
                f"(spend \u00a3{basket_spend:.0f}/\u00a3{cfg.capital:.0f}, expected profit \u00a3{basket_profit:.0f})"
            )
        else:
            print("\n[Budget Basket] No profitable BUY/OFFER portfolio fits current capital.")
    else:
        basket_rows, basket_spend, basket_profit = [], 0.0, 0.0
        print("\n[Cart] Manual cart mode active (auto basket disabled).")
    print_report(
        top_rows, capital=cfg.capital,
        price_min=cfg.price_min, price_max=cfg.price_max,
        mode=mode_str, target_margin=cfg.target_margin,
        holding_cost=cfg.holding_cost,
        ebay_fee_rate=cfg.ebay_fee_rate, pay_fee_rate=cfg.pay_fee_rate,
        admin_buffer=cfg.admin_buffer,
        transport_buffer=cfg.transport_buffer,
        basket_rows=basket_rows,
        basket_spend=basket_spend,
        basket_profit=basket_profit,
    )

    if near_miss:
        print(f"\n--- Near-Miss (within {cfg.near_miss_band:.0f} "
              f"-- try negotiating) ---")
        for l, d, o in near_miss:
            rounded = round_to_nearest(o.max_bid, 50)
            print(f"  {l.price_gbp:.0f} listed -> offer ~{rounded} "
                  f"(gap {l.price_gbp-o.max_bid:.0f}) -- {l.title[:65]}")

    # Include all platforms that returned results (from Phase 1 tracking),
    # plus any platforms found in scored listings.
    _all_scored = list(top_rows) + list(near_miss or []) + list(avoid_rows or [])
    _scored_plats = {l.platform for l, _, _ in _all_scored}
    # Add platforms that were attempted in Phase 1 (n >= 0: includes zero-result runs;
    # n == -1 means adapter unavailable and is excluded).
    _phase1_plats = {p for p, n in _platform_results.items() if n >= 0}
    plats = list(_scored_plats | _phase1_plats or {"ebay"})
    # Sprint 9: build seen-before map for all output listings
    _all_output = list(top_rows) + list(near_miss or []) + list(avoid_rows or [])
    _seen_map: dict = {}
    for _l, _d, _o in _all_output:
        try:
            _seen_map[_l.item_id] = get_listing_seen_info(
                conn, _l.item_id, _l.vrm or ""
            )
        except Exception:
            pass

    _pipeline_runs_for_map = list_pipeline_runs(conn)
    report_path = generate_html_report(
        top_rows, near_miss, capital=cfg.capital,
        price_min=cfg.price_min, price_max=cfg.price_max,
        mode=(f"{_mode_label} ({ebay_env}) | auction={cfg.auction_only}"),
        target_margin=cfg.target_margin, stats=final_stats,
        platforms=plats, at_used=(at_comps is not None),
        avoid_rows=avoid_rows,
        enrich_stats={**_p3_enrich_stats,
                      "vrm_found_p45": _vrm_found_p45,
                      "enriched_p45":  _enriched_p45},
        platform_results=_platform_results,
        basket_item_ids={l.item_id for l, _, _ in basket_rows},
        basket_budget=cfg.capital,
        basket_spend=basket_spend,
        basket_profit=basket_profit,
        runtime_banner=dealerly_runtime_banner(),
        seen_map=_seen_map,
        pipeline_runs=_pipeline_runs_for_map,
    )
    _set_loading(100, "Finalizing report", done=True, report_path=report_path)
    print(f"\n[Report] {report_path}")
    # Always open the HTML report when enabled — loading-page redirect can fail
    # (file://, blocked fetch). User may get two tabs if loading was already open.
    if cfg.open_html_report:
        _open_path_preferred(report_path, line=1)
    if cfg.open_html_report:
        _open_postrun_dashboard(None)
    os.environ.pop("DEALERLY_LOADING_ALREADY_OPEN", None)

    append_deal_log(top_rows)
    print(f"[Log] {DEAL_LOG_PATH}")

    # Sprint 2: Google Sheets export (runs if GOOGLE_SHEET_ID is configured)
    if is_sheets_available():
        print("\n[Sheets] Exporting pipeline state...")
        ok = export_to_sheets(top_rows, conn)
        print(f"  {'OK' if ok else 'FAILED — check GOOGLE_SHEET_ID / service account JSON'}")

    if top_rows:
        _skip_wl = os.environ.get("DEALERLY_SKIP_WATCHLIST_PROMPT", "").strip().lower() in {
            "1", "true", "yes",
        }
        _wl_yes = False
        if not _skip_wl:
            try:
                _wl_yes = (
                    input("\nAdd BUY/OFFER listings to watchlist? (y/N): ").strip().lower()
                    == "y"
                )
            except EOFError:
                _wl_yes = False
        if _wl_yes:
            # v0.9.5.8: watchlist_add returns 'added'/'updated'/'unchanged'
            results = [watchlist_add(conn, l, d, o)
                       for l, d, o in top_rows if o.decision in ("BUY", "OFFER")]
            n_added   = results.count("added")
            n_updated = results.count("updated")
            n_same    = results.count("unchanged")
            print(f"  Watchlist: {n_added} new, {n_updated} updated, {n_same} unchanged."
                  f" View with --watchlist flag.")

    # v0.9.9: Write debug JSON log if --debug flag was set
    if cfg.debug_mode:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        _debug_log["total_elapsed_s"] = round(time.time() - _run_start, 1)
        _debug_log["near_miss_count"] = len(near_miss)
        _debug_log["api_budget"] = dict(_budget_counters)
        _debug_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _debug_path = REPORTS_DIR / f"debug_{_debug_ts}.json"
        _debug_path.write_text(
            json.dumps(_debug_log, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[Debug] Log written: {_debug_path}")
        
    # =================================================================
    # Automated AI Pipeline Report Generation
    # =================================================================
    try:
        # Assumes you run Dealerly from the root folder containing the 'prompts' dir
        report_md_path = Path("prompts/PIPELINE_REPORT.md")
        next_version_md_path = Path("prompts/NEXT_VERSION.md")
        report_md_path.parent.mkdir(parents=True, exist_ok=True)

        # Format platform status
        plats_str = []
        for p_name, n in _platform_results.items():
            if n > 0: plats_str.append(f"{p_name.capitalize()} (Success: {n})")
            elif n == 0: plats_str.append(f"{p_name.capitalize()} (Failed: 0 results)")
            else: plats_str.append(f"{p_name.capitalize()} (Unavailable)")
        plat_out = ", ".join(plats_str) if plats_str else "eBay (Legacy)"

        # Calculate totals
        total_gathered = len(listings)
        b = final_stats.get("buy", 0)
        o = final_stats.get("offer", 0)
        p = final_stats.get("pass", 0)
        a = final_stats.get("avoid_shock", 0)

        # Determine if there are active errors to flag for the AI
        error_lines = []
        for p_name, n in _platform_results.items():
            if n == 0:
                error_lines.append(f"* `{p_name}.py` - Platform returned 0 listings. Cloudflare challenge or auth issue likely.")

        errors_section = "\n".join(error_lines) if error_lines else "* No critical ingestion errors detected."

        # Build phase timing breakdown (delta from cumulative elapsed_s snapshots).
        phase_timing = _phase_timing_breakdown(_debug_log)
        phase_timing_lines = [
            f"* {k.capitalize()}: {v:.1f}s" for k, v in phase_timing.items() if v > 0
        ]
        if not phase_timing_lines:
            phase_timing_lines.append("* Timing unavailable")
        phase_timing_md = "\n".join(phase_timing_lines)

        # Continuous intelligence loop:
        # 1) deterministic directives from current run metrics
        # 2) optional AI refinement into concise sprint bullets
        heuristic_steps = _build_heuristic_next_steps(
            phase_timing=phase_timing,
            platform_results=_platform_results,
            vrm_found_pool=vrm_found_p4,
            candidate_limit=_PHASE4_CANDIDATE_LIMIT,
            mot_verified=mot_verified,
            buy_n=b,
            offer_n=o,
            obsidian_cache_hits=_obsidian_vrm_cache_hits,
            anpr_skips=_anpr_skips,
        )
        ai_steps = _ai_refine_next_steps(heuristic_steps)
        ai_steps_md = "\n".join(f"* {s}" for s in ai_steps)

        # Infer mode
        inferred_mode = "Dealer" if cfg.capital >= 5000 else "Flipper"

        _fb_total = _budget_counters.get("fb_total", 0)
        _fb_good  = _budget_counters.get("fb_titles_good", 0)
        _fb_mil   = _budget_counters.get("fb_mileage_found", 0)
        _fb_thumb = _budget_counters.get("fb_thumb_found", 0)
        _fb_quality_line = (
            f"* FB quality: {_fb_good}/{_fb_total} good titles, "
            f"{_fb_mil} mileage, {_fb_thumb} thumbnails "
            f"(cap={fb_max_listings()})"
        ) if _fb_total > 0 else "* FB quality: n/a (adapter skipped or zero results)"

        md_content = f"""# Pipeline Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}
**Mode:** {inferred_mode} | **Capital:** £{cfg.capital:.0f}
**Platforms Attempted:** {plat_out}

## Metrics
* Candidates Gathered: {total_gathered}
* VRMs Found: {vrm_found_p4} (in top {_PHASE4_CANDIDATE_LIMIT} pool)
* Obsidian VRM cache hits: {_obsidian_vrm_cache_hits}
* ANPR calls avoided (verified/cache paths): {_anpr_skips}
* ANPR calls made: {_budget_counters['anpr_calls']} (skipped budget: {_budget_counters['anpr_skipped_budget']}, verified: {_budget_counters['anpr_skipped_verified']}, cache: {_budget_counters['anpr_skipped_cache']})
* DVLA calls made: {_budget_counters['dvla_calls']} + {_budget_counters['dvla_validation_calls']} validations (skipped top-slice: {_budget_counters['dvla_skipped_top_slice']})
* AutoTrader scored candidates: phase2 {_budget_counters['autotrader_candidates_phase2']}, phase4 {_budget_counters['autotrader_candidates_phase4']}
* AutoTrader comp cache: {at_comps._cache_hits if at_comps else 0} hits / {(at_comps._cache_hits + at_comps._cache_misses) if at_comps else 0} lookups ({int(at_comps._cache_hits / max(at_comps._cache_hits + at_comps._cache_misses, 1) * 100) if at_comps else 0}% hit rate)
* DVSA Verified: {mot_verified}/{_PHASE4_CANDIDATE_LIMIT}
* Final Decisions: {b} BUY, {o} OFFER, {p} PASS, {a} AVOID
{_fb_quality_line}

## Phase Timings
{phase_timing_md}

## Errors & Exceptions
{errors_section}

## AI Next Steps Prompt
{ai_steps_md}
"""
        report_md_path.write_text(md_content, encoding="utf-8")
        next_md = _render_next_version_md(
            phase_timing=phase_timing,
            platform_results=_platform_results,
            candidate_limit=_PHASE4_CANDIDATE_LIMIT,
            vrm_found_pool=vrm_found_p4,
            mot_verified=mot_verified,
            decisions=final_stats,
            next_steps=ai_steps,
        )
        next_version_md_path.write_text(next_md, encoding="utf-8")
        print("[AI Sync] PIPELINE_REPORT.md updated for next AI session.")
        print("[AI Sync] NEXT_VERSION.md refreshed with next-build directives.")
    except Exception as e:
        print(f"[AI Sync] Failed to generate PIPELINE_REPORT.md: {e}")

    conn.close()
    print("\nDone.")

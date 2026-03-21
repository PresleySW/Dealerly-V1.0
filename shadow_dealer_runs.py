"""
dealerly/shadow_dealer_runs.py
==============================
Run **six** full pipeline batches into a **shadow** SQLite DB (default ``dealerly_shadow.db``):
capital tiers **£3k / £6k / £12k** (two runs each), **~30 enrichments**, fresh UK postcodes
(not Surrey/Nottingham test areas), **HTML reports generated** like a normal run.

Usage (from ``Dealerly 1.0`` project root)::

    python -m dealerly.shadow_dealer_runs

Smoke test Phase 1–3 only (no final report / Phase 4+)::

    python -m dealerly.shadow_dealer_runs --stop-after-phase 3 --one

After Phase 2 only (prelim scoring; skip VRM / MOT)::

    python -m dealerly.shadow_dealer_runs --stop-after-phase 2 --one

With AI offer messages::

    python -m dealerly.shadow_dealer_runs --with-ai

Custom DB::

    set DEALERLY_DB_FILE=dealerly_shadow2.db
    python -m dealerly.shadow_dealer_runs

Requires the same **``.env``** as ``python -m dealerly.cli`` (loads ``dealerly/.env`` first):
``EBAY_CLIENT_ID`` and ``EBAY_CLIENT_SECRET`` are mandatory for full runs. Optional: DVSA keys for MOT.

Uses multi-query presets only (no keyword prompt).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _ai_backend() -> str:
    from dealerly.offers import claude_api_key, openai_api_key

    if openai_api_key():
        return "openai"
    if claude_api_key():
        return "claude"
    return "none"


def _base_config(*, with_ai: bool) -> Any:
    from dealerly.config import (
        DEFAULT_EBAY_FEE_RATE,
        DEFAULT_NEAR_MISS_BAND,
        DEFAULT_PAYMENT_FEE_RATE,
        DEFAULT_RESALE_DISCOUNT,
        DEFAULT_TRANSPORT_BUFFER,
        Config,
    )

    ai = _ai_backend() if with_ai else "none"
    return Config(
        capital=3000.0,
        price_min=800,
        price_max=2800,
        target_margin=300.0,
        holding_cost=100.0,
        ebay_fee_rate=DEFAULT_EBAY_FEE_RATE,
        pay_fee_rate=DEFAULT_PAYMENT_FEE_RATE,
        admin_buffer=30.0,
        transport_buffer=DEFAULT_TRANSPORT_BUFFER,
        mot_mode="2",
        category_ids="9801",
        pages=4,
        near_miss_band=DEFAULT_NEAR_MISS_BAND,
        auction_only=False,
        store_comps=True,
        comps_ttl=12.0,
        resale_discount=DEFAULT_RESALE_DISCOUNT,
        preset="6",
        enrich_mode="1",
        enrich_n=30,
        sort="endingSoonest",
        misprice_ratio=0.90,
        require_comps=False,
        open_html_report=True,
        ai_backend=ai if ai in ("openai", "claude", "none", "local") else "openai",
        use_autotrader=True,
        generate_offer_msgs=with_ai and ai != "none",
        autotrader_postcode="BS1 5AH",
        input_mode="all",
        buyer_postcode="BS1 5AH",
        search_radius_miles=65,
        debug_mode=False,
    )


def _scenarios() -> List[Dict[str, Any]]:
    """
    Six full runs: two at £3k, two at £6k, two at £12k capital — different regions
    (South West, North West, Yorkshire, Wales, Scotland, North East).
    """
    return [
        {
            "label": "£3k — Bristol (SW)",
            "buyer_postcode": "BS1 5AH",
            "autotrader_postcode": "BS1 5AH",
            "capital": 3000.0,
            "price_min": 800,
            "price_max": 2900,
            "search_radius_miles": 58,
            "preset": "6",
            "pages": 4,
            "enrich_n": 30,
            "near_miss_band": 300.0,
        },
        {
            "label": "£3k — Manchester (NW)",
            "buyer_postcode": "M1 1AE",
            "autotrader_postcode": "M1 1AE",
            "capital": 3000.0,
            "price_min": 750,
            "price_max": 2800,
            "search_radius_miles": 62,
            "preset": "7",
            "pages": 4,
            "enrich_n": 30,
            "near_miss_band": 310.0,
        },
        {
            "label": "£6k — Leeds (Yorkshire)",
            "buyer_postcode": "LS1 4DY",
            "autotrader_postcode": "LS1 4DY",
            "capital": 6000.0,
            "price_min": 1200,
            "price_max": 5500,
            "search_radius_miles": 65,
            "preset": "8",
            "pages": 4,
            "enrich_n": 30,
            "near_miss_band": 320.0,
        },
        {
            "label": "£6k — Cardiff (Wales)",
            "buyer_postcode": "CF10 3AT",
            "autotrader_postcode": "CF10 3AT",
            "capital": 6000.0,
            "price_min": 1100,
            "price_max": 5600,
            "search_radius_miles": 55,
            "preset": "9",
            "pages": 4,
            "enrich_n": 30,
            "near_miss_band": 315.0,
        },
        {
            "label": "£12k — Edinburgh (Scotland)",
            "buyer_postcode": "EH1 1YZ",
            "autotrader_postcode": "EH1 1YZ",
            "capital": 12000.0,
            "price_min": 2000,
            "price_max": 10500,
            "search_radius_miles": 70,
            "preset": "10",
            "pages": 4,
            "enrich_n": 30,
            "near_miss_band": 350.0,
        },
        {
            "label": "£12k — Newcastle (NE)",
            "buyer_postcode": "NE1 4LP",
            "autotrader_postcode": "NE1 4LP",
            "capital": 12000.0,
            "price_min": 2200,
            "price_max": 11000,
            "search_radius_miles": 68,
            "preset": "8",
            "pages": 4,
            "enrich_n": 30,
            "near_miss_band": 340.0,
        },
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description="Six full shadow pipeline runs (separate DB).")
    ap.add_argument(
        "--with-ai",
        action="store_true",
        help="Enable offer-message generation (uses configured AI backend).",
    )
    ap.add_argument(
        "--db-file",
        default="",
        help="SQLite filename under the project data dir (default: dealerly_shadow.db).",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=3.0,
        help="Seconds between runs (rate-limit friendly).",
    )
    ap.add_argument(
        "--stop-after-phase",
        type=int,
        choices=(0, 2, 3),
        default=0,
        metavar="N",
        help="0=full pipeline; 2=after preliminary scoring; 3=after VRM enrichment (skips Phase 4+).",
    )
    ap.add_argument(
        "--one",
        action="store_true",
        help="Run only the first scenario (quick check).",
    )
    args = ap.parse_args()

    os.chdir(_ROOT)
    # Same .env resolution as dealerly.cli — without this, EBAY_* are empty and every run fails.
    from dealerly.cli import _load_env

    _load_env()

    if not (
        os.environ.get("EBAY_CLIENT_ID", "").strip()
        and os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    ):
        print(
            "\n[shadow] Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET.\n"
            f"  Add them to: {_ROOT / 'dealerly' / '.env'}\n"
            "  (same file the main CLI uses — see eBay developer app / OAuth keys.)\n"
        )
        sys.exit(1)

    db_name = (args.db_file or "dealerly_shadow.db").strip()
    os.environ["DEALERLY_DB_FILE"] = db_name
    os.environ["DEALERLY_SKIP_WATCHLIST_PROMPT"] = "1"
    # Full runs: allow loading screen + post-run report/dashboard like normal CLI
    os.environ.pop("DEALERLY_DISABLE_POSTRUN_DASHBOARD", None)
    os.environ["DEALERLY_LOADING_ALREADY_OPEN"] = "0"

    from dealerly.config import DB_PATH, default_holding_cost, default_target_margin
    from dealerly.db import init_db
    from dealerly.pipeline import run

    init_db(DB_PATH)
    scenarios = _scenarios()[:1] if args.one else _scenarios()
    _early = args.stop_after_phase in (2, 3)
    print(f"[shadow] Using DB: {DB_PATH}")
    print(
        f"[shadow] Runs: {len(scenarios)} | enrich_n≈30 | "
        f"HTML loading={'OFF' if _early else 'ON'} | "
        f"stop_after_phase={args.stop_after_phase} | AI msgs: {args.with_ai}\n"
    )

    base = _base_config(with_ai=args.with_ai)
    base = replace(
        base,
        stop_after_phase=args.stop_after_phase,
        open_html_report=False if _early else base.open_html_report,
    )

    for i, spec in enumerate(scenarios, 1):
        label = spec.pop("label")
        cfg = replace(base, **spec)
        cfg = replace(
            cfg,
            holding_cost=default_holding_cost(cfg.capital),
            target_margin=default_target_margin(cfg.capital),
        )
        print("=" * 60)
        print(f"  Run {i}/{len(scenarios)} — {label}")
        print(
            f"  £{cfg.price_min}–£{cfg.price_max} | cap £{cfg.capital:.0f} | "
            f"margin £{cfg.target_margin:.0f} | {cfg.buyer_postcode} ({cfg.search_radius_miles}mi) | "
            f"preset {cfg.preset} | pages {cfg.pages} | enrich {cfg.enrich_n}"
        )
        print("=" * 60)
        try:
            run(cfg)
        except KeyboardInterrupt:
            print("\n[shadow] Stopped by user.")
            raise
        except Exception as exc:
            print(f"[shadow] Run {i} failed: {type(exc).__name__}: {exc}")
        if i < len(scenarios) and args.sleep > 0:
            time.sleep(args.sleep)

    print(f"\n[shadow] Done. DB: {DB_PATH}")
    if args.stop_after_phase:
        print(
            f"  Early stop was Phase {args.stop_after_phase} — "
            "no HTML report. Use --stop-after-phase 0 for full runs."
        )
    else:
        print("  Reports under reports/report_*.html")
        print("  UK map (PowerShell):")
        print(f'    $env:DEALERLY_DB_FILE="{db_name}"; python -m dealerly.cli --stats-map')


if __name__ == "__main__":
    main()

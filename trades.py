"""
dealerly/trades.py
==================
Sprint 1 (PDF Rollout Plan): Trade outcome logging system.
Sprint 2 (PDF Rollout Plan): SWAutos integration — bulk CSV import,
  demo seed (Meriva KT06YNX), Obsidian trade notes.

Records actual flip results (buy price, sell price, repair costs, days to
sell) so Dealerly can track prediction accuracy and calibrate its models
over time.

Supports:
  - Manual entry for pre-Dealerly flips (e.g. the Meriva KT06YNX)
  - Bulk CSV import from dealer/personal history (--import-trades)
  - Demo seed: Meriva KT06YNX SWAutos flip (--seed-demo-trades)
  - Obsidian trade notes in Dealerly_Vault/Trades/
  - Accuracy summary for Sprint 3 (Prediction vs Outcome analysis)
"""
from __future__ import annotations

import csv as _csv
import sqlite3
from datetime import date as _date
from pathlib import Path
from typing import Optional

from dealerly.config import DB_PATH, obsidian_vault_path
from dealerly.db import db_connect, init_db, log_completed_trade, list_completed_trades, get_trades_accuracy_summary
from dealerly.calibration import prediction_vs_outcome, format_prediction_vs_outcome
from dealerly.utils import console_safe


# ---------------------------------------------------------------------------
# Interactive CLI entry
# ---------------------------------------------------------------------------

def _prompt(label: str, default: str = "", required: bool = False) -> str:
    """Prompt the user for a value with an optional default."""
    hint = f" [{default}]" if default else (" (required)" if required else " (optional, Enter to skip)")
    while True:
        val = input(f"  {label}{hint}: ").strip()
        if val:
            return val
        if default:
            return default
        if not required:
            return ""
        print("  This field is required.")


def _prompt_float(label: str, default: Optional[float] = None, required: bool = False) -> Optional[float]:
    hint = f" [{default:.2f}]" if default is not None else (" (required)" if required else " (optional, Enter=0)")
    while True:
        raw = input(f"  {label}{hint}: ").strip()
        if not raw:
            if default is not None:
                return default
            return 0.0 if not required else None  # type: ignore[return-value]
        try:
            return float(raw.replace("£", "").replace(",", ""))
        except ValueError:
            print("  Enter a number (e.g. 1250 or 1250.50).")


def _prompt_int(label: str) -> Optional[int]:
    raw = input(f"  {label} (optional, Enter to skip): ").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def log_trade_interactive(db_path=DB_PATH) -> None:
    """
    Interactive CLI flow to log a completed trade.

    Usage (from CLI):  python -m dealerly.cli --log-trade
    Usage (direct):    from dealerly.trades import log_trade_interactive; log_trade_interactive()
    """
    init_db(db_path)
    conn = db_connect(db_path)

    print("\n" + "=" * 60)
    print("  DEALERLY - Log Completed Trade")
    print("  Record an actual flip outcome to improve model accuracy.")
    print("=" * 60)

    vrm = _prompt("VRM (number plate, e.g. KT06YNX)", required=True).upper().replace(" ", "")
    make = _prompt("Make (e.g. Vauxhall)")
    model = _prompt("Model (e.g. Meriva)")
    year = _prompt_int("Year (e.g. 2006)")
    mileage = _prompt_int("Mileage at purchase (e.g. 29000)")

    print()
    buy_price = _prompt_float("Buy price (£)", required=True)
    sell_price = _prompt_float("Sell price (£)", required=True)
    repair_costs = _prompt_float("Repair / prep costs (£)", default=0.0)
    other_costs = _prompt_float("Other costs — transport, equipment, etc. (£)", default=0.0)

    print()
    days_to_sell = _prompt_int("Days from purchase to sale")
    platform_sold = _prompt("Platform sold on (ebay / facebook / motors / private)")
    condition_notes = _prompt("Condition notes (e.g. paint tattered, solid service history)")

    print()
    predicted_raw = input("  Dealerly predicted profit (£) — Enter to skip if unknown: ").strip()
    predicted_profit: Optional[float] = None
    if predicted_raw:
        try:
            predicted_profit = float(predicted_raw.replace("£", ""))
        except ValueError:
            pass

    realised = (sell_price or 0.0) - (buy_price or 0.0) - (repair_costs or 0.0) - (other_costs or 0.0)

    print()
    print("  --- Summary ------------------------------------")
    print(f"  VRM:          {vrm}   {make or ''} {model or ''} {year or ''}")
    print(f"  Buy:          £{buy_price:,.0f}   Sell: £{sell_price:,.0f}")
    print(f"  Repairs:      £{repair_costs:,.0f}   Other: £{other_costs:,.0f}")
    print(f"  Realised:     £{realised:,.0f}")
    if predicted_profit is not None:
        error = realised - predicted_profit
        sign = "+" if error >= 0 else ""
        print(f"  Prediction:   £{predicted_profit:,.0f}  ->  error {sign}£{error:,.0f}")
    print(f"  Days to sell: {days_to_sell or 'n/a'}   Platform: {platform_sold or 'n/a'}")
    print("  -----------------------------------------------")

    confirm = input("\n  Save this trade? (y/N): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        conn.close()
        return

    trade_id = log_completed_trade(
        conn,
        vrm=vrm, make=make or None, model=model or None,
        year=year, mileage=mileage,
        buy_price=buy_price, sell_price=sell_price,
        repair_costs=repair_costs, other_costs=other_costs,
        days_to_sell=days_to_sell, platform_sold=platform_sold or None,
        condition_notes=condition_notes or None,
        predicted_profit=predicted_profit,
        source="manual",
    )
    conn.close()
    print(f"\n  [OK] Trade #{trade_id} logged. Run --trades to see accuracy summary.\n")


# ---------------------------------------------------------------------------
# Summary / report
# ---------------------------------------------------------------------------

def print_trades_summary(db_path=DB_PATH) -> None:
    """Print a human-readable accuracy report to stdout."""
    init_db(db_path)
    conn = db_connect(db_path)

    trades = list_completed_trades(conn)
    summary = get_trades_accuracy_summary(conn)
    conn.close()

    if not trades:
        print("\n  No completed trades logged yet. Use --log-trade to add one.\n")
        return

    print("\n" + "=" * 60)
    print("  DEALERLY - Completed Trades & Accuracy")
    print("=" * 60)

    for t in trades:
        vrm_str = t["vrm"]
        desc = " ".join(filter(None, [t.get("make"), t.get("model"), str(t.get("year") or "")]))
        rp = t["realised_profit"]
        pe = t.get("prediction_error")
        error_str = f"  err {'+' if pe >= 0 else ''}£{pe:,.0f}" if pe is not None else ""
        days_str = f"  {t['days_to_sell']}d" if t.get("days_to_sell") else ""
        print(
            console_safe(
                f"  {vrm_str:<10s}  {desc:<28s}  "
                f"buy £{t['buy_price']:,.0f}  sell £{t['sell_price']:,.0f}  "
                f"profit £{rp:,.0f}{error_str}{days_str}"
            )
        )

    print("\n  --- Totals -------------------------------------")
    print(f"  Trades logged:     {summary.get('total_trades', 0)}")
    print(f"  Total realised:    £{summary.get('total_realised') or 0:,.0f}")
    print(f"  Avg per trade:     £{summary.get('avg_realised') or 0:,.0f}")
    print(f"  Best / Worst:      £{summary.get('best_trade') or 0:,.0f} / £{summary.get('worst_trade') or 0:,.0f}")
    print(f"  Avg days to sell:  {summary.get('avg_days_to_sell') or 'n/a'}")

    n_pred = summary.get("trades_with_prediction", 0)
    if n_pred:
        avg_err = summary.get("avg_prediction_error") or 0
        abs_err = summary.get("avg_abs_error") or 0
        over = summary.get("prediction_over_count", 0)
        sign = "+" if avg_err >= 0 else ""
        print(f"\n  --- Prediction Accuracy ({n_pred} trade(s) with forecast) ---")
        print(f"  Avg error:         {sign}£{avg_err:,.0f}  (positive = model underestimated)")
        print(f"  Avg abs error:     £{abs_err:,.0f}")
        print(f"  Beat forecast:     {over}/{n_pred} trades")

    # Sprint 6: cross-reference deal log CSV with completed trades on VRM
    try:
        conn2 = db_connect(DB_PATH)
        matches = prediction_vs_outcome(conn2)
        conn2.close()
        if matches:
            print(console_safe(format_prediction_vs_outcome(matches)))
        elif not n_pred:
            print("\n  (No pipeline-vs-trade VRM matches found - run a pipeline first)")
    except Exception:
        pass
    print()


# ---------------------------------------------------------------------------
# Sprint 2: bulk CSV import
# ---------------------------------------------------------------------------

def import_trades_from_csv(csv_path: str, db_path: str = DB_PATH) -> int:
    """
    Bulk-import completed trades from a CSV file.

    Required columns: vrm, buy_price, sell_price
    Optional columns: make, model, year, mileage, repair_costs, other_costs,
                      days_to_sell, platform_sold, condition_notes,
                      predicted_profit, source

    Returns the number of rows inserted.
    """
    init_db(db_path)
    conn = db_connect(db_path)
    inserted = 0

    def _f(row: dict, key: str, default: float = 0.0) -> float:
        raw = row.get(key, "").strip().replace("£", "").replace(",", "")
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    def _i(row: dict, key: str) -> Optional[int]:
        raw = row.get(key, "").strip()
        return int(raw) if raw.lstrip("-").isdigit() else None

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            reader = _csv.DictReader(fh)
            for row in reader:
                vrm = row.get("vrm", "").strip().upper().replace(" ", "")
                buy_price = _f(row, "buy_price")
                sell_price = _f(row, "sell_price")
                if not vrm or not buy_price or not sell_price:
                    continue
                pred_raw = row.get("predicted_profit", "").strip().replace("£", "")
                predicted: Optional[float] = float(pred_raw) if pred_raw else None
                try:
                    log_completed_trade(
                        conn,
                        vrm=vrm,
                        make=row.get("make", "").strip() or None,
                        model=row.get("model", "").strip() or None,
                        year=_i(row, "year"),
                        mileage=_i(row, "mileage"),
                        buy_price=buy_price,
                        sell_price=sell_price,
                        repair_costs=_f(row, "repair_costs"),
                        other_costs=_f(row, "other_costs"),
                        days_to_sell=_i(row, "days_to_sell"),
                        platform_sold=row.get("platform_sold", "").strip() or None,
                        condition_notes=row.get("condition_notes", "").strip() or None,
                        predicted_profit=predicted,
                        source=row.get("source", "csv").strip() or "csv",
                    )
                    inserted += 1
                except Exception:
                    continue
    finally:
        conn.close()

    return inserted


# ---------------------------------------------------------------------------
# Sprint 2: demo seed (Meriva KT06YNX — SWAutos first flip)
# ---------------------------------------------------------------------------

def seed_demo_trades(db_path: str = DB_PATH) -> bool:
    """
    Seed the Meriva KT06YNX SWAutos flip as the first completed trade.

    Idempotent — no-ops if the VRM is already in completed_trades.
    Returns True if the record was inserted, False if it already existed.
    """
    init_db(db_path)
    conn = db_connect(db_path)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE vrm=?", ("KT06YNX",)
        )
        if (cur.fetchone() or [0])[0] > 0:
            return False
        log_completed_trade(
            conn,
            vrm="KT06YNX",
            make="Vauxhall",
            model="Meriva",
            year=2006,
            mileage=29000,
            buy_price=900.0,
            sell_price=1695.0,
            repair_costs=200.0,
            other_costs=80.0,
            days_to_sell=None,
            platform_sold="private",
            condition_notes="Paint tattered, solid service history",
            predicted_profit=None,
            source="swautosautos_seed",
        )
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sprint 2: Obsidian trade notes
# ---------------------------------------------------------------------------

def write_trade_to_obsidian(trade: dict, vault: Optional[Path] = None) -> Optional[Path]:
    """
    Write a completed trade as a markdown note to Dealerly_Vault/Trades/.

    Args:
        trade: dict as returned by list_completed_trades() (keys: vrm, make,
               model, year, buy_price, sell_price, repair_costs, other_costs,
               realised_profit, days_to_sell, platform_sold, condition_notes,
               predicted_profit, prediction_error, logged_at)
        vault: override vault path (default: obsidian_vault_path())

    Returns the Path written, or None if the vault is unavailable.
    """
    vault_root = vault or obsidian_vault_path()
    try:
        trades_dir = vault_root / "Trades"
        trades_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    vrm = trade.get("vrm", "UNKNOWN")
    make = trade.get("make") or ""
    model = trade.get("model") or ""
    year = trade.get("year") or ""
    desc = " ".join(filter(None, [str(make), str(model), str(year)])) or vrm
    buy = trade.get("buy_price") or 0.0
    sell = trade.get("sell_price") or 0.0
    repairs = trade.get("repair_costs") or 0.0
    other = trade.get("other_costs") or 0.0
    profit = trade.get("realised_profit") or (sell - buy - repairs - other)
    days = trade.get("days_to_sell")
    platform = trade.get("platform_sold") or "n/a"
    notes = trade.get("condition_notes") or ""
    pred = trade.get("predicted_profit")
    err = trade.get("prediction_error")
    logged = str(trade.get("logged_at", _date.today()))[:10]

    pred_line = ""
    if pred is not None:
        sign = "+" if (err or 0) >= 0 else ""
        pred_line = f"\n**Predicted profit:** £{pred:,.0f}  **Model error:** {sign}£{(err or 0):,.0f}"

    vrm_link = f"[[Database/VRMs/{vrm}]]" if (vault_root / "Database" / "VRMs" / f"{vrm}.md").exists() else vrm

    content = (
        f"# Trade: {vrm} — {desc}\n\n"
        f"**Date logged:** {logged}  \n"
        f"**Buy:** £{buy:,.0f} | **Sell:** £{sell:,.0f} | "
        f"**Repairs:** £{repairs:,.0f} | **Other:** £{other:,.0f}  \n"
        f"**Realised profit:** £{profit:,.0f}  \n"
        f"**Days to sell:** {days or 'n/a'} | **Platform:** {platform}{pred_line}  \n"
    )
    if notes:
        content += f"\n**Condition:** {notes}  \n"
    content += f"\n## Links\n\n- {vrm_link}\n"

    slug = logged.replace("-", "") + f"_{vrm}"
    note_path = trades_dir / f"{slug}.md"
    note_path.write_text(content, encoding="utf-8")
    return note_path

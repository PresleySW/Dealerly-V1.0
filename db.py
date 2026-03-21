"""
dealerly/db.py
==============
SQLite persistence layer.

v0.9.0 additions:
  - price_observations table (analytics)
  - leads table (CRM workflow)
  - lead_history table (status change audit trail)
  - posting_drafts table (multi-platform posting)
  - dealer_profiles table (B2B network)
  - dealer_inventory table (B2B network)
  - dealer_messages table (B2B network)
  - dvla_cache table (DVLA vehicle enquiry cache)

UK stats map:
  - postcode_geo_cache — resolved lat/lon for buyer postcodes
  - pipeline_runs — one row per pipeline run (location, radius, new/repeat obs counts)

All functions accept an explicit sqlite3.Connection.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from dealerly.config import DB_PATH
from dealerly.utils import now_utc_iso

if TYPE_CHECKING:
    from dealerly.models import DealInput, DealOutput, Listing


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def db_connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with foreign keys enabled."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db(db_path: Path = DB_PATH) -> None:
    """Create all tables and indexes if they do not already exist."""
    conn = db_connect(db_path)
    conn.executescript("""
    -- =================================================================
    -- EXISTING TABLES
    -- =================================================================

    CREATE TABLE IF NOT EXISTS comps (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_key  TEXT    NOT NULL,
        source       TEXT    NOT NULL,
        price        REAL    NOT NULL,
        year         INTEGER,
        mileage      INTEGER,
        location     TEXT,
        date_seen    TEXT,
        url          TEXT
    );

    CREATE TABLE IF NOT EXISTS mot_cache (
        vrm          TEXT PRIMARY KEY,
        fetched_at   TEXT NOT NULL,
        provider     TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ai_cache (
        cache_key    TEXT PRIMARY KEY,
        fetched_at   TEXT NOT NULL,
        model        TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS watchlist (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        added_at          TEXT NOT NULL,
        platform          TEXT NOT NULL,
        item_id           TEXT NOT NULL UNIQUE,
        title             TEXT,
        url               TEXT,
        vrm               TEXT,
        buy_price         REAL,
        max_bid           REAL,
        expected_profit   REAL,
        decision          TEXT,
        notes             TEXT,
        status            TEXT DEFAULT 'watching',
        actual_buy_price  REAL,
        actual_sale_price REAL,
        actual_repairs    REAL,
        actual_days_to_sell INTEGER,
        realised_profit   REAL
    );

    CREATE TABLE IF NOT EXISTS autotrader_comps (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_key TEXT NOT NULL,
        price       REAL NOT NULL,
        year        INTEGER,
        mileage     INTEGER,
        location    TEXT,
        date_seen   TEXT,
        url         TEXT
    );

    -- =================================================================
    -- NEW v0.9.0 TABLES
    -- =================================================================

    -- DVLA vehicle enquiry cache
    CREATE TABLE IF NOT EXISTS dvla_cache (
        vrm          TEXT PRIMARY KEY,
        fetched_at   TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );

    -- Price observations for trend analytics
    CREATE TABLE IF NOT EXISTS price_observations (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_key  TEXT    NOT NULL,
        platform     TEXT    NOT NULL,
        price        REAL    NOT NULL,
        mileage      INTEGER,
        year         INTEGER,
        location     TEXT,
        observed_at  TEXT    NOT NULL,
        item_id      TEXT,
        url          TEXT
    );

    -- CRM leads (deal pipeline)
    CREATE TABLE IF NOT EXISTS leads (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id             TEXT NOT NULL,
        platform            TEXT NOT NULL,
        title               TEXT,
        vrm                 TEXT,
        url                 TEXT,
        buy_price           REAL,
        max_bid             REAL,
        expected_profit     REAL,
        actual_buy_price    REAL,
        actual_sale_price   REAL,
        actual_repairs      REAL,
        actual_days_to_sell INTEGER,
        realised_profit     REAL,
        status              TEXT DEFAULT 'sourced',
        decision            TEXT,
        notes               TEXT,
        seller_name         TEXT DEFAULT '',
        seller_contact      TEXT DEFAULT '',
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        contacted_at        TEXT,
        bought_at           TEXT,
        listed_at           TEXT,
        sold_at             TEXT,
        next_action         TEXT DEFAULT '',
        next_action_due     TEXT,
        tags                TEXT DEFAULT '',
        offer_message       TEXT DEFAULT '',
        listing_draft_id    INTEGER,
        UNIQUE(item_id, platform)
    );

    -- Lead status change audit trail
    CREATE TABLE IF NOT EXISTS lead_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id     INTEGER NOT NULL,
        old_status  TEXT NOT NULL,
        new_status  TEXT NOT NULL,
        changed_at  TEXT NOT NULL,
        changed_by  TEXT DEFAULT 'system',
        notes       TEXT DEFAULT '',
        FOREIGN KEY (lead_id) REFERENCES leads(id)
    );

    -- Multi-platform posting drafts
    CREATE TABLE IF NOT EXISTS posting_drafts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id             INTEGER,
        vrm                 TEXT,
        title               TEXT NOT NULL,
        description         TEXT,
        price_gbp           REAL,
        suggested_price     REAL,
        ai_description      TEXT,
        ai_bullet_points    TEXT,
        ai_price_suggestion REAL,
        ai_image_tags       TEXT,
        platforms           TEXT DEFAULT '',
        ebay_posted         INTEGER DEFAULT 0,
        facebook_posted     INTEGER DEFAULT 0,
        autotrader_posted   INTEGER DEFAULT 0,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        status              TEXT DEFAULT 'draft',
        image_paths         TEXT DEFAULT '',
        FOREIGN KEY (lead_id) REFERENCES leads(id)
    );

    -- Dealer profiles (B2B network)
    CREATE TABLE IF NOT EXISTS dealer_profiles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        location    TEXT,
        postcode    TEXT,
        phone       TEXT,
        email       TEXT,
        specialties TEXT DEFAULT '',
        rating      REAL DEFAULT 0.0,
        trade_count INTEGER DEFAULT 0,
        created_at  TEXT NOT NULL,
        is_active   INTEGER DEFAULT 1
    );

    -- Dealer inventory (B2B network)
    CREATE TABLE IF NOT EXISTS dealer_inventory (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        dealer_id       INTEGER NOT NULL,
        vrm             TEXT,
        title           TEXT NOT NULL,
        description     TEXT,
        price_gbp       REAL,
        trade_price     REAL,
        condition_notes TEXT,
        mot_expiry      TEXT,
        mileage         INTEGER,
        year            INTEGER,
        fuel_type       TEXT,
        colour          TEXT,
        image_urls      TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        status          TEXT DEFAULT 'available',
        views           INTEGER DEFAULT 0,
        FOREIGN KEY (dealer_id) REFERENCES dealer_profiles(id)
    );

    -- =================================================================
    -- v0.9.9: Verified vehicles (permanent DVSA-confirmed records)
    -- =================================================================
    -- Distinct from mot_cache (24hr TTL) — these records persist permanently
    -- and serve as a local dataset of DVSA-confirmed vehicles across runs.
    CREATE TABLE IF NOT EXISTS verified_vehicles (
        vrm              TEXT PRIMARY KEY,
        make             TEXT,
        model            TEXT,
        fuel_type        TEXT,
        colour           TEXT,
        first_used_date  TEXT,
        last_mot_date    TEXT,
        last_mot_result  TEXT,
        total_tests      INTEGER,
        advisory_count   INTEGER,
        fail_count       INTEGER,
        last_mileage     INTEGER,
        mot_json         TEXT NOT NULL,
        first_seen_date  TEXT NOT NULL,
        last_updated     TEXT NOT NULL
    );

    -- Dealer messages (B2B network)
    CREATE TABLE IF NOT EXISTS dealer_messages (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        from_dealer_id    INTEGER NOT NULL,
        to_dealer_id      INTEGER NOT NULL,
        inventory_item_id INTEGER,
        subject           TEXT,
        body              TEXT,
        created_at        TEXT NOT NULL,
        read              INTEGER DEFAULT 0,
        FOREIGN KEY (from_dealer_id) REFERENCES dealer_profiles(id),
        FOREIGN KEY (to_dealer_id) REFERENCES dealer_profiles(id),
        FOREIGN KEY (inventory_item_id) REFERENCES dealer_inventory(id)
    );

    -- =================================================================
    -- INDEXES
    -- =================================================================

    CREATE INDEX IF NOT EXISTS idx_comps_key       ON comps(vehicle_key);
    CREATE INDEX IF NOT EXISTS idx_at_key          ON autotrader_comps(vehicle_key);
    CREATE INDEX IF NOT EXISTS idx_watchlist_item   ON watchlist(item_id);
    CREATE INDEX IF NOT EXISTS idx_price_obs_key    ON price_observations(vehicle_key);
    CREATE INDEX IF NOT EXISTS idx_price_obs_date   ON price_observations(observed_at);
    CREATE INDEX IF NOT EXISTS idx_leads_status     ON leads(status);
    CREATE INDEX IF NOT EXISTS idx_leads_item       ON leads(item_id, platform);
    CREATE INDEX IF NOT EXISTS idx_lead_history     ON lead_history(lead_id);
    CREATE INDEX IF NOT EXISTS idx_posting_lead     ON posting_drafts(lead_id);
    CREATE INDEX IF NOT EXISTS idx_dealer_inv       ON dealer_inventory(dealer_id);
    CREATE INDEX IF NOT EXISTS idx_dealer_inv_status ON dealer_inventory(status);
    CREATE INDEX IF NOT EXISTS idx_dealer_msg_to    ON dealer_messages(to_dealer_id, read);
    CREATE INDEX IF NOT EXISTS idx_verified_vehicles ON verified_vehicles(last_updated);

    -- =================================================================
    -- v0.9.9: item_vrm — persist VRM findings per listing across runs
    -- =================================================================
    -- Stores the best VRM found for each item_id (by ANPR, regex, or DVLA).
    -- Checked before running enrichment — saves Plate Recognizer credits on
    -- repeat runs of the same listings.
    CREATE TABLE IF NOT EXISTS item_vrm (
        item_id     TEXT PRIMARY KEY,
        vrm         TEXT NOT NULL,
        source      TEXT NOT NULL,
        confidence  REAL NOT NULL,
        found_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_item_vrm_found ON item_vrm(found_at);

    -- =================================================================
    -- v1.0: completed_trades — actual outcome logging (Sprint 1 PDF)
    -- =================================================================
    -- Records real flip outcomes so Dealerly can compare predictions vs
    -- reality and gradually calibrate the repair / resale models.
    -- Supports both Dealerly-led trades (lead_id not null) and manual
    -- entries for pre-existing flips (e.g. cars bought before Dealerly).
    CREATE TABLE IF NOT EXISTS completed_trades (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at       TEXT    NOT NULL,
        vrm               TEXT    NOT NULL,
        make              TEXT,
        model             TEXT,
        year              INTEGER,
        mileage           INTEGER,
        buy_price         REAL    NOT NULL,
        sell_price        REAL    NOT NULL,
        repair_costs      REAL    NOT NULL DEFAULT 0.0,
        other_costs       REAL    NOT NULL DEFAULT 0.0,
        days_to_sell      INTEGER,
        platform_sold     TEXT,
        condition_notes   TEXT,
        predicted_profit  REAL,           -- from Dealerly at source time
        realised_profit   REAL    NOT NULL,  -- sell - buy - repair - other
        prediction_error  REAL,            -- realised - predicted (null if no prediction)
        lead_id           INTEGER REFERENCES leads(id),
        source            TEXT    NOT NULL DEFAULT 'manual'
    );
    CREATE INDEX IF NOT EXISTS idx_trades_vrm      ON completed_trades(vrm);
    CREATE INDEX IF NOT EXISTS idx_trades_recorded ON completed_trades(recorded_at);

    -- =================================================================
    -- UK stats map: geocoded postcodes + one row per pipeline run
    -- =================================================================
    CREATE TABLE IF NOT EXISTS postcode_geo_cache (
        postcode    TEXT PRIMARY KEY,
        lat         REAL NOT NULL,
        lon         REAL NOT NULL,
        resolved_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS pipeline_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT NOT NULL,
        buyer_postcode TEXT NOT NULL,
        lat REAL,
        lon REAL,
        search_radius_miles INTEGER NOT NULL,
        new_observations INTEGER NOT NULL,
        repeat_observations INTEGER NOT NULL,
        total_price_observations_in_db INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pipeline_runs_at ON pipeline_runs(run_at DESC);
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Comps (eBay) — existing
# ---------------------------------------------------------------------------

def insert_comps(
    conn: sqlite3.Connection,
    vehicle_key: str,
    rows: List[Tuple],
    source: str = "ebay_active",
) -> None:
    """
    Persist eBay comp rows. Uses one BEGIN IMMEDIATE transaction per batch so
    Windows / IDE / concurrent readers do not hit per-row autocommit lock churn.
    Retries on database locked with exponential backoff (Spyder, AV, WAL).
    """
    import time as _time

    if not rows:
        return

    seen = now_utc_iso()
    params: List[Tuple] = [
        (vehicle_key, source, float(p), y, m, loc, seen, url)
        for (p, y, m, loc, url) in rows
    ]
    max_attempts = 12
    for attempt in range(max_attempts):
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany(
                    "INSERT INTO comps (vehicle_key, source, price, year, mileage, "
                    "location, date_seen, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
                conn.commit()
                return
            except BaseException:
                conn.rollback()
                raise
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if ("locked" in msg or "busy" in msg) and attempt < max_attempts - 1:
                _time.sleep(min(3.0, 0.05 * (2 ** attempt)))
                continue
            raise


def load_recent_comps(
    conn: sqlite3.Connection,
    vehicle_key: str,
    ttl_hours: float,
    source: str = "ebay_active",
) -> List[float]:
    cur = conn.cursor()
    cur.execute(
        "SELECT date_seen, price FROM comps"
        " WHERE vehicle_key = ? AND source = ?"
        " ORDER BY date_seen DESC LIMIT 200",
        (vehicle_key, source),
    )
    now_dt = datetime.now(timezone.utc)
    out: List[float] = []
    for date_seen, price in cur.fetchall():
        try:
            age_h = (
                now_dt - datetime.fromisoformat(
                    str(date_seen).replace("Z", "+00:00"))
            ).total_seconds() / 3600.0
            if age_h <= ttl_hours:
                out.append(float(price))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Comps (AutoTrader) — existing
# ---------------------------------------------------------------------------

def load_recent_autotrader_comps(
    conn: sqlite3.Connection,
    vehicle_key: str,
    ttl_hours: float,
) -> List[float]:
    cur = conn.cursor()
    cur.execute(
        "SELECT date_seen, price FROM autotrader_comps"
        " WHERE vehicle_key = ?"
        " ORDER BY date_seen DESC LIMIT 100",
        (vehicle_key,),
    )
    now_dt = datetime.now(timezone.utc)
    out: List[float] = []
    for date_seen, price in cur.fetchall():
        try:
            age_h = (
                now_dt - datetime.fromisoformat(
                    str(date_seen).replace("Z", "+00:00"))
            ).total_seconds() / 3600.0
            if age_h <= ttl_hours:
                out.append(float(price))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Watchlist — existing
# ---------------------------------------------------------------------------

def watchlist_add(
    conn: sqlite3.Connection,
    listing: "Listing",
    deal: "DealInput",
    out: "DealOutput",
) -> str:
    """
    UPSERT a listing into the watchlist.

    Returns:
        'added'     — new item inserted
        'updated'   — existing item had changed price/decision/max_bid, updated
        'unchanged' — item already exists with same data, no action taken

    v0.9.5.8: changed from INSERT-or-return-False to a three-way UPSERT so
    re-runs reflect price changes and decision flips instead of silently
    doing nothing. The pipeline can now print "X new, Y updated, Z unchanged"
    instead of the misleading "Added 0 items".
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, buy_price, max_bid, decision FROM watchlist WHERE item_id = ?",
        (listing.item_id,),
    )
    existing = cur.fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO watchlist"
            " (added_at, platform, item_id, title, url, vrm,"
            "  buy_price, max_bid, expected_profit, decision, notes, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_utc_iso(), listing.platform, listing.item_id,
                listing.title[:200], listing.url, listing.vrm,
                listing.price_gbp, out.max_bid, out.expected_profit,
                out.decision, out.notes[:500], "watching",
            ),
        )
        conn.commit()
        return "added"

    ex_id, ex_price, ex_bid, ex_dec = existing
    changed = (
        abs((ex_price or 0) - listing.price_gbp) > 1
        or abs((ex_bid or 0) - out.max_bid) > 1
        or ex_dec != out.decision
    )
    if changed:
        conn.execute(
            "UPDATE watchlist SET buy_price=?, max_bid=?, expected_profit=?,"
            " decision=?, notes=?, vrm=? WHERE id=?",
            (
                listing.price_gbp, out.max_bid, out.expected_profit,
                out.decision, out.notes[:500], listing.vrm, ex_id,
            ),
        )
        conn.commit()
        return "updated"

    return "unchanged"


def watchlist_list(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, added_at, platform, item_id, title, vrm, buy_price, "
        "max_bid, expected_profit, decision, status, url"
        " FROM watchlist WHERE status = 'watching'"
        " ORDER BY expected_profit DESC"
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# AI cache — existing
# ---------------------------------------------------------------------------

def ai_cache_get(conn: sqlite3.Connection, key: str) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("SELECT payload_json FROM ai_cache WHERE cache_key = ?", (key,))
    row = cur.fetchone()
    return json.loads(row[0]) if row else None


def ai_cache_put(
    conn: sqlite3.Connection, key: str, model: str,
    payload: Dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO ai_cache (cache_key, fetched_at, model, payload_json)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(cache_key) DO UPDATE SET"
        "   fetched_at=excluded.fetched_at, model=excluded.model,"
        "   payload_json=excluded.payload_json",
        (key, now_utc_iso(), model, json.dumps(payload)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# DVLA cache (v0.9.0)
# ---------------------------------------------------------------------------

def dvla_cache_get(
    conn: sqlite3.Connection, vrm: str, ttl_hours: float = 168.0,
) -> Optional[Dict[str, Any]]:
    """Return cached DVLA payload for VRM, or None if stale/missing."""
    cur = conn.cursor()
    cur.execute(
        "SELECT fetched_at, payload_json FROM dvla_cache WHERE vrm = ?",
        (vrm,),
    )
    row = cur.fetchone()
    if not row:
        return None
    try:
        age_h = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        ).total_seconds() / 3600
        if age_h > ttl_hours:
            return None
        return json.loads(row[1])
    except Exception:
        return None


def dvla_cache_put(
    conn: sqlite3.Connection, vrm: str, payload: Dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO dvla_cache (vrm, fetched_at, payload_json)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(vrm) DO UPDATE SET"
        "   fetched_at=excluded.fetched_at,"
        "   payload_json=excluded.payload_json",
        (vrm, now_utc_iso(), json.dumps(payload)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Price observations (v0.9.0 — analytics)
# ---------------------------------------------------------------------------

def count_prior_observations_for_item(
    conn: sqlite3.Connection,
    item_id: str,
    platform: str,
) -> int:
    """How many price_observations rows exist for this listing before a new insert."""
    if not item_id or not platform:
        return 0
    cur = conn.execute(
        "SELECT COUNT(*) FROM price_observations WHERE item_id = ? AND platform = ?",
        (item_id, platform),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def insert_price_observation(
    conn: sqlite3.Connection,
    vehicle_key: str,
    platform: str,
    price: float,
    mileage: Optional[int] = None,
    year: Optional[int] = None,
    location: str = "",
    item_id: str = "",
    url: str = "",
) -> None:
    conn.execute(
        "INSERT INTO price_observations"
        " (vehicle_key, platform, price, mileage, year, location,"
        "  observed_at, item_id, url)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (vehicle_key, platform, price, mileage, year, location,
         now_utc_iso(), item_id, url),
    )
    conn.commit()


def load_price_observations(
    conn: sqlite3.Connection,
    vehicle_key: str,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Return price observations for vehicle_key within last N days."""
    cur = conn.cursor()
    cur.execute(
        "SELECT price, mileage, year, observed_at, platform"
        " FROM price_observations"
        " WHERE vehicle_key = ?"
        " ORDER BY observed_at DESC LIMIT 500",
        (vehicle_key,),
    )
    now_dt = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for price, mileage, year, observed_at, platform in cur.fetchall():
        try:
            obs_dt = datetime.fromisoformat(
                str(observed_at).replace("Z", "+00:00"))
            age_days = (now_dt - obs_dt).total_seconds() / 86400
            if age_days <= days:
                out.append({
                    "price": float(price),
                    "mileage": mileage,
                    "year": year,
                    "observed_at": observed_at,
                    "platform": platform,
                    "age_days": age_days,
                })
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Pipeline runs (UK stats map)
# ---------------------------------------------------------------------------

def insert_pipeline_run(
    conn: sqlite3.Connection,
    *,
    buyer_postcode: str,
    lat: Optional[float],
    lon: Optional[float],
    search_radius_miles: int,
    new_observations: int,
    repeat_observations: int,
    total_price_observations_in_db: int,
) -> None:
    """Persist one pipeline run for the 3D UK stats board."""
    conn.execute(
        "INSERT INTO pipeline_runs"
        " (run_at, buyer_postcode, lat, lon, search_radius_miles,"
        "  new_observations, repeat_observations, total_price_observations_in_db)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_utc_iso(),
            (buyer_postcode or "").strip().upper(),
            lat,
            lon,
            int(search_radius_miles),
            int(new_observations),
            int(repeat_observations),
            int(total_price_observations_in_db),
        ),
    )
    conn.commit()


def list_pipeline_runs(
    conn: sqlite3.Connection,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Recent runs for the stats map (newest first)."""
    cur = conn.execute(
        "SELECT id, run_at, buyer_postcode, lat, lon, search_radius_miles,"
        " new_observations, repeat_observations, total_price_observations_in_db"
        " FROM pipeline_runs ORDER BY run_at DESC LIMIT ?",
        (max(1, min(500, limit)),),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        out.append({
            "id": row[0],
            "run_at": row[1],
            "buyer_postcode": row[2],
            "lat": row[3],
            "lon": row[4],
            "search_radius_miles": row[5],
            "new_observations": row[6],
            "repeat_observations": row[7],
            "total_price_observations_in_db": row[8],
        })
    return out


# ---------------------------------------------------------------------------
# Leads (v0.9.0 — CRM workflow)
# ---------------------------------------------------------------------------

def lead_create(
    conn: sqlite3.Connection,
    item_id: str,
    platform: str,
    title: str = "",
    vrm: str = "",
    url: str = "",
    buy_price: float = 0.0,
    max_bid: float = 0.0,
    expected_profit: float = 0.0,
    decision: str = "",
    notes: str = "",
    offer_message: str = "",
    tags: str = "",
) -> Optional[int]:
    """Create a new lead. Returns lead ID, or None if duplicate."""
    now = now_utc_iso()
    try:
        cur = conn.execute(
            "INSERT INTO leads"
            " (item_id, platform, title, vrm, url, buy_price, max_bid,"
            "  expected_profit, decision, notes, offer_message, tags,"
            "  status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sourced', ?, ?)",
            (item_id, platform, title[:200], vrm, url, buy_price,
             max_bid, expected_profit, decision, notes[:500],
             offer_message, tags, now, now),
        )
        conn.commit()

        # Record initial history
        lead_id = cur.lastrowid
        conn.execute(
            "INSERT INTO lead_history (lead_id, old_status, new_status,"
            " changed_at, changed_by, notes)"
            " VALUES (?, '', 'sourced', ?, 'system', 'Auto-created by pipeline')",
            (lead_id, now),
        )
        conn.commit()
        return lead_id
    except sqlite3.IntegrityError:
        return None


def lead_update_status(
    conn: sqlite3.Connection,
    lead_id: int,
    new_status: str,
    changed_by: str = "user",
    notes: str = "",
) -> bool:
    """Transition a lead to a new status. Returns True on success."""
    cur = conn.cursor()
    cur.execute("SELECT status FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    if not row:
        return False
    old_status = row[0]

    now = now_utc_iso()
    conn.execute(
        "UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, lead_id),
    )

    # Update timestamp fields based on status
    ts_map = {
        "contacted": "contacted_at",
        "bought": "bought_at",
        "listed": "listed_at",
        "sold": "sold_at",
    }
    if new_status in ts_map:
        conn.execute(
            f"UPDATE leads SET {ts_map[new_status]} = ? WHERE id = ?",
            (now, lead_id),
        )

    conn.execute(
        "INSERT INTO lead_history (lead_id, old_status, new_status,"
        " changed_at, changed_by, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (lead_id, old_status, new_status, now, changed_by, notes),
    )
    conn.commit()
    return True


def lead_update_fields(
    conn: sqlite3.Connection,
    lead_id: int,
    **fields: Any,
) -> bool:
    """Update arbitrary fields on a lead."""
    if not fields:
        return False
    allowed = {
        "seller_name", "seller_contact", "next_action", "next_action_due",
        "tags", "notes", "actual_buy_price", "actual_sale_price",
        "actual_repairs", "actual_days_to_sell", "realised_profit",
        "offer_message", "listing_draft_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False

    updates["updated_at"] = now_utc_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [lead_id]
    conn.execute(f"UPDATE leads SET {set_clause} WHERE id = ?", values)
    conn.commit()
    return True


def lead_list(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List leads, optionally filtered by status."""
    cur = conn.cursor()
    if status:
        cur.execute(
            "SELECT * FROM leads WHERE status = ?"
            " ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur.execute(
            "SELECT * FROM leads"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def lead_get(conn: sqlite3.Connection, lead_id: int) -> Optional[Dict[str, Any]]:
    """Get a single lead by ID."""
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row))


def leads_due_followup(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return leads with overdue next_action_due."""
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM leads"
        " WHERE next_action_due IS NOT NULL"
        "   AND next_action_due <= ?"
        "   AND status NOT IN ('sold', 'closed', 'lost', 'withdrawn')"
        " ORDER BY next_action_due ASC",
        (now_utc_iso(),),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Posting drafts (v0.9.0)
# ---------------------------------------------------------------------------

def posting_draft_create(
    conn: sqlite3.Connection, **fields: Any,
) -> int:
    now = now_utc_iso()
    conn.execute(
        "INSERT INTO posting_drafts"
        " (lead_id, vrm, title, description, price_gbp, suggested_price,"
        "  ai_description, ai_bullet_points, ai_price_suggestion,"
        "  ai_image_tags, platforms, status, created_at, updated_at,"
        "  image_paths)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
        (
            fields.get("lead_id"),
            fields.get("vrm", ""),
            fields.get("title", ""),
            fields.get("description", ""),
            fields.get("price_gbp", 0.0),
            fields.get("suggested_price", 0.0),
            fields.get("ai_description", ""),
            fields.get("ai_bullet_points", ""),
            fields.get("ai_price_suggestion", 0.0),
            fields.get("ai_image_tags", ""),
            fields.get("platforms", ""),
            now, now,
            fields.get("image_paths", ""),
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def posting_draft_list(
    conn: sqlite3.Connection, status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    if status:
        cur.execute(
            "SELECT * FROM posting_drafts WHERE status = ?"
            " ORDER BY updated_at DESC", (status,))
    else:
        cur.execute(
            "SELECT * FROM posting_drafts ORDER BY updated_at DESC")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Dealer network (v0.9.0)
# ---------------------------------------------------------------------------

def dealer_profile_create(
    conn: sqlite3.Connection, **fields: Any,
) -> int:
    conn.execute(
        "INSERT INTO dealer_profiles"
        " (name, location, postcode, phone, email, specialties,"
        "  created_at, is_active)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (
            fields.get("name", ""),
            fields.get("location", ""),
            fields.get("postcode", ""),
            fields.get("phone", ""),
            fields.get("email", ""),
            fields.get("specialties", ""),
            now_utc_iso(),
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def dealer_inventory_add(
    conn: sqlite3.Connection, dealer_id: int, **fields: Any,
) -> int:
    conn.execute(
        "INSERT INTO dealer_inventory"
        " (dealer_id, vrm, title, description, price_gbp, trade_price,"
        "  condition_notes, mot_expiry, mileage, year, fuel_type, colour,"
        "  image_urls, created_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available')",
        (
            dealer_id,
            fields.get("vrm", ""),
            fields.get("title", ""),
            fields.get("description", ""),
            fields.get("price_gbp", 0.0),
            fields.get("trade_price", 0.0),
            fields.get("condition_notes", ""),
            fields.get("mot_expiry", ""),
            fields.get("mileage"),
            fields.get("year"),
            fields.get("fuel_type", ""),
            fields.get("colour", ""),
            fields.get("image_urls", ""),
            now_utc_iso(),
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def dealer_inventory_search(
    conn: sqlite3.Connection,
    make: str = "",
    model: str = "",
    max_price: float = 0,
    status: str = "available",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search dealer inventory with optional filters."""
    query = "SELECT di.*, dp.name as dealer_name, dp.location as dealer_location FROM dealer_inventory di JOIN dealer_profiles dp ON di.dealer_id = dp.id WHERE di.status = ?"
    params: list = [status]

    if make:
        query += " AND LOWER(di.title) LIKE ?"
        params.append(f"%{make.lower()}%")
    if model:
        query += " AND LOWER(di.title) LIKE ?"
        params.append(f"%{model.lower()}%")
    if max_price > 0:
        query += " AND di.trade_price <= ?"
        params.append(max_price)

    query += " ORDER BY di.created_at DESC LIMIT ?"
    params.append(limit)

    cur = conn.cursor()
    cur.execute(query, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def dealer_message_send(
    conn: sqlite3.Connection,
    from_id: int, to_id: int,
    subject: str, body: str,
    inventory_item_id: Optional[int] = None,
) -> int:
    conn.execute(
        "INSERT INTO dealer_messages"
        " (from_dealer_id, to_dealer_id, inventory_item_id,"
        "  subject, body, created_at, read)"
        " VALUES (?, ?, ?, ?, ?, ?, 0)",
        (from_id, to_id, inventory_item_id, subject, body, now_utc_iso()),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ---------------------------------------------------------------------------
# Verified vehicles (v0.9.9)
# ---------------------------------------------------------------------------

def upsert_verified_vehicle(
    conn: sqlite3.Connection,
    vrm: str,
    payload: Dict[str, Any],
) -> None:
    """
    Upsert a DVSA-verified vehicle record into verified_vehicles.

    Extracts summary fields from the DVSA payload for quick querying
    without parsing the full JSON. The full payload is stored in mot_json.

    Args:
        conn:    Database connection.
        vrm:     Normalised VRM string (uppercase, no spaces).
        payload: Raw DVSA MOT history API response dict.
    """
    tests = payload.get("motTests") or []
    latest = tests[0] if tests else {}

    total_tests = len(tests)
    advisory_count = sum(
        1 for t in tests
        for d in (t.get("defects") or [])
        if str(d.get("type", "")).upper() in ("ADVISORY", "MONITOR")
    )
    fail_count = sum(
        1 for t in tests
        if str(t.get("testResult", "")).upper() == "FAILED"
    )

    last_mot_date   = str(latest.get("completedDate", ""))[:10] or None
    last_mot_result = str(latest.get("testResult", "")).upper() or None
    last_mileage_raw = latest.get("odometerValue")
    try:
        last_mileage = int(last_mileage_raw) if last_mileage_raw else None
    except (ValueError, TypeError):
        last_mileage = None

    now = now_utc_iso()
    conn.execute(
        "INSERT INTO verified_vehicles"
        " (vrm, make, model, fuel_type, colour, first_used_date,"
        "  last_mot_date, last_mot_result, total_tests, advisory_count,"
        "  fail_count, last_mileage, mot_json, first_seen_date, last_updated)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(vrm) DO UPDATE SET"
        "   make=excluded.make, model=excluded.model,"
        "   fuel_type=excluded.fuel_type, colour=excluded.colour,"
        "   first_used_date=excluded.first_used_date,"
        "   last_mot_date=excluded.last_mot_date,"
        "   last_mot_result=excluded.last_mot_result,"
        "   total_tests=excluded.total_tests,"
        "   advisory_count=excluded.advisory_count,"
        "   fail_count=excluded.fail_count,"
        "   last_mileage=excluded.last_mileage,"
        "   mot_json=excluded.mot_json,"
        "   last_updated=excluded.last_updated",
        (
            vrm.upper().replace(" ", ""),
            payload.get("make", ""),
            payload.get("model", ""),
            payload.get("fuelType", ""),
            payload.get("primaryColour", ""),
            payload.get("firstUsedDate", ""),
            last_mot_date,
            last_mot_result,
            total_tests,
            advisory_count,
            fail_count,
            last_mileage,
            json.dumps(payload),
            now,  # first_seen_date only set on INSERT (ON CONFLICT preserves original)
            now,
        ),
    )
    conn.commit()


def get_verified_vehicle(
    conn: sqlite3.Connection,
    vrm: str,
    max_age_days: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Look up a VRM in verified_vehicles. Returns the stored DVSA payload
    if the record exists and was updated within max_age_days, else None.

    This is checked before calling the DVSA API to avoid redundant fetches.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT last_updated, mot_json FROM verified_vehicles WHERE vrm = ?",
        (vrm.upper().replace(" ", ""),),
    )
    row = cur.fetchone()
    if not row:
        return None
    try:
        age_days = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        ).total_seconds() / 86400
        if age_days > max_age_days:
            return None
        return json.loads(row[1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# item_vrm — VRM persistence per listing (v0.9.9)
# ---------------------------------------------------------------------------

def upsert_item_vrm(
    conn: sqlite3.Connection,
    item_id: str,
    vrm: str,
    source: str,
    confidence: float,
) -> None:
    """
    Save or update the best known VRM for a listing item_id.

    Called after any successful VRM find (ANPR, regex, DVLA). Subsequent
    pipeline runs check this table before spending Plate Recognizer credits.
    A higher-confidence result overwrites a lower one.
    """
    now = now_utc_iso()
    conn.execute(
        "INSERT INTO item_vrm (item_id, vrm, source, confidence, found_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(item_id) DO UPDATE SET"
        "   vrm = CASE WHEN excluded.confidence > item_vrm.confidence"
        "              THEN excluded.vrm ELSE item_vrm.vrm END,"
        "   source = CASE WHEN excluded.confidence > item_vrm.confidence"
        "                 THEN excluded.source ELSE item_vrm.source END,"
        "   confidence = MAX(excluded.confidence, item_vrm.confidence),"
        "   found_at = excluded.found_at",
        (item_id, vrm.upper(), source, float(confidence), now),
    )
    conn.commit()


def get_item_vrm(
    conn: sqlite3.Connection,
    item_id: str,
    max_age_days: int = 14,
) -> Optional[Tuple[str, str, float]]:
    """
    Return (vrm, source, confidence) for item_id if found within max_age_days.
    Returns None if not found or too old.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT vrm, source, confidence, found_at FROM item_vrm WHERE item_id = ?",
        (item_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    try:
        age_days = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(str(row[3]).replace("Z", "+00:00"))
        ).total_seconds() / 86400
        if age_days > max_age_days:
            return None
        return str(row[0]), str(row[1]), float(row[2])
    except Exception:
        return None


def dealer_messages_inbox(
    conn: sqlite3.Connection, dealer_id: int, unread_only: bool = False,
) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    query = (
        "SELECT dm.*, dp.name as from_dealer_name"
        " FROM dealer_messages dm"
        " JOIN dealer_profiles dp ON dm.from_dealer_id = dp.id"
        " WHERE dm.to_dealer_id = ?"
    )
    if unread_only:
        query += " AND dm.read = 0"
    query += " ORDER BY dm.created_at DESC"
    cur.execute(query, (dealer_id,))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# v1.0: Trade outcome logging (Sprint 1 PDF rollout plan)
# ---------------------------------------------------------------------------

def log_completed_trade(
    conn: sqlite3.Connection,
    *,
    vrm: str,
    buy_price: float,
    sell_price: float,
    repair_costs: float = 0.0,
    other_costs: float = 0.0,
    days_to_sell: Optional[int] = None,
    platform_sold: Optional[str] = None,
    condition_notes: Optional[str] = None,
    make: Optional[str] = None,
    model: Optional[str] = None,
    year: Optional[int] = None,
    mileage: Optional[int] = None,
    predicted_profit: Optional[float] = None,
    lead_id: Optional[int] = None,
    source: str = "manual",
) -> int:
    """
    Record a completed flip outcome. Returns the new trade id.

    realised_profit = sell_price - buy_price - repair_costs - other_costs
    prediction_error = realised_profit - predicted_profit  (None if no prediction)
    """
    from dealerly.utils import now_utc_iso
    realised = sell_price - buy_price - repair_costs - other_costs
    error = (realised - predicted_profit) if predicted_profit is not None else None
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO completed_trades
          (recorded_at, vrm, make, model, year, mileage,
           buy_price, sell_price, repair_costs, other_costs,
           days_to_sell, platform_sold, condition_notes,
           predicted_profit, realised_profit, prediction_error,
           lead_id, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now_utc_iso(), vrm.upper().replace(" ", ""),
            make, model, year, mileage,
            buy_price, sell_price, repair_costs, other_costs,
            days_to_sell, platform_sold, condition_notes,
            predicted_profit, realised, error,
            lead_id, source,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def list_completed_trades(conn: sqlite3.Connection) -> list:
    """Return all completed trades newest-first."""
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM completed_trades ORDER BY recorded_at DESC"
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Sprint 9: Seen-before / price trend intelligence
# ---------------------------------------------------------------------------

def get_listing_seen_info(
    conn: sqlite3.Connection,
    item_id: str,
    vrm: str = "",
) -> Dict[str, Any]:
    """
    Return 'seen before' intelligence for a single listing.

    Checks:
      - price_observations: how many times this item_id was recorded across runs
      - watchlist: whether the item_id was saved by the user
      - leads: whether a lead was created for this item_id
      - completed_trades: whether this VRM was a completed flip

    Returns a dict consumed by report._seen_badges_html().
    """
    result: Dict[str, Any] = {
        "times_seen": 0,
        "first_seen_price": None,
        "watchlisted": False,
        "has_lead": False,
        "was_traded": False,
    }
    try:
        cur = conn.cursor()

        # How many times has this item_id appeared in price_observations?
        cur.execute(
            "SELECT price, observed_at FROM price_observations"
            " WHERE item_id = ? ORDER BY observed_at ASC",
            (item_id,),
        )
        obs_rows = cur.fetchall()
        result["times_seen"] = len(obs_rows)
        if obs_rows:
            result["first_seen_price"] = float(obs_rows[0][0])

        # Watchlist
        cur.execute("SELECT 1 FROM watchlist WHERE item_id = ? LIMIT 1", (item_id,))
        if cur.fetchone():
            result["watchlisted"] = True

        # Leads
        cur.execute("SELECT 1 FROM leads WHERE item_id = ? LIMIT 1", (item_id,))
        if cur.fetchone():
            result["has_lead"] = True

        # Completed trades (by normalised VRM)
        if vrm:
            vrm_norm = vrm.upper().replace(" ", "")
            cur.execute(
                "SELECT 1 FROM completed_trades WHERE vrm = ? LIMIT 1",
                (vrm_norm,),
            )
            if cur.fetchone():
                result["was_traded"] = True
    except Exception:
        pass
    return result


def get_trades_accuracy_summary(conn: sqlite3.Connection) -> dict:
    """
    Return aggregated accuracy stats across all logged trades.
    Used for Sprint 3 (Prediction vs Outcome analysis).
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COUNT(*)                                        AS total_trades,
            ROUND(SUM(realised_profit), 2)                 AS total_realised,
            ROUND(AVG(realised_profit), 2)                 AS avg_realised,
            ROUND(MAX(realised_profit), 2)                 AS best_trade,
            ROUND(MIN(realised_profit), 2)                 AS worst_trade,
            COUNT(prediction_error)                        AS trades_with_prediction,
            ROUND(AVG(prediction_error), 2)                AS avg_prediction_error,
            ROUND(AVG(ABS(prediction_error)), 2)           AS avg_abs_error,
            SUM(CASE WHEN prediction_error >= 0 THEN 1 ELSE 0 END)
                                                           AS prediction_over_count,
            ROUND(AVG(days_to_sell), 1)                    AS avg_days_to_sell
        FROM completed_trades
        """
    )
    cols = [c[0] for c in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else {}

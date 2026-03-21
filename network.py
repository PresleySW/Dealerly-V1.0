"""
dealerly/network.py
===================
Dealer-to-dealer B2B network and marketplace.

Local-first SQLite implementation. Future versions sync to central server.

Depends on:
  - dealerly.db (dealer_profile_create, dealer_inventory_*, dealer_message_*)
  - dealerly.ebay (guess_make_model)
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from dealerly.db import (
    dealer_inventory_add,
    dealer_inventory_search,
    dealer_message_send,
    dealer_messages_inbox,
    dealer_profile_create,
)
from dealerly.ebay import guess_make_model
from dealerly.utils import now_utc_iso


def register_dealer(
    conn: sqlite3.Connection,
    name: str, location: str = "", postcode: str = "",
    phone: str = "", email: str = "", specialties: str = "",
) -> int:
    return dealer_profile_create(
        conn, name=name, location=location, postcode=postcode,
        phone=phone, email=email, specialties=specialties,
    )


def get_dealer_profile(conn: sqlite3.Connection, dealer_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM dealer_profiles WHERE id = ?", (dealer_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row))


def list_active_dealers(conn: sqlite3.Connection, specialty: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    if specialty:
        cur.execute(
            "SELECT * FROM dealer_profiles WHERE is_active = 1 AND LOWER(specialties) LIKE ? ORDER BY trade_count DESC LIMIT ?",
            (f"%{specialty.lower()}%", limit))
    else:
        cur.execute("SELECT * FROM dealer_profiles WHERE is_active = 1 ORDER BY trade_count DESC LIMIT ?", (limit,))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def publish_vehicle(
    conn: sqlite3.Connection, dealer_id: int, title: str, price_gbp: float,
    trade_price: float = 0.0, vrm: str = "", description: str = "",
    condition_notes: str = "", mot_expiry: str = "",
    mileage: Optional[int] = None, year: Optional[int] = None,
    fuel_type: str = "", colour: str = "", image_urls: str = "",
) -> int:
    if not trade_price:
        trade_price = price_gbp * 0.85
    return dealer_inventory_add(
        conn, dealer_id, vrm=vrm, title=title, description=description,
        price_gbp=price_gbp, trade_price=trade_price,
        condition_notes=condition_notes, mot_expiry=mot_expiry,
        mileage=mileage, year=year, fuel_type=fuel_type, colour=colour,
        image_urls=image_urls,
    )


def search_network_inventory(
    conn: sqlite3.Connection, make: str = "", model: str = "",
    max_price: float = 0, limit: int = 50,
) -> List[Dict[str, Any]]:
    return dealer_inventory_search(conn, make=make, model=model, max_price=max_price, limit=limit)


def mark_inventory_sold(conn: sqlite3.Connection, item_id: int) -> bool:
    conn.execute("UPDATE dealer_inventory SET status = 'sold' WHERE id = ?", (item_id,))
    conn.commit()
    return True


def mark_inventory_reserved(conn: sqlite3.Connection, item_id: int) -> bool:
    conn.execute("UPDATE dealer_inventory SET status = 'reserved' WHERE id = ?", (item_id,))
    conn.commit()
    return True


def send_message(
    conn: sqlite3.Connection, from_dealer_id: int, to_dealer_id: int,
    subject: str, body: str, inventory_item_id: Optional[int] = None,
) -> int:
    return dealer_message_send(conn, from_dealer_id, to_dealer_id, subject, body, inventory_item_id)


def get_inbox(conn: sqlite3.Connection, dealer_id: int, unread_only: bool = False) -> List[Dict[str, Any]]:
    return dealer_messages_inbox(conn, dealer_id, unread_only)


def mark_message_read(conn: sqlite3.Connection, message_id: int) -> bool:
    conn.execute("UPDATE dealer_messages SET read = 1 WHERE id = ?", (message_id,))
    conn.commit()
    return True


def get_unread_count(conn: sqlite3.Connection, dealer_id: int) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM dealer_messages WHERE to_dealer_id = ? AND read = 0", (dealer_id,))
    return cur.fetchone()[0]


def find_matching_inventory(
    conn: sqlite3.Connection, want_make: str, want_model: str,
    max_trade_price: float = 0, exclude_dealer_id: Optional[int] = None, limit: int = 10,
) -> List[Dict[str, Any]]:
    results = search_network_inventory(conn, make=want_make, model=want_model, max_price=max_trade_price, limit=limit * 2)
    if exclude_dealer_id:
        results = [r for r in results if r.get("dealer_id") != exclude_dealer_id]
    return results[:limit]


def suggest_trades_for_dealer(
    conn: sqlite3.Connection, dealer_id: int,
    wanted_makes_models: List[Dict[str, str]], max_budget: float = 5000,
) -> List[Dict[str, Any]]:
    suggestions = []
    for want in wanted_makes_models:
        matches = find_matching_inventory(
            conn, want_make=want.get("make", ""), want_model=want.get("model", ""),
            max_trade_price=max_budget, exclude_dealer_id=dealer_id, limit=5,
        )
        suggestions.extend(matches)
    seen = set()
    unique = []
    for s in suggestions:
        sid = s.get("id")
        if sid not in seen:
            seen.add(sid)
            unique.append(s)
    return unique


def network_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM dealer_profiles WHERE is_active = 1")
    active_dealers = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM dealer_inventory WHERE status = 'available'")
    available = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM dealer_inventory WHERE status = 'sold'")
    sold = cur.fetchone()[0]
    cur.execute("SELECT AVG(trade_price) FROM dealer_inventory WHERE status = 'available' AND trade_price > 0")
    avg_tp = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM dealer_messages")
    msgs = cur.fetchone()[0]
    return {
        "active_dealers": active_dealers, "available_vehicles": available,
        "sold_vehicles": sold, "avg_trade_price": round(avg_tp, 0), "total_messages": msgs,
    }

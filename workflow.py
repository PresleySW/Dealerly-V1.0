"""
dealerly/workflow.py
====================
CRM-style deal pipeline and workflow management.

Responsibilities:
  - Auto-create leads from scored pipeline output
  - Lead status transitions with validation
  - Automated follow-up reminders
  - Seller contact management
  - Deal pipeline statistics
  - P&L actuals recording

Lead status flow:
  sourced → contacted → inspecting → bidding → bought →
    preparing → listed → sold → closed
  Any stage → lost | withdrawn
  lost/withdrawn → sourced (re-open)

Depends on:
  - dealerly.db (lead_create, lead_update_status, lead_update_fields,
                 lead_list, lead_get, leads_due_followup)
  - dealerly.models (Listing, DealInput, DealOutput, Lead,
                     LEAD_STATUSES, LEAD_STATUS_TRANSITIONS)
  - dealerly.config (DEFAULT_FOLLOW_UP_HOURS, DEFAULT_INSPECTION_DEADLINE_DAYS)
  - dealerly.utils (now_utc_iso)

No external API calls.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from dealerly.config import (
    DEFAULT_FOLLOW_UP_HOURS,
    DEFAULT_INSPECTION_DEADLINE_DAYS,
)
from dealerly.db import (
    lead_create,
    lead_get,
    lead_list,
    lead_update_fields,
    lead_update_status,
    leads_due_followup,
)
from dealerly.models import (
    DealInput,
    DealOutput,
    LEAD_STATUS_TRANSITIONS,
    LEAD_STATUSES,
    Listing,
)
from dealerly.utils import now_utc_iso


# ---------------------------------------------------------------------------
# Auto-create leads from pipeline output
# ---------------------------------------------------------------------------

_OBSIDIAN_LEAD_PATH_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_SCAN_ROW_RE = re.compile(
    r"^\|\s*(?P<ts>[^|]+?)\s*\|\s*(?P<item>.*?)\s*\|\s*(?P<vrm>.*?)\s*\|"
    r"\s*(?P<src>[^|]+?)\s*\|\s*(?P<conf>\d{1,3}%?)\s*\|\s*(?P<platform>[^|]+?)\s*\|"
    r"\s*(?P<title>.*?)\|\s*(?P<url>.*?)\|\s*$",
    re.IGNORECASE,
)


def _safe_lead_slug(text: str, fallback: str = "lead") -> str:
    base = _OBSIDIAN_LEAD_PATH_SAFE_RE.sub("-", (text or "").strip())[:80].strip("-")
    return base or fallback


def _clean_ref_token(text: str) -> str:
    """Sanitize tokens extracted from markdown links."""
    s = (text or "").strip()
    s = s.replace("[", "").replace("]", "")
    s = s.replace("(", "").replace(")", "")
    s = s.split("|", 1)[0].strip()
    if "/" in s:
        s = s.split("/")[-1].strip()
    return s


def _extract_item_ref(cell: str) -> Tuple[str, str]:
    """Parse item cell and return (item_id, item_slug)."""
    s = (cell or "").strip()
    m = _WIKILINK_RE.search(s)
    if m:
        target = (m.group(1) or "").strip()
        label = (m.group(2) or target.split("/")[-1]).strip()
        slug = _clean_ref_token(target.split("/")[-1])
        item_id = _clean_ref_token(label or slug)
        return item_id or slug, slug or _safe_lead_slug(item_id, "item")
    m = _MD_LINK_RE.search(s)
    if m:
        label = (m.group(1) or "").strip()
        target = (m.group(2) or "").strip()
        slug = _clean_ref_token(target.split("/")[-1].replace(".md", ""))
        item_id = _clean_ref_token(label or slug)
        return item_id or slug, slug or _safe_lead_slug(item_id, "item")
    item_id = _clean_ref_token(s)
    return item_id, _safe_lead_slug(item_id, "item")


def _extract_vrm_ref(cell: str) -> Tuple[str, str]:
    """Parse VRM cell and return (vrm, vrm_note_name)."""
    s = (cell or "").strip()
    m = _WIKILINK_RE.search(s)
    if m:
        target = (m.group(1) or "").strip()
        label = (m.group(2) or target.split("/")[-1]).strip()
        vrm = _clean_ref_token(label or target.split("/")[-1]).upper().replace(" ", "")
        note = _clean_ref_token(target.split("/")[-1]).upper().strip()
        return vrm, note or _safe_lead_slug(vrm, "unknown-vrm").upper()
    m = _MD_LINK_RE.search(s)
    if m:
        label = (m.group(1) or "").strip().upper().replace(" ", "")
        target = (m.group(2) or "").strip()
        note = _clean_ref_token(target.split("/")[-1].replace(".md", "")).upper().strip()
        vrm = _clean_ref_token(label or note).upper().replace(" ", "")
        return vrm, note or _safe_lead_slug(vrm, "unknown-vrm").upper()
    vrm = _clean_ref_token(s).upper().replace(" ", "")
    return vrm, _safe_lead_slug(vrm, "unknown-vrm").upper()


def _render_graph_index(item_links: List[str], vrm_links: List[str]) -> str:
    """Render a clean, deterministic Obsidian graph index page."""
    item_count = len(item_links)
    vrm_count = len(vrm_links)
    total_nodes = item_count + vrm_count

    # Mermaid overview uses aliases to avoid special chars in node IDs.
    max_edges = 80
    mermaid_lines = ["graph LR", "  HUB[Dealerly Brain]"]
    for i, link in enumerate(item_links[:40], 1):
        mermaid_lines.append(f"  HUB --> I{i}[Item {i}]")
    for i, link in enumerate(vrm_links[:40], 1):
        mermaid_lines.append(f"  HUB --> V{i}[VRM {i}]")
    if total_nodes > max_edges:
        mermaid_lines.append("  MORE[More nodes in lists below]")
        mermaid_lines.append("  HUB --> MORE")
    mermaid = "\n".join(mermaid_lines)

    lines = [
        "# Dealerly Brain Graph Index",
        "",
        "## Overview",
        f"- Total nodes: {total_nodes}",
        f"- Item nodes: {item_count}",
        f"- VRM nodes: {vrm_count}",
        f"- Last rebuilt: {now_utc_iso()}",
        "",
        "## Graph Preview",
        "```mermaid",
        mermaid,
        "```",
        "",
        "## Item Nodes",
    ]
    if item_links:
        lines.extend(item_links)
    else:
        lines.append("- (none yet)")
    lines.extend(["", "## VRM Nodes"])
    if vrm_links:
        lines.extend(vrm_links)
    else:
        lines.append("- (none yet)")
    lines.append("")
    return "\n".join(lines)


def _extract_graph_link_sets(graph_text: str) -> Tuple[set[str], set[str]]:
    """Return existing item/vrm bullet-link sets from graph index text."""
    item_set: set[str] = set()
    vrm_set: set[str] = set()
    for line in (graph_text or "").splitlines():
        s = line.strip()
        if not s.startswith("- [[Database/"):
            continue
        if "/Items/" in s:
            item_set.add(s)
        elif "/VRMs/" in s:
            vrm_set.add(s)
    return item_set, vrm_set


def _extract_existing_scan_keys(scans_text: str) -> set[str]:
    """Parse vrm_scans markdown table and return item|vrm dedupe keys."""
    keys: set[str] = set()
    for line in (scans_text or "").splitlines():
        s = line.strip()
        if not s.startswith("|") or "---|" in s or "Timestamp" in s:
            continue
        m = _SCAN_ROW_RE.match(s)
        if not m:
            continue
        item_id, _item_slug = _extract_item_ref(m.group("item"))
        vrm, _vrm_note = _extract_vrm_ref(m.group("vrm"))
        if item_id and vrm:
            keys.add(f"{item_id}|{vrm}")
    return keys


def _collect_graph_links_from_scans(scans_text: str) -> Tuple[set[str], set[str]]:
    """Build canonical item/vrm link sets from vrm_scans markdown table."""
    item_set: set[str] = set()
    vrm_set: set[str] = set()
    for line in (scans_text or "").splitlines():
        s = line.strip()
        if not s.startswith("|") or "---|" in s or "Timestamp" in s:
            continue
        m = _SCAN_ROW_RE.match(s)
        if not m:
            continue
        item_id, item_slug = _extract_item_ref(m.group("item"))
        vrm, vrm_note = _extract_vrm_ref(m.group("vrm"))
        if item_id and item_slug:
            item_set.add(f"- [[Database/Items/{item_slug}|{item_id}]]")
        if vrm and vrm_note:
            vrm_set.add(f"- [[Database/VRMs/{vrm_note}|{vrm}]]")
    return item_set, vrm_set


def _upsert_obsidian_item_note(
    vault_db_dir: Path,
    listing: Listing,
    *,
    item_slug: str,
    vrm_note_name: str,
) -> None:
    items_dir = vault_db_dir / "Items"
    items_dir.mkdir(parents=True, exist_ok=True)
    note_path = items_dir / f"{item_slug}.md"
    item_tags = [
        "dealerly",
        "node/item",
        f"platform/{(listing.platform or 'unknown').lower()}",
    ]
    if listing.vrm:
        item_tags.append("has-vrm")
    if listing.vrm_confidence >= 0.85:
        item_tags.append("vrm/high-confidence")
    elif listing.vrm_confidence > 0:
        item_tags.append("vrm/low-confidence")
    if "cat s" in (listing.title or "").lower():
        item_tags.append("risk/cat-s")
    lines = [
        "---",
        f"tags: [{', '.join(item_tags)}]",
        "---",
        "",
        f"# Item {listing.item_id}",
        "",
        "## Links",
        "- [[Database/vrm_scans]]",
        f"- [[Database/VRMs/{vrm_note_name}]]",
        "",
        "## Snapshot",
        f"- Platform: {listing.platform}",
        f"- Title: {listing.title}",
        f"- VRM: {listing.vrm}",
        f"- Source: {listing.vrm_source or 'unknown'}",
        f"- Confidence: {listing.vrm_confidence:.0%}",
        f"- URL: {listing.url}",
        "",
        f"_Updated: {now_utc_iso()}_",
    ]
    note_path.write_text("\n".join(lines), encoding="utf-8")


def _upsert_obsidian_vrm_note(
    vault_db_dir: Path,
    listing: Listing,
    *,
    item_slug: str,
    vrm_note_name: str,
) -> None:
    vrms_dir = vault_db_dir / "VRMs"
    vrms_dir.mkdir(parents=True, exist_ok=True)
    note_path = vrms_dir / f"{vrm_note_name}.md"
    if note_path.exists():
        existing = note_path.read_text(encoding="utf-8")
    else:
        vrm_tags = [
            "dealerly",
            "node/vrm",
            f"platform/{(listing.platform or 'unknown').lower()}",
        ]
        existing = (
            "---\n"
            f"tags: [{', '.join(vrm_tags)}]\n"
            "---\n\n"
            f"# VRM {listing.vrm}\n\n"
            "## Links\n"
            "- [[Database/vrm_scans]]\n\n"
            "## Sightings\n"
        )
    row = (
        f"- {now_utc_iso()} | [[Database/Items/{item_slug}|{listing.item_id}]] | "
        f"{listing.platform} | {listing.vrm_source or 'unknown'} | {listing.vrm_confidence:.0%}"
    )
    if row not in existing:
        existing = existing.rstrip() + "\n" + row + "\n"
    note_path.write_text(existing, encoding="utf-8")

def auto_create_leads(
    conn: sqlite3.Connection,
    rows: List[Tuple[Listing, DealInput, DealOutput]],
) -> int:
    """
    Auto-create CRM leads for BUY and OFFER decisions.

    Called at Phase 7 of the pipeline. Skips listings that already
    have a lead (by item_id + platform unique constraint).

    Sets initial next_action to "Contact seller" with a due date
    of DEFAULT_FOLLOW_UP_HOURS from now.

    Returns count of leads created.
    """
    created = 0
    follow_up_dt = (
        datetime.now(timezone.utc)
        + timedelta(hours=DEFAULT_FOLLOW_UP_HOURS)
    ).replace(microsecond=0).isoformat()

    for listing, deal, out in rows:
        if out.decision not in ("BUY", "OFFER"):
            continue

        # Determine tags
        tags = []
        if out.decision == "BUY":
            tags.append("hot-deal")
        if out.expected_profit > 400:
            tags.append("high-profit")
        if listing.vrm:
            tags.append("vrm-confirmed")
        if listing.mot_history:
            tags.append("mot-checked")

        lead_id = lead_create(
            conn,
            item_id=listing.item_id,
            platform=listing.platform,
            title=listing.title,
            vrm=listing.vrm,
            url=listing.url,
            buy_price=listing.price_gbp,
            max_bid=out.max_bid,
            expected_profit=out.expected_profit,
            decision=out.decision,
            notes=out.notes[:500],
            offer_message=listing.offer_message,
            tags=",".join(tags),
        )

        if lead_id:
            # Set initial follow-up reminder
            lead_update_fields(
                conn, lead_id,
                next_action="Contact seller / send offer",
                next_action_due=follow_up_dt,
            )
            created += 1

    return created


def export_buy_leads_to_obsidian(
    rows: List[Tuple[Listing, DealInput, DealOutput]],
    vault_leads_dir: Path,
) -> int:
    """
    Export BUY recommendations as markdown files into an Obsidian Leads folder.
    """
    vault_leads_dir.mkdir(parents=True, exist_ok=True)
    index_path = vault_leads_dir / "_Leads_Index.md"
    if not index_path.exists():
        index_path.write_text("# Leads Index\n\n", encoding="utf-8")
    index_existing = index_path.read_text(encoding="utf-8")
    exported = 0

    for listing, deal, out in rows:
        if out.decision != "BUY":
            continue

        # Deterministic filename per listing prevents duplicate lead notes on
        # repeat pipeline runs and keeps Obsidian links stable.
        fname = (
            f"{_safe_lead_slug(listing.item_id, 'item')}"
            f"_{_safe_lead_slug(listing.title, 'listing')}.md"
        )
        out_path = vault_leads_dir / fname

        lines = [
            "---",
            "tags: [dealerly, lead/buy, action/priority]",
            "---",
            "",
            f"# BUY Lead - {listing.title}",
            "",
            "## Links",
            "- [[Database/vrm_scans]]",
            f"- [[Database/Items/{_safe_lead_slug(listing.item_id, 'item')}]]",
            (
                f"- [[Database/VRMs/{_safe_lead_slug(listing.vrm, 'unknown-vrm')}]]"
                if listing.vrm else "- [[Database/VRMs]]"
            ),
            "",
            "## Snapshot",
            f"- Platform: {listing.platform}",
            f"- Item ID: {listing.item_id}",
            f"- URL: {listing.url}",
            f"- Price GBP: {listing.price_gbp:.0f}",
            f"- Expected Resale GBP: {deal.expected_resale:.0f}",
            f"- Expected Profit GBP: {out.expected_profit:.0f}",
            f"- Max Bid GBP: {out.max_bid:.0f}",
            f"- Shock Ratio: {out.shock_impact_ratio:.2f}",
            f"- p_MOT: {out.p_mot:.0%}",
            f"- Decision: {out.decision}",
            f"- Reason: {out.reason}",
            "",
            "## Verification",
            f"- VRM: {listing.vrm or 'unknown'}",
            f"- VRM Source: {listing.vrm_source or 'unknown'}",
            f"- VRM Confidence: {listing.vrm_confidence:.0%}",
            f"- ULEZ: {listing.ulez_compliant}",
            "",
            "## Notes",
            out.notes or "(none)",
            "",
            f"_Exported: {now_utc_iso()}_",
        ]
        out_path.write_text("\n".join(lines), encoding="utf-8")
        link_line = f"- [[Leads/{out_path.stem}|{listing.item_id}]]"
        if link_line not in index_existing:
            with index_path.open("a", encoding="utf-8") as idxf:
                idxf.write(link_line + "\n")
            index_existing += link_line + "\n"
        exported += 1

    return exported


def export_vrm_scans_to_obsidian(
    listings: List[Listing],
    vault_db_dir: Path,
) -> int:
    """
    Export found/scanned VRMs into an Obsidian database markdown ledger.

    This captures all listings with a discovered VRM, not only BUY decisions.
    """
    vault_db_dir.mkdir(parents=True, exist_ok=True)
    out_path = vault_db_dir / "vrm_scans.md"
    if not out_path.exists():
        header = [
            "# VRM Scans",
            "",
            "| Timestamp | Item ID | VRM | Source | Confidence | Platform | Title | URL |",
            "|---|---|---|---|---:|---|---|---|",
        ]
        out_path.write_text("\n".join(header) + "\n", encoding="utf-8")
    graph_index = vault_db_dir / "_Graph_Index.md"
    existing = out_path.read_text(encoding="utf-8")
    existing_keys = _extract_existing_scan_keys(existing)
    item_links, vrm_links = _collect_graph_links_from_scans(existing)
    appended = 0
    rows: List[str] = []
    ts = now_utc_iso()

    for listing in listings:
        if not listing.vrm:
            continue
        dedupe_key = f"{listing.item_id}|{listing.vrm}"
        title = (listing.title or "").replace("|", "/")[:100]
        url = (listing.url or "").replace("|", "%7C")
        source = (listing.vrm_source or "unknown").replace("|", "/")
        item_slug = _safe_lead_slug(listing.item_id, "item")
        vrm_note = _safe_lead_slug(listing.vrm, "unknown-vrm").upper()
        _upsert_obsidian_item_note(
            vault_db_dir, listing, item_slug=item_slug, vrm_note_name=vrm_note
        )
        _upsert_obsidian_vrm_note(
            vault_db_dir, listing, item_slug=item_slug, vrm_note_name=vrm_note
        )
        item_link = f"[[Database/Items/{item_slug}]]"
        vrm_link = f"[[Database/VRMs/{vrm_note}]]"
        item_idx = f"- [[Database/Items/{item_slug}|{listing.item_id}]]"
        vrm_idx = f"- [[Database/VRMs/{vrm_note}|{listing.vrm}]]"
        item_links.add(item_idx)
        vrm_links.add(vrm_idx)
        if dedupe_key in existing_keys:
            continue
        rows.append(
            f"| {ts} | {item_link} | {vrm_link} | {source} | "
            f"{listing.vrm_confidence:.0%} | {listing.platform} | {title} | {url} |"
        )
        existing_keys.add(dedupe_key)
        appended += 1

    if rows:
        with out_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")

    graph_index.write_text(
        _render_graph_index(sorted(item_links), sorted(vrm_links)),
        encoding="utf-8",
    )

    return appended


def backfill_obsidian_graph_from_vrm_scans(vault_db_dir: Path) -> Tuple[int, int]:
    """
    Rebuild Obsidian graph node files from historical vrm_scans.md rows.

    Returns:
        (unique_item_nodes, unique_vrm_nodes)
    """
    scans_path = vault_db_dir / "vrm_scans.md"
    if not scans_path.exists():
        return 0, 0

    lines = scans_path.read_text(encoding="utf-8").splitlines()
    item_seen: set[str] = set()
    vrm_seen: set[str] = set()

    graph_index = vault_db_dir / "_Graph_Index.md"
    item_links, vrm_links = _collect_graph_links_from_scans("\n".join(lines))

    for line in lines:
        if not line.strip().startswith("|"):
            continue
        if "---|" in line or "Timestamp" in line:
            continue
        m = _SCAN_ROW_RE.match(line.strip())
        if not m:
            continue
        item_cell = m.group("item")
        vrm_cell = m.group("vrm")
        platform = m.group("platform")
        title = m.group("title")
        url = m.group("url")
        source = m.group("src")
        conf_txt = m.group("conf")
        try:
            conf = max(0.0, min(1.0, float(conf_txt.replace("%", "")) / 100.0))
        except Exception:
            conf = 0.0

        item_id, item_slug = _extract_item_ref(item_cell)
        vrm, vrm_note = _extract_vrm_ref(vrm_cell)
        if not item_id or not vrm:
            continue
        fake_listing = Listing(
            platform=platform or "unknown",
            item_id=item_id,
            title=title or item_id,
            price_gbp=0.0,
            url=url or "",
            location="",
            condition="Used",
            vrm=vrm,
            raw={},
            vrm_source=source or "unknown",
            vrm_confidence=conf,
        )
        _upsert_obsidian_item_note(
            vault_db_dir, fake_listing, item_slug=item_slug, vrm_note_name=vrm_note
        )
        _upsert_obsidian_vrm_note(
            vault_db_dir, fake_listing, item_slug=item_slug, vrm_note_name=vrm_note
        )

        item_idx = f"- [[Database/Items/{item_slug}|{item_id}]]"
        vrm_idx = f"- [[Database/VRMs/{vrm_note}|{vrm}]]"
        item_links.add(item_idx)
        vrm_links.add(vrm_idx)

        item_seen.add(item_slug)
        vrm_seen.add(vrm_note)

    graph_index.write_text(
        _render_graph_index(sorted(item_links), sorted(vrm_links)),
        encoding="utf-8",
    )

    return len(item_seen), len(vrm_seen)


# ---------------------------------------------------------------------------
# Status management
# ---------------------------------------------------------------------------

def transition_lead(
    conn: sqlite3.Connection,
    lead_id: int,
    new_status: str,
    notes: str = "",
    changed_by: str = "user",
) -> Tuple[bool, str]:
    """
    Transition a lead to a new status with validation.

    Returns (success, message).
    """
    if new_status not in LEAD_STATUSES:
        return False, f"Invalid status: {new_status}"

    lead = lead_get(conn, lead_id)
    if not lead:
        return False, f"Lead {lead_id} not found"

    current = lead["status"]
    allowed = LEAD_STATUS_TRANSITIONS.get(current, [])

    if new_status not in allowed:
        return False, (
            f"Cannot transition from '{current}' to '{new_status}'. "
            f"Allowed: {', '.join(allowed) or 'none'}")

    success = lead_update_status(conn, lead_id, new_status, changed_by, notes)
    if not success:
        return False, "Database update failed"

    # Auto-set next actions based on new status
    _auto_set_next_action(conn, lead_id, new_status)

    return True, f"Lead {lead_id}: {current} → {new_status}"


def _auto_set_next_action(
    conn: sqlite3.Connection,
    lead_id: int,
    new_status: str,
) -> None:
    """Set sensible default next_action based on status transition."""
    now = datetime.now(timezone.utc)

    actions = {
        "contacted": (
            "Follow up on response",
            now + timedelta(hours=DEFAULT_FOLLOW_UP_HOURS),
        ),
        "inspecting": (
            "Complete inspection / decide to bid",
            now + timedelta(days=DEFAULT_INSPECTION_DEADLINE_DAYS),
        ),
        "bidding": (
            "Check bid status / follow up",
            now + timedelta(hours=24),
        ),
        "bought": (
            "Arrange collection / delivery",
            now + timedelta(hours=48),
        ),
        "preparing": (
            "Complete prep: valet, minor repairs, photos",
            now + timedelta(days=3),
        ),
        "listed": (
            "Monitor listing views / adjust price if needed",
            now + timedelta(days=7),
        ),
        "sold": (
            "Record final sale price and close",
            now + timedelta(hours=24),
        ),
    }

    if new_status in actions:
        action_text, due_dt = actions[new_status]
        lead_update_fields(
            conn, lead_id,
            next_action=action_text,
            next_action_due=due_dt.replace(microsecond=0).isoformat(),
        )
    elif new_status in ("closed", "lost", "withdrawn"):
        lead_update_fields(
            conn, lead_id,
            next_action="",
            next_action_due=None,
        )


# ---------------------------------------------------------------------------
# Record actuals (when a deal completes)
# ---------------------------------------------------------------------------

def record_deal_actuals(
    conn: sqlite3.Connection,
    lead_id: int,
    actual_buy_price: float,
    actual_sale_price: Optional[float] = None,
    actual_repairs: Optional[float] = None,
    actual_days_to_sell: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Record actual financial outcomes for a completed deal.

    Calculates realised_profit = sale - buy - repairs.
    Returns (success, message).
    """
    updates: Dict[str, Any] = {
        "actual_buy_price": actual_buy_price,
    }

    if actual_sale_price is not None:
        updates["actual_sale_price"] = actual_sale_price
    if actual_repairs is not None:
        updates["actual_repairs"] = actual_repairs
    if actual_days_to_sell is not None:
        updates["actual_days_to_sell"] = actual_days_to_sell

    # Calculate realised profit if we have enough data
    if actual_sale_price is not None:
        repairs = actual_repairs or 0
        updates["realised_profit"] = actual_sale_price - actual_buy_price - repairs

    success = lead_update_fields(conn, lead_id, **updates)
    if not success:
        return False, f"Failed to update lead {lead_id}"

    profit_msg = ""
    if "realised_profit" in updates:
        p = updates["realised_profit"]
        profit_msg = f" Realised profit: £{p:.0f}"

    return True, f"Actuals recorded for lead {lead_id}.{profit_msg}"


# ---------------------------------------------------------------------------
# Pipeline statistics
# ---------------------------------------------------------------------------

def pipeline_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Compute CRM pipeline statistics.

    Returns dict with counts per status, total value, conversion rates.
    """
    cur = conn.cursor()

    # Count by status
    cur.execute(
        "SELECT status, COUNT(*), SUM(expected_profit)"
        " FROM leads GROUP BY status"
    )
    by_status = {}
    total_count = 0
    total_expected = 0.0
    for status, count, expected in cur.fetchall():
        by_status[status] = {
            "count": count,
            "expected_profit": round(expected or 0, 2),
        }
        total_count += count
        total_expected += (expected or 0)

    # Active leads (not closed/lost/withdrawn)
    active = sum(
        v["count"] for k, v in by_status.items()
        if k not in ("closed", "lost", "withdrawn")
    )

    # Conversion rate: sold / (sold + lost + withdrawn)
    sold = by_status.get("sold", {}).get("count", 0)
    lost = by_status.get("lost", {}).get("count", 0)
    withdrawn = by_status.get("withdrawn", {}).get("count", 0)
    completed = sold + lost + withdrawn
    conversion = (sold / completed * 100) if completed > 0 else 0

    # Overdue follow-ups
    overdue = leads_due_followup(conn)

    # Recent activity
    cur.execute(
        "SELECT COUNT(*) FROM leads"
        " WHERE created_at >= datetime('now', '-7 days')"
    )
    new_7d = cur.fetchone()[0]

    return {
        "by_status": by_status,
        "total_leads": total_count,
        "active_leads": active,
        "total_expected_profit": round(total_expected, 2),
        "conversion_rate_pct": round(conversion, 1),
        "overdue_followups": len(overdue),
        "new_last_7d": new_7d,
    }


# ---------------------------------------------------------------------------
# Follow-up reminders
# ---------------------------------------------------------------------------

def get_pending_reminders(
    conn: sqlite3.Connection,
) -> List[Dict[str, Any]]:
    """
    Get all leads with overdue next_action_due.

    Returns list of lead dicts with reminder context.
    """
    return leads_due_followup(conn)


def snooze_reminder(
    conn: sqlite3.Connection,
    lead_id: int,
    hours: int = 24,
) -> bool:
    """Push the next_action_due forward by N hours."""
    new_due = (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).replace(microsecond=0).isoformat()
    return lead_update_fields(conn, lead_id, next_action_due=new_due)


# ---------------------------------------------------------------------------
# Seller contact management
# ---------------------------------------------------------------------------

def update_seller_contact(
    conn: sqlite3.Connection,
    lead_id: int,
    name: str = "",
    contact: str = "",
) -> bool:
    """Update seller name and contact info on a lead."""
    updates: Dict[str, Any] = {}
    if name:
        updates["seller_name"] = name
    if contact:
        updates["seller_contact"] = contact
    if not updates:
        return False
    return lead_update_fields(conn, lead_id, **updates)


# ---------------------------------------------------------------------------
# Tag management
# ---------------------------------------------------------------------------

def add_tag(conn: sqlite3.Connection, lead_id: int, tag: str) -> bool:
    """Add a tag to a lead (comma-separated in tags field)."""
    lead = lead_get(conn, lead_id)
    if not lead:
        return False
    existing = set(t.strip() for t in (lead.get("tags") or "").split(",") if t.strip())
    existing.add(tag.strip())
    return lead_update_fields(conn, lead_id, tags=",".join(sorted(existing)))


def remove_tag(conn: sqlite3.Connection, lead_id: int, tag: str) -> bool:
    """Remove a tag from a lead."""
    lead = lead_get(conn, lead_id)
    if not lead:
        return False
    existing = set(t.strip() for t in (lead.get("tags") or "").split(",") if t.strip())
    existing.discard(tag.strip())
    return lead_update_fields(conn, lead_id, tags=",".join(sorted(existing)))

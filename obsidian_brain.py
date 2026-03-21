"""
dealerly/obsidian_brain.py
==========================
Lightweight Obsidian-backed runtime memory for faster pipeline decisions.

Current capabilities:
  - Parse `Database/vrm_scans.md` from the vault.
  - Provide item_id -> VRM hints that can short-circuit enrichment calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, Optional, Tuple


_TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<ts>[^|]+?)\s*\|\s*(?P<item>.*?)\s*\|\s*(?P<vrm>.*?)\s*\|"
    r"\s*(?P<src>[^|]+?)\s*\|\s*(?P<conf>\d{1,3}%?)\s*\|\s*(?P<platform>[^|]+?)\s*\|"
    r"\s*(?P<title>.*?)\|\s*(?P<url>.*?)\|\s*$",
    re.IGNORECASE,
)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_EBAY_SLUG_RE = re.compile(r"^v1-(\d+)-(\d+)$", re.IGNORECASE)
_EBAY_PIPE_RE = re.compile(r"^v1\|(\d+)\|(\d+)$", re.IGNORECASE)


def _parse_confidence_pct(value: str) -> float:
    v = (value or "").strip().replace("%", "")
    try:
        return max(0.0, min(1.0, float(v) / 100.0))
    except Exception:
        return 0.0


def _extract_table_cell_value(cell: str) -> str:
    """Extract a display value from plain text, wikilinks, or markdown links."""
    s = (cell or "").strip()
    m = _WIKILINK_RE.search(s)
    if m:
        target = (m.group(1) or "").strip()
        label = (m.group(2) or "").strip()
        return (label or target.split("/")[-1]).strip()
    m = _MD_LINK_RE.search(s)
    if m:
        label = (m.group(1) or "").strip()
        target = (m.group(2) or "").strip()
        return (label or target.split("/")[-1].replace(".md", "")).strip()
    return s


def _item_key_aliases(item_id: str) -> tuple[str, ...]:
    """
    Return equivalent item-id representations used across Dealerly/Obsidian.
    Example:
      v1|298115097640|0 <-> v1-298115097640-0
    """
    raw = (item_id or "").strip()
    if not raw:
        return tuple()
    keys = {raw}
    m_slug = _EBAY_SLUG_RE.match(raw)
    if m_slug:
        keys.add(f"v1|{m_slug.group(1)}|{m_slug.group(2)}")
    m_pipe = _EBAY_PIPE_RE.match(raw)
    if m_pipe:
        keys.add(f"v1-{m_pipe.group(1)}-{m_pipe.group(2)}")
    return tuple(keys)


@dataclass
class ObsidianBrain:
    vault_root: Path
    vrm_by_item: Dict[str, Tuple[str, str, float]]

    @property
    def vrm_count(self) -> int:
        return len(self.vrm_by_item)

    def get_vrm_hint(self, item_id: str) -> Optional[Tuple[str, str, float]]:
        for key in _item_key_aliases(item_id):
            hit = self.vrm_by_item.get(key)
            if hit:
                return hit
        return None


def _load_vrm_scans(vault_root: Path) -> Dict[str, Tuple[str, str, float]]:
    out: Dict[str, Tuple[str, str, float]] = {}
    scans_path = vault_root / "Database" / "vrm_scans.md"
    if not scans_path.exists():
        return out
    try:
        text = scans_path.read_text(encoding="utf-8")
    except Exception:
        return out

    for line in text.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        item_id = _extract_table_cell_value(m.group("item"))
        vrm = _extract_table_cell_value(m.group("vrm")).upper().replace(" ", "")
        src = m.group("src").strip()
        conf = _parse_confidence_pct(m.group("conf"))
        if not item_id or not vrm:
            continue
        # Keep the highest-confidence hint per item (and aliases).
        for key in _item_key_aliases(item_id):
            prev = out.get(key)
            if prev is None or conf >= prev[2]:
                out[key] = (vrm, src or "obsidian_scan", conf)
    return out


def load_obsidian_brain(vault_root: Path) -> ObsidianBrain:
    return ObsidianBrain(
        vault_root=vault_root,
        vrm_by_item=_load_vrm_scans(vault_root),
    )

"""
dealerly/report.py — v0.10.0

Sprint 2 full rewrite:
  - Card-based layout replacing table rows
  - Car thumbnail per card (first_image_url from Listing)
  - Filter bar: decision, platform, ULEZ, VRM-verified, profit range
  - Sort: profit desc/asc, price asc/desc, p_MOT desc
  - Top-3 comparison strip at page top (BUY/OFFER only)
  - Dark mode toggle (CSS custom properties, localStorage + prefers-color-scheme)
  - Manual VRM entry on unverified cards (localStorage, no backend)
  - Near-miss as compact cards; AVOID in collapsible section
  - All self-contained in single HTML file — no external dependencies

Data extraction helpers (print_report, append_deal_log) preserved from v0.9.6.
"""
from __future__ import annotations

import html as html_lib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dealerly.config import DEAL_LOG_PATH, REPORTS_DIR, VERSION
from dealerly.models import DealInput, DealOutput, Listing
from dealerly.mot_formatter import format_mot_history_html
from dealerly.utils import now_utc_iso, round_to_nearest
from dealerly.vrm import is_vrm_displayable


_DECISION_COLOURS = {
    "BUY":   "#22c55e",
    "OFFER": "#f59e0b",
    "PASS":  "#94a3b8",
    "AVOID": "#ef4444",
}


def _badge(decision: str) -> str:
    colour = _DECISION_COLOURS.get(decision, "#94a3b8")
    return (
        f'<span style="background:{colour};color:#fff;padding:3px 10px;'
        f'border-radius:12px;font-weight:700;font-size:0.82em">{decision}</span>'
    )


def _platform_badge(platform: str) -> str:
    p = (platform or "").strip().lower()
    label = html_lib.escape(p.capitalize() or "Unknown")
    return (
        "<span style='background:var(--surface2);color:var(--dim);padding:3px 8px;"
        "border-radius:10px;font-weight:700;font-size:0.75em;border:1px solid var(--border)'>"
        f"{label}</span>"
    )


def _location_label(listing: Listing) -> str:
    """
    Show a useful location label; fall back to platform-safe seller labels when
    location is missing or masked.
    """
    raw_loc = (listing.location or "").strip()
    plat = (listing.platform or "").strip().lower()
    if plat == "facebook":
        loc_norm = raw_loc.lower()
        title_norm = (listing.title or "").strip().lower()
        if (
            not raw_loc
            or loc_norm == title_norm
            or loc_norm in {"london", "london, united kingdom", "united kingdom"}
        ):
            return "facebook seller"
    if raw_loc and "***" not in raw_loc:
        return html_lib.escape(raw_loc[:40])
    if plat == "ebay":
        return "ebay seller"
    if plat == "motors":
        return "motors seller"
    return html_lib.escape(f"{plat or 'unknown'} seller")


def _p_mot_label(p_mot: float, mot_history: Any) -> str:
    if p_mot >= 0.90:
        col = "#22c55e"
    elif p_mot >= 0.80:
        col = "#f59e0b"
    else:
        col = "#ef4444"
    src = "\u2713 DVSA" if mot_history else "est."
    return (
        f"<span style='color:{col};font-weight:700'>{p_mot:.0%}</span> "
        f"<small>{src}</small>"
    )


def _thumb_html(url: str, title: str) -> str:
    if not url:
        return '<div class="thumb-empty">\U0001F4F7</div>'
    su = html_lib.escape(url)
    st = html_lib.escape(title[:60])
    return (
        f'<a href="{su}" target="_blank" tabindex="-1">'
        f'<img src="{su}" alt="{st}" class="thumb" loading="lazy" '
        f"onerror=\"this.closest('.thumb-wrap').innerHTML='<div class=thumb-empty>\U0001F4F7</div>'\">"
        f'</a>'
    )


def _card_html(
    rank: int,
    listing: Listing,
    deal: DealInput,
    out: DealOutput,
    target_margin: float,
    *,
    featured: bool = False,
    compact: bool = False,
    id_suffix: str = "",
    basket_selected: bool = False,
) -> str:
    """Render a single listing as a card.

    id_suffix: appended to all element IDs to avoid duplicates when the same
    listing appears in both the top-3 strip and the main grid.
    """
    border_col = _DECISION_COLOURS.get(out.decision, "#94a3b8")
    displayable = is_vrm_displayable(listing.vrm, listing.vrm_confidence)
    raw_iid = html_lib.escape(listing.item_id)
    iid = raw_iid + id_suffix   # unique per card instance

    # VRM / manual entry block
    if displayable:
        vrm_html = (
            f"<code class='vrm-code' id='vrm-disp-{iid}'>"
            f"{html_lib.escape(listing.vrm)}</code>"
            f" <small class='dim'>({html_lib.escape(listing.vrm_source)}, "
            f"{listing.vrm_confidence:.0%})</small>"
        )
    else:
        vrm_html = (
            f"<span class='dim' id='vrm-disp-{iid}'>no VRM</span>"
            f" <form class='vrm-form' onsubmit='saveVrm(event,\"{raw_iid}\",\"{iid}\")'>"
            f"<input class='vrm-input' id='vrm-inp-{iid}' "
            f"placeholder='Enter VRM' maxlength='8' autocomplete='off'>"
            f"<button type='submit' class='vrm-btn'>Save</button>"
            f"</form>"
        )
        if out.decision == "BUY":
            vrm_html += " <small class='warn'>Verification pending</small>"

    # Write-off badge (v0.9.10)
    writeoff_badge = ""
    if listing.writeoff_category:
        writeoff_badge = (
            f'<span class="tag-writeoff">'
            f'{html_lib.escape(listing.writeoff_category)} structural risk</span>'
        )
    basket_badge = '<span class="tag-basket">\U0001F6D2 Basket</span>' if basket_selected else ""

    # ULEZ tag + filter value
    if listing.ulez_compliant is True:
        ulez = "<span class='tag-ulez-ok'>\u2713 ULEZ</span>"
        ulez_val = "yes"
    elif listing.ulez_compliant is False:
        ulez = "<span class='tag-ulez-fail'>\u2717 ULEZ fail</span>"
        ulez_val = "no"
    else:
        ulez = ""
        ulez_val = "unknown"

    # Profit colour class
    if out.expected_profit >= target_margin:
        pcls = "profit-good"
    elif out.expected_profit > 0:
        pcls = "profit-mid"
    else:
        pcls = "profit-bad"

    # Offer hint line — shown for BUY/OFFER and for PASS near-misses where
    # max_bid is within 30% of listed price (achievable negotiation range).
    offer_line = ""
    _is_near_miss_pass = (
        out.decision == "PASS"
        and out.max_bid > 0
        and out.max_bid / listing.price_gbp >= 0.70
    )
    if out.decision in ("OFFER", "BUY") or _is_near_miss_pass:
        rounded = round_to_nearest(out.max_bid, 50)
        if rounded >= listing.price_gbp:
            offer_line = (
                f"<div class='offer-hint ok'>Max bid exceeds asking \u2014 "
                f"buy at \u00a3{listing.price_gbp:.0f}</div>"
            )
        else:
            _hint_label = "Negotiate to" if _is_near_miss_pass else "Offer around"
            offer_line = (
                f"<div class='offer-hint'>{_hint_label}: "
                f"<strong>\u00a3{rounded:,}</strong></div>"
            )

    # Repair profile note
    repair_note = ""
    if deal.repair_profile_notes:
        repair_note = (
            f"<div class='repair-note'>Note: "
            f"{html_lib.escape(deal.repair_profile_notes[:90])}</div>"
        )

    # AI offer message (omit on compact cards)
    offer_msg_html = ""
    if listing.offer_message and out.decision in ("BUY", "OFFER") and not compact:
        uid = f"msg_{listing.item_id[:10]}"
        safe_msg = html_lib.escape(listing.offer_message).replace("\n", "<br>")
        offer_msg_html = (
            f"<details class='expandable'>"
            f"<summary>AI negotiation message</summary>"
            f"<div class='expand-body' id='{uid}'>{safe_msg}</div>"
            f"<button class='copy-btn' onclick=\"navigator.clipboard.writeText("
            f"document.getElementById('{uid}').innerText);"
            f"this.textContent='Copied';"
            f"setTimeout(()=>this.textContent='Copy message',1500)\">Copy message</button>"
            f"</details>"
        )

    # MOT history — always show when available, even on compact (near-miss) cards.
    # v0.9.9: removed `not compact` gate. Near-miss OFFER cards with DVSA data
    # are the most actionable listings and buyers need to see the MOT history.
    mot_html = ""
    if listing.mot_history:
        inner = format_mot_history_html(listing.mot_history, listing.vrm)
        if inner:
            n_tests = len((listing.mot_history or {}).get("motTests") or [])
            summary_label = (
                f"DVSA MOT record ({n_tests} tests)"
                if n_tests > 0 else "DVSA vehicle confirmed — no MOT records"
            )
            mot_html = (
                f"<details class='expandable'>"
                f"<summary>{summary_label}</summary>"
                f"<div class='expand-body'>{inner}</div>"
                f"</details>"
            )

    # Meta
    year_str   = str(listing.year) if listing.year else "?"
    miles_str  = f"{listing.mileage/1000:.0f}k" if listing.mileage else "?"
    mileage_tag = (
        f"<span class='tag-mileage'>{miles_str} mi</span>"
        if listing.mileage else
        "<span class='tag-mileage is-missing'>mileage n/a</span>"
    )
    plat_safe  = html_lib.escape(listing.platform)
    url_safe   = html_lib.escape(listing.url)
    title_safe = html_lib.escape(listing.title[:90])
    loc        = _location_label(listing)
    vrm_filter = "yes" if displayable else "no"

    cls = "card" + (" featured" if featured else "") + (" compact" if compact else "")
    media_meta = ""
    if featured:
        media_meta = (
            f'<div class="media-meta">{_platform_badge(listing.platform)}'
            f'<span class="card-meta">{year_str} \u00b7 {loc}</span></div>'
        )
    top_meta = "" if featured else f'<span class="card-meta">{year_str} \u00b7 {loc}</span>'
    cart_button = (
        f"<button class='cart-btn' type='button' id='cart-btn-{iid}' "
        f"onclick=\"toggleCart('{raw_iid}')\">Add to cart</button>"
    )

    return (
        f'<div class="{cls}"'
        f' data-decision="{out.decision}"'
        f' data-platform="{plat_safe}"'
        f' data-ulez="{ulez_val}"'
        f' data-vrm="{vrm_filter}"'
        f' data-profit="{out.expected_profit:.0f}"'
        f' data-price="{listing.price_gbp:.0f}"'
        f' data-pmot="{out.p_mot:.4f}"'
        f' data-basket="{"yes" if basket_selected else "no"}"'
        f' data-item="{raw_iid}"'
        f' style="border-left:4px solid {border_col}">\n'
        f'  <div class="thumb-wrap">{_thumb_html(listing.first_image_url, listing.title)}</div>\n'
        f'  {media_meta}\n'
        f'  <div class="card-body">\n'
        f'    <div class="card-top">'
        f'<span class="rank">#{rank}</span> {_badge(out.decision)} {_platform_badge(listing.platform) if not featured else ""} {basket_badge}{writeoff_badge}{ulez}'
        f'{mileage_tag}{top_meta}'
        f'</div>\n'
        f'    <a href="{url_safe}" target="_blank" class="card-title">{title_safe}</a>\n'
        f'    <div class="num-row">\n'
        f'      <div class="num"><span class="nv">\u00a3{listing.price_gbp:.0f}</span>'
        f'<span class="nl">Buy</span></div>\n'
        f'      <div class="num"><span class="nv">\u00a3{deal.expected_resale:.0f}</span>'
        f'<span class="nl">Resale</span></div>\n'
        f'      <div class="num"><span class="nv {pcls}">\u00a3{out.expected_profit:.0f}</span>'
        f'<span class="nl">Profit</span></div>\n'
        f'      <div class="num"><span class="nv">\u00a3{out.max_bid:.0f}</span>'
        f'<span class="nl">Max bid</span></div>\n'
        f'      <div class="num"><span class="nv">\u00a3{deal.base_repair_estimate:.0f}'
        f'<small style="font-weight:400"> (w/c \u00a3{deal.worst_case_repair:.0f})</small>'
        f'</span><span class="nl">Repair estimate</span></div>\n'
        f'      <div class="num"><span class="nv">'
        f'{_p_mot_label(out.p_mot, listing.mot_history)}'
        f'</span><span class="nl">p_MOT</span></div>\n'
        f'    </div>\n'
        f'    <div class="vrm-row">VRM: {vrm_html}</div>\n'
        f'    {cart_button}\n'
        f'    {offer_line}{repair_note}{offer_msg_html}{mot_html}\n'
        f'  </div>\n'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# CSS (CSS custom properties for dark mode theming)
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg:#f1f5f9; --surface:#fff; --surface2:#f8fafc;
  --text:#0f172a; --dim:#64748b; --border:#e2e8f0;
  --hover:#e2e8f0; --link:#1d4ed8;
  --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.06);
}
[data-theme=dark] {
  --bg:#0d1117; --surface:#161b22; --surface2:#21262d;
  --text:#e6edf3; --dim:#8b949e; --border:#30363d;
  --hover:#1f2937; --link:#58a6ff;
  --shadow:0 1px 3px rgba(0,0,0,.5);
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:var(--bg);color:var(--text);padding:20px 24px;line-height:1.45}
.app-shell{max-width:1680px;margin:0 auto}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
code{background:var(--surface2);padding:2px 5px;border-radius:4px;font-size:.88em}
small{font-size:.8em;color:var(--dim)}

/* header */
.page-header{display:flex;align-items:center;justify-content:space-between;
             margin-bottom:8px;flex-wrap:wrap;gap:10px}
h1{font-size:1.62em;letter-spacing:.01em;font-weight:720}
.header-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dark-toggle{padding:6px 14px;border-radius:8px;border:1.5px solid var(--border);
             background:var(--surface);color:var(--text);cursor:pointer;
             font-size:.85em;font-weight:600;transition:all .12s}
.dark-toggle:hover{border-color:var(--dim)}
.meta{color:var(--dim);font-size:.9em;margin-bottom:20px}

/* stat cards */
.stat-grid{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:28px}
.stat-card{background:var(--surface);border-radius:10px;padding:14px 18px;
           box-shadow:var(--shadow);min-width:100px}
.stat-card .sv{font-size:1.7em;font-weight:700;line-height:1.1}
.stat-card .sl{font-size:.73em;color:var(--dim);margin-top:3px}

/* section titles */
.section-title{font-size:1.05em;font-weight:700;margin:28px 0 12px;color:var(--text)}

/* top-3 grid */
.top3-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
           gap:14px;margin-bottom:4px}

/* filter bar */
.filter-bar{background:var(--surface);border-radius:14px;padding:12px 14px;
            box-shadow:var(--shadow);margin-bottom:14px;
            display:flex;flex-wrap:wrap;gap:8px;align-items:center;
            position:sticky;top:10px;z-index:40;border:1px solid var(--border);
            backdrop-filter:blur(6px)}
.filter-group{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.filter-label{font-size:.75em;font-weight:700;color:var(--dim);
              text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
.fbtn{padding:6px 13px;border-radius:10px;border:1.5px solid var(--border);
      background:var(--surface2);color:var(--text);cursor:pointer;
      font-size:.82em;font-weight:650;transition:all .14s}
.fbtn:hover{border-color:var(--dim);transform:translateY(-1px)}
.fbtn.active{color:#fff;border-color:transparent}
.fbtn[data-v=BUY].active{background:#22c55e}
.fbtn[data-v=OFFER].active{background:#f59e0b}
.fbtn[data-v=PASS].active{background:#94a3b8}
.profit-range{display:flex;align-items:center;gap:4px}
.profit-range input{width:70px;padding:4px 6px;border-radius:6px;
                    border:1.5px solid var(--border);background:var(--surface2);
                    color:var(--text);font-size:.82em}
.sort-sel{padding:5px 10px;border-radius:7px;border:1.5px solid var(--border);
          background:var(--surface2);color:var(--text);font-size:.82em;cursor:pointer}

/* cards */
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:10px}
.card{background:var(--surface);border-radius:14px;box-shadow:var(--shadow);
      display:flex;transition:box-shadow .15s,transform .15s;border:1px solid var(--border);overflow:hidden}
.card:hover{box-shadow:0 8px 22px rgba(0,0,0,.12);transform:translateY(-1px)}
.card.hidden{display:none!important}

/* thumbnail — border-radius clips the left corners without overflow:hidden on the card,
   allowing <details> expandables inside .card-body to open freely */
.thumb-wrap{flex-shrink:0;width:236px;background:transparent;
            border-radius:10px 0 0 10px;
            display:flex;align-items:center;justify-content:center;overflow:hidden}
.thumb{width:236px;height:164px;object-fit:cover;display:block}
.thumb-empty{width:236px;height:164px;display:flex;align-items:center;
             justify-content:center;font-size:1.8em;color:var(--dim)}
.card.featured{display:block}
.card.featured .thumb-wrap{width:100%;border-radius:10px 10px 0 0}
.card.featured .thumb,.card.featured .thumb-empty{width:100%;height:220px}
.media-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 12px;
           border-top:1px solid var(--border);border-bottom:1px solid var(--border);background:var(--surface2)}
.compact .thumb-wrap{width:112px}
.compact .thumb,.compact .thumb-empty{width:112px;height:84px}

/* density modes */
body[data-density=dense] .card-grid{grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:8px}
body[data-density=dense] .thumb-wrap{width:188px}
body[data-density=dense] .thumb,body[data-density=dense] .thumb-empty{width:188px;height:132px}
body[data-density=dense] .card.featured .thumb,body[data-density=dense] .card.featured .thumb-empty{height:190px}
body[data-density=dense] .card-body{padding:9px 11px}
body[data-density=dense] .num-row{gap:7px}
body[data-density=dense] .meta{margin-bottom:16px}
body[data-density=dense] .section-title{margin:22px 0 10px}

/* card body */
.card-body{flex:1;padding:11px 13px;min-width:0}
.card-top{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:5px}
.rank{font-size:.75em;font-weight:700;color:var(--dim)}
.card-meta{font-size:.74em;color:var(--dim);margin-left:auto}
.card-title{display:block;font-weight:680;font-size:.98em;margin-bottom:8px;
            color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-title:hover{color:var(--link)}

/* numbers row */
.num-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.num{display:flex;flex-direction:column}
.nv{font-size:.9em;font-weight:700}
.nl{font-size:.68em;color:var(--dim);margin-top:1px}
.profit-good{color:#22c55e}
.profit-mid{color:#f59e0b}
.profit-bad{color:#ef4444}

/* tags */
.tag-ulez-ok{font-size:.75em;color:#22c55e;font-weight:600}
.tag-ulez-fail{font-size:.75em;color:#ef4444;font-weight:600}
.tag-mileage{font-size:.72em;color:#0369a1;background:#e0f2fe;padding:2px 8px;
             border-radius:8px;font-weight:700}
.tag-mileage.is-missing{color:#64748b;background:#e2e8f0}
[data-theme=dark] .tag-mileage{color:#bae6fd;background:#0b3350}
[data-theme=dark] .tag-mileage.is-missing{color:#94a3b8;background:#1f2937}
.tag-writeoff{font-size:.75em;color:#7a4f10;background:#f8e8bd;padding:2px 8px;
              border-radius:8px;font-weight:700;margin-right:4px}
[data-theme=dark] .tag-writeoff{color:#f3d598;background:#3a2f1c}
.tag-basket{font-size:.75em;color:#0c4a6e;background:#bae6fd;padding:2px 8px;
            border-radius:8px;font-weight:700;margin-right:4px}
[data-theme=dark] .tag-basket{color:#bae6fd;background:#0b3350}
.warn{color:#7a4f10;font-weight:600}
[data-theme=dark] .warn{color:#d9b16d}
.dim{color:var(--dim)}

/* vrm */
.vrm-row{font-size:.82em;margin-bottom:5px;display:flex;align-items:center;
         flex-wrap:wrap;gap:4px}
.vrm-form{display:inline-flex;gap:4px;align-items:center}
.vrm-input{padding:3px 6px;border-radius:5px;border:1.5px solid var(--border);
           background:var(--surface2);color:var(--text);font-size:.9em;width:88px}
.vrm-btn{padding:3px 8px;border-radius:5px;border:1.5px solid var(--border);
         background:var(--surface);color:var(--text);cursor:pointer;font-size:.8em}
.vrm-saved{font-weight:700;color:#22c55e}
.cart-btn{margin:4px 0 6px;padding:6px 10px;border-radius:8px;border:1px solid var(--border);
         background:var(--surface2);color:var(--text);font-size:.78em;font-weight:600;cursor:pointer}
.cart-btn:hover{border-color:var(--dim)}
.cart-btn.in-cart{background:#0f3f2a;border-color:#22c55e;color:#d1fae5}
.cart-clear{padding:5px 9px;border-radius:8px;border:1px solid var(--border);
         background:var(--surface2);color:var(--dim);font-size:.76em;font-weight:600;cursor:pointer}
.cart-clear:hover{color:var(--text);border-color:var(--dim)}
.cart-open{padding:5px 9px;border-radius:8px;border:1px solid var(--border);
        background:var(--surface2);color:var(--text);font-size:.76em;font-weight:600;cursor:pointer}
.cart-open:hover{border-color:var(--dim)}
.cart-panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;
        padding:10px 12px;margin-bottom:12px;box-shadow:var(--shadow)}
.cart-panel.hidden{display:none}
.cart-panel-title{font-size:.9em;font-weight:700;margin-bottom:6px}
.cart-panel-empty{font-size:.82em;color:var(--dim)}
.cart-panel-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px}
.cart-row{border:1px solid var(--border);border-radius:8px;padding:8px;background:var(--surface2)}
.cart-row a{display:block;font-size:.84em;font-weight:650;margin-bottom:3px;color:var(--text);
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cart-row-meta{font-size:.78em;color:var(--dim)}

/* hints */
.offer-hint{font-size:.82em;font-weight:600;color:#f59e0b;margin-bottom:4px}
.offer-hint.ok{color:#22c55e}
.repair-note{font-size:.76em;color:#7a4f10;margin-bottom:4px;padding:4px 8px;
             background:#f7ead1;border-radius:6px}
[data-theme=dark] .repair-note{background:#342a1f;color:#d9b16d}

/* expandables */
.expandable{margin-top:6px}
.expandable summary{cursor:pointer;font-size:.8em;color:var(--link);
                    user-select:none;list-style:none;display:inline-flex;
                    align-items:center;gap:4px}
.expandable summary::-webkit-details-marker{display:none}
.expandable summary::before{content:'\\25b6';font-size:.65em;
                             display:inline-block;transition:transform .15s}
.expandable[open] summary::before{transform:rotate(90deg)}
.expand-body{margin-top:6px;font-size:.82em;line-height:1.5;
             background:var(--surface2);border-radius:6px;
             padding:8px 10px;border:1px solid var(--border)}
.copy-btn{margin-top:5px;padding:3px 10px;border-radius:5px;
          border:1px solid var(--border);background:var(--surface);
          color:var(--text);cursor:pointer;font-size:.78em}

/* near-miss grid */
.nearmiss-grid{display:grid;
               grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
               gap:10px;margin-top:8px}

/* avoid toggle */
.avoid-toggle{cursor:pointer;color:#7a4f10;font-weight:700;font-size:1em;
              user-select:none;padding:8px 0;margin-top:28px;display:block}
[data-theme=dark] .avoid-toggle{color:#d9b16d}

/* footer */
.footer{margin-top:36px;color:var(--dim);font-size:.78em;
        border-top:1px solid var(--border);padding-top:12px}
"""


# ---------------------------------------------------------------------------
# JavaScript (dark mode + filters + manual VRM)
# ---------------------------------------------------------------------------

_JS = r"""
/* Dark mode — applied before paint via inline script in <head> */
function toggleDark() {
  const cur  = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('dealerly_theme', next);
  const btn = document.getElementById('dark-btn');
  if (btn) btn.textContent = next === 'dark' ? '\u2600 Light' : '\u263e Dark';
}

function toggleDensity() {
  const body = document.body;
  if (!body) return;
  const cur = body.getAttribute('data-density') || 'comfortable';
  const next = cur === 'dense' ? 'comfortable' : 'dense';
  body.setAttribute('data-density', next);
  localStorage.setItem('dealerly_density', next);
  const btn = document.getElementById('density-btn');
  if (btn) btn.textContent = next === 'dense' ? 'Comfortable View' : 'Dense View';
}

/* Manual VRM — restore saved values from localStorage on load.
   Uses data-item querySelectorAll so BOTH the top-3 strip instance and the
   main grid instance of the same listing are updated together. */
function _applyVrm(itemId, val) {
  document.querySelectorAll(`.card[data-item="${itemId}"]`).forEach(card => {
    const disp = card.querySelector('[id^="vrm-disp-"]');
    const form = card.querySelector('.vrm-form');
    if (disp) { disp.textContent = val; disp.className = 'vrm-saved'; }
    if (form) form.style.display = 'none';
  });
}

(function () {
  const seen = new Set();
  document.querySelectorAll('.card[data-item]').forEach(card => {
    const id = card.dataset.item;
    if (seen.has(id)) return;
    seen.add(id);
    const val = localStorage.getItem('vrm_' + id);
    if (val) _applyVrm(id, val);
  });
})();

function saveVrm(ev, itemId, iid) {
  ev.preventDefault();
  const inp = document.getElementById('vrm-inp-' + iid);
  const val = inp.value.trim().toUpperCase().replace(/\s/g, '');
  if (!val) return;
  localStorage.setItem('vrm_' + itemId, val);
  _applyVrm(itemId, val);
}

const CART_KEY = 'dealerly_cart_v1';
function _loadCart() {
  try {
    const raw = localStorage.getItem(CART_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch (_) {
    return new Set();
  }
}
function _saveCart(setObj) {
  localStorage.setItem(CART_KEY, JSON.stringify(Array.from(setObj)));
}
function _renderCartButtons() {
  const cart = _loadCart();
  document.querySelectorAll('.card[data-item]').forEach(card => {
    const id = card.dataset.item;
    const btn = card.querySelector('.cart-btn');
    if (!btn) return;
    const inCart = cart.has(id);
    btn.classList.toggle('in-cart', inCart);
    btn.textContent = inCart ? 'Remove from cart' : 'Add to cart';
  });
}
function _renderCartSummary() {
  const cart = _loadCart();
  let spend = 0, profit = 0;
  document.querySelectorAll('.card[data-item]').forEach(card => {
    if (!cart.has(card.dataset.item)) return;
    spend += parseFloat(card.dataset.price || '0') || 0;
    profit += parseFloat(card.dataset.profit || '0') || 0;
  });
  const el = document.getElementById('cart-summary');
  if (!el) return;
  const budget = parseFloat(el.dataset.budget || '0') || 0;
  const left = Math.max(0, budget - spend);
  el.textContent = `Cart ${cart.size} · £${Math.round(spend)} spend · £${Math.round(left)} left · ~£${Math.round(profit)} profit`;
  const cartBtn = document.getElementById('cart-open-btn');
  if (cartBtn) cartBtn.textContent = `Open cart (${cart.size})`;
}
function _renderCartPanel() {
  const cart = _loadCart();
  const panel = document.getElementById('cart-panel');
  const listEl = document.getElementById('cart-panel-list');
  const emptyEl = document.getElementById('cart-panel-empty');
  if (!panel || !listEl || !emptyEl) return;
  const cards = Array.from(document.querySelectorAll('#main-grid .card[data-item]'));
  const seen = new Set();
  const rows = [];
  cards.forEach(card => {
    const id = card.dataset.item;
    if (!id || !cart.has(id) || seen.has(id)) return;
    seen.add(id);
    const titleEl = card.querySelector('.card-title');
    const title = titleEl ? titleEl.textContent.trim() : id;
    const href = titleEl ? titleEl.getAttribute('href') : '#';
    const price = Math.round(parseFloat(card.dataset.price || '0') || 0);
    const profit = Math.round(parseFloat(card.dataset.profit || '0') || 0);
    rows.push(
      `<div class="cart-row"><a href="${href}" target="_blank">${title}</a>` +
      `<div class="cart-row-meta">Buy £${price} · Profit ~£${profit}</div></div>`
    );
  });
  listEl.innerHTML = rows.join('');
  emptyEl.style.display = rows.length ? 'none' : 'block';
}
function toggleCartPanel() {
  const panel = document.getElementById('cart-panel');
  if (!panel) return;
  panel.classList.toggle('hidden');
  if (!panel.classList.contains('hidden')) _renderCartPanel();
}
function toggleCart(itemId) {
  const cart = _loadCart();
  if (cart.has(itemId)) cart.delete(itemId); else cart.add(itemId);
  _saveCart(cart);
  _renderCartButtons();
  _renderCartSummary();
  _renderCartPanel();
}
function clearCart() {
  _saveCart(new Set());
  _renderCartButtons();
  _renderCartSummary();
  _renderCartPanel();
}

/* Filter + sort state */
const state = {
  dec: 'ALL', plat: 'ALL', ulez: 'ALL', vrm: 'ALL',
  profMin: -99999, profMax: 99999, sort: 'profit_desc'
};

function applyFilters() {
  const grid = document.getElementById('main-grid');
  if (!grid) return;
  const cards = Array.from(grid.querySelectorAll('.card'));

  cards.forEach(c => {
    const show =
      (state.dec  === 'ALL' || c.dataset.decision === state.dec)  &&
      (state.plat === 'ALL' || c.dataset.platform === state.plat) &&
      (state.ulez === 'ALL' || c.dataset.ulez     === state.ulez) &&
      (state.vrm  === 'ALL' || c.dataset.vrm      === state.vrm)  &&
      parseFloat(c.dataset.profit) >= state.profMin &&
      parseFloat(c.dataset.profit) <= state.profMax;
    c.classList.toggle('hidden', !show);
  });

  const visible = cards.filter(c => !c.classList.contains('hidden'));
  visible.sort((a, b) => {
    switch (state.sort) {
      case 'profit_desc': return parseFloat(b.dataset.profit) - parseFloat(a.dataset.profit);
      case 'profit_asc':  return parseFloat(a.dataset.profit) - parseFloat(b.dataset.profit);
      case 'price_asc':   return parseFloat(a.dataset.price)  - parseFloat(b.dataset.price);
      case 'price_desc':  return parseFloat(b.dataset.price)  - parseFloat(a.dataset.price);
      case 'pmot_desc':   return parseFloat(b.dataset.pmot)   - parseFloat(a.dataset.pmot);
    }
    return 0;
  });
  visible.forEach(c => grid.appendChild(c));

  const cnt = document.getElementById('result-count');
  if (cnt) cnt.textContent = visible.length + ' listing' + (visible.length !== 1 ? 's' : '');
  _renderCartButtons();
  _renderCartSummary();
  _renderCartPanel();
}

function setFilter(key, val, el) {
  state[key] = val;
  if (el) {
    const grp = el.closest('.filter-group');
    if (grp) grp.querySelectorAll('.fbtn').forEach(b => b.classList.remove('active'));
    el.classList.add('active');
  }
  applyFilters();
}

function setProfitRange() {
  const lo = document.getElementById('prof-min');
  const hi = document.getElementById('prof-max');
  state.profMin = (lo && lo.value !== '') ? parseFloat(lo.value) : -99999;
  state.profMax = (hi && hi.value !== '') ? parseFloat(hi.value) :  99999;
  applyFilters();
}

function setSort(sel) {
  state.sort = sel.value;
  applyFilters();
}
"""


def write_loading_screen(
    *,
    progress_pct: int,
    stage_text: str,
    done: bool = False,
    report_path: str = "",
) -> str:
    """
    Write/update a local loading screen used while pipeline phases run.

    When done=True, the page fades out and redirects to report_path in-place.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    loading_path = REPORTS_DIR / "dealerly_loading.html"
    state_path = REPORTS_DIR / "dealerly_loading_state.json"
    pct = max(0, min(100, int(progress_pct)))
    stage_safe = html_lib.escape(stage_text or "Working...")
    done_text = "true" if done else "false"
    report_uri = Path(report_path).resolve().as_uri() if report_path else ""
    report_uri_safe = html_lib.escape(report_uri)
    try:
        state_path.write_text(
            json.dumps(
                {
                    "pct": pct,
                    "stage": stage_text or "Working...",
                    "done": bool(done),
                    "report_url": report_uri,
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Dealerly Loading</title>
  <style>
    :root {{
      --bg: #0d1117;
      --surface: #161b22;
      --border: #30363d;
      --text: #e6edf3;
      --dim: #8b949e;
      --accent: #58a6ff;
      --accent2: #22c55e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: radial-gradient(circle at 20% 20%, #1b2432 0%, var(--bg) 55%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      transition: opacity .45s ease, transform .45s ease;
    }}
    body.fade-out {{
      opacity: 0;
      transform: scale(0.985);
    }}
    .shell {{
      width: min(680px, 92vw);
      background: linear-gradient(180deg, #161b22, #0f151d);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 28px 24px;
      box-shadow: 0 16px 40px rgba(0,0,0,.35);
    }}
    .title {{
      font-size: 1.35rem;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .subtitle {{
      color: var(--dim);
      font-size: .93rem;
      margin-bottom: 20px;
    }}
    .bar {{
      width: 100%;
      height: 12px;
      border-radius: 999px;
      background: #0b1324;
      border: 1px solid #1e293b;
      overflow: hidden;
    }}
    .fill {{
      height: 100%;
      width: {pct}%;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent), var(--accent2), var(--accent));
      background-size: 220% 100%;
      animation: flow 1.4s linear infinite;
      transition: width .5s ease;
    }}
    @keyframes flow {{
      from {{ background-position: 0% 50%; }}
      to {{ background-position: 200% 50%; }}
    }}
    .row {{
      margin-top: 12px;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 8px;
      font-size: .92rem;
    }}
    .stage {{ color: #cbd5e1; flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }}
    .pct {{ font-weight: 700; color: #86efac; min-width: 56px; text-align: right; flex: 0 0 auto; }}
    .pulse {{
      margin-top: 16px;
      color: var(--dim);
      font-size: .85rem;
      letter-spacing: .01em;
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="title">Dealerly is building your report</div>
    <div class="subtitle">Sourcing, enrichment, scoring, and workflow sync in progress.</div>
    <div class="bar"><div class="fill"></div></div>
    <div class="row">
      <span class="stage">{stage_safe}</span>
      <span class="pct">{pct}%</span>
    </div>
    <div class="pulse">Please keep this window open.</div>
  </div>
  <script>
    const stageEl = document.querySelector('.stage');
    const pctEl = document.querySelector('.pct');
    const fillEl = document.querySelector('.fill');
    const localDone = {done_text};
    const localReport = "{report_uri_safe}";
    let redirected = false;

    function applyState(s) {{
      if (!s) return;
      const pct = Math.max(0, Math.min(100, parseInt(s.pct ?? 0, 10)));
      if (fillEl) fillEl.style.width = pct + '%';
      if (pctEl) pctEl.textContent = pct + '%';
      if (stageEl && typeof s.stage === 'string') stageEl.textContent = s.stage;
      if (!redirected && s.done && s.report_url) {{
        redirected = true;
        let target = s.report_url;
        if (window.location.protocol.startsWith('http') && String(target).startsWith('file://')) {{
          target = '/open-report?u=' + encodeURIComponent(String(target));
        }}
        document.body.classList.add('fade-out');
        setTimeout(() => window.location.href = target, 700);
        setTimeout(() => {{
          if (window.location.pathname.includes('/loading')) {{
            const shell = document.querySelector('.shell');
            if (shell) {{
              const a = document.createElement('a');
              a.href = target;
              a.textContent = 'Open report now';
              a.style.display = 'inline-block';
              a.style.marginTop = '10px';
              a.style.color = '#93c5fd';
              shell.appendChild(a);
            }}
          }}
        }}, 2200);
      }}
    }}

    if (localDone && localReport) {{
      applyState({{pct:{pct}, stage:{json.dumps(stage_text or "Working...", ensure_ascii=True)}, done:true, report_url:localReport}});
    }} else if (window.location.protocol.startsWith('http')) {{
      const poll = async () => {{
        try {{
          const r = await fetch('/loading-state?_=' + Date.now(), {{cache: 'no-store'}});
          if (!r.ok) return;
          const s = await r.json();
          applyState(s);
        }} catch (_e) {{}}
      }};
      poll();
      setInterval(poll, 700);
    }} else {{
      // File URI fallback: periodic reload to pick up rewritten file content.
      setInterval(() => {{
        if (!redirected) window.location.reload();
      }}, 1400);
    }}
  </script>
</body>
</html>"""

    # Atomic write to avoid transient blank reads while /loading is being served.
    tmp_path = loading_path.with_suffix(".html.tmp")
    tmp_path.write_text(html, encoding="utf-8")
    tmp_path.replace(loading_path)
    return str(loading_path)


# ---------------------------------------------------------------------------
# Main report generator
# ---------------------------------------------------------------------------

def generate_html_report(
    rows, near_miss, *, capital, price_min, price_max, mode,
    target_margin, stats, platforms, at_used,
    avoid_rows=None, enrich_stats=None, platform_results=None,
    basket_item_ids=None, basket_budget=0.0, basket_spend=0.0, basket_profit=0.0,
):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    report_path = REPORTS_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

    # ---- Stat cards ----
    all_disp   = list(rows) + list(near_miss or []) + list(avoid_rows or [])
    vrm_top    = sum(1 for l, _, _ in rows if l.vrm)
    dvsa_top   = sum(1 for l, _, _ in rows if l.mot_history)
    vrm_all    = sum(1 for l, _, _ in all_disp if l.vrm)
    dvsa_all   = sum(1 for l, _, _ in all_disp if l.mot_history)
    n_top      = len(rows)
    n_all      = len(all_disp)
    top_plat_counts: Dict[str, int] = {}
    for l, _, _ in rows:
        key = (l.platform or "unknown").lower()
        top_plat_counts[key] = top_plat_counts.get(key, 0) + 1
    plat_mix = " | ".join(
        f"{k}:{v}" for k, v in sorted(top_plat_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ) if top_plat_counts else "n/a"
    basket_ids = set(basket_item_ids or [])
    basket_count = sum(1 for l, _, _ in rows if l.item_id in basket_ids)

    enrich_card_html = ""
    if enrich_stats and enrich_stats.get("enriched_total", 0) > 0:
        et = enrich_stats["enriched_total"] + enrich_stats.get("enriched_p45", 0)
        ef = enrich_stats.get("vrm_found_p3", 0) + enrich_stats.get("vrm_found_p45", 0)
        enrich_card_html = (
            f'<div class="stat-card"><div class="sv" style="color:#7c3aed">'
            f'{ef}/{et}</div><div class="sl">VRM enriched</div></div>'
        )

    # Build Sources string — show active platforms normally, zero-result (blocked)
    # platforms as a yellow warning badge so it's clear they were attempted.
    if platform_results:
        active_plats  = sorted(p for p, n in platform_results.items() if n > 0)
        blocked_plats = sorted(p for p, n in platform_results.items() if n == 0)
        plat_str = " + ".join(active_plats) if active_plats else "eBay"
        for p in blocked_plats:
            plat_str += (
                f' <span style="background:#fef3c7;color:#92400e;padding:2px 7px;'
                f'border-radius:8px;font-size:.78em" title="0 listings — blocked or empty">'
                f'⚠ {p}</span>'
            )
    else:
        plat_str = " + ".join(sorted(set(platforms))) or "eBay"
    at_badge = (
        ' <span style="background:#e0f2fe;color:#0369a1;padding:2px 8px;'
        'border-radius:8px;font-size:.78em">AutoTrader</span>'
        if at_used else ""
    )

    stat_html = (
        f'<div class="stat-grid">'
        f'<div class="stat-card"><div class="sv" style="color:#22c55e">{stats.get("buy",0)}</div>'
        f'<div class="sl">BUY</div></div>'
        f'<div class="stat-card"><div class="sv" style="color:#f59e0b">{stats.get("offer",0)}</div>'
        f'<div class="sl">OFFER</div></div>'
        f'<div class="stat-card"><div class="sv" style="color:#94a3b8">{stats.get("pass",0)}</div>'
        f'<div class="sl">PASS</div></div>'
        f'<div class="stat-card"><div class="sv" style="color:#ef4444">{stats.get("avoid_shock",0)}</div>'
        f'<div class="sl">AVOID</div></div>'
        f'<div class="stat-card"><div class="sv">{stats.get("total",0)}</div>'
        f'<div class="sl">Scored</div></div>'
        f'<div class="stat-card"><div class="sv" style="color:#0369a1">'
        f'{vrm_top}/{n_top} top \u00b7 {vrm_all}/{n_all} all'
        f'</div><div class="sl">VRMs found</div></div>'
        f'<div class="stat-card"><div class="sv" style="color:#0369a1">'
        f'{dvsa_top}/{n_top} top \u00b7 {dvsa_all}/{n_all} all'
        f'</div><div class="sl">DVSA verified</div></div>'
        f'<div class="stat-card"><div class="sv" style="color:#06b6d4">'
        f'{basket_count} \u00b7 \u00a3{basket_spend:.0f}/\u00a3{basket_budget:.0f}'
        f'</div><div class="sl">Budget basket</div></div>'
        f'<div class="stat-card"><div class="sv" style="color:#64748b">{plat_mix}</div>'
        f'<div class="sl">Top platform mix</div></div>'
        f'{enrich_card_html}'
        f'</div>'
    )

    # ---- Top-3 comparison strip ----
    top3_cands = [(l, d, o) for l, d, o in rows if o.decision in ("BUY", "OFFER")][:3]
    top3_html = ""
    if top3_cands:
        cards_str = "\n".join(
            _card_html(i + 1, l, d, o, target_margin, featured=True, id_suffix="f")
            for i, (l, d, o) in enumerate(top3_cands)
        )
        top3_html = (
            f'<div class="section-title">'
            f'Top {len(top3_cands)} Opportunities</div>'
            f'<div class="top3-grid">{cards_str}</div>'
        )
    elif near_miss:
        # No pure BUY/OFFER this run — surface best near-misses in Act Now
        # so there's always something actionable at the top of the report.
        nm_featured = near_miss[:3]
        nm_cards_str = "\n".join(
            _card_html(i + 1, l, d, o, target_margin, featured=True, id_suffix="f")
            for i, (l, d, o) in enumerate(nm_featured)
        )
        _disc_pct = lambda x: int((1 - x[2].max_bid / x[0].price_gbp) * 100)
        _best_disc = _disc_pct(nm_featured[0])
        top3_html = (
            f'<div class="section-title">'
            f'Top {len(nm_featured)} Negotiation Opportunities'
            f'<small style="font-weight:400;color:var(--dim);font-size:.82em">'
            f' \u2014 offer at max bid (~{_best_disc}% below asking) to unlock profit'
            f'</small></div>'
            f'<div class="top3-grid">{nm_cards_str}</div>'
        )

    # ---- Filter bar ----
    plat_options = sorted({l.platform for l, _, _ in rows})
    plat_btns = "".join(
        f'<button class="fbtn" data-v="{html_lib.escape(p)}" '
        f"onclick=\"setFilter('plat','{html_lib.escape(p)}',this)\">"
        f"{html_lib.escape(p).capitalize()}</button>"
        for p in plat_options
    )
    plat_group = ""
    if len(plat_options) > 1:
        plat_group = (
            f'<div class="filter-group">'
            f'<span class="filter-label">Platform</span>'
            f'<button class="fbtn active" data-v="ALL" '
            f"onclick=\"setFilter('plat','ALL',this)\">All</button>"
            f"{plat_btns}</div>"
        )

    filter_bar_html = (
        f'<div class="filter-bar">'
        f'<div class="filter-group">'
        f'<span class="filter-label">Decision</span>'
        f'<button class="fbtn active" data-v="ALL" onclick="setFilter(\'dec\',\'ALL\',this)">All</button>'
        f'<button class="fbtn" data-v="BUY"   onclick="setFilter(\'dec\',\'BUY\',this)">BUY</button>'
        f'<button class="fbtn" data-v="OFFER" onclick="setFilter(\'dec\',\'OFFER\',this)">OFFER</button>'
        f'<button class="fbtn" data-v="PASS"  onclick="setFilter(\'dec\',\'PASS\',this)">PASS</button>'
        f'</div>'
        f'{plat_group}'
        f'<div class="filter-group">'
        f'<span class="filter-label">ULEZ</span>'
        f'<button class="fbtn active" data-v="ALL" onclick="setFilter(\'ulez\',\'ALL\',this)">All</button>'
        f'<button class="fbtn" data-v="yes" onclick="setFilter(\'ulez\',\'yes\',this)">\u2713 ULEZ</button>'
        f'<button class="fbtn" data-v="no"  onclick="setFilter(\'ulez\',\'no\',this)">Fail</button>'
        f'</div>'
        f'<div class="filter-group">'
        f'<span class="filter-label">VRM</span>'
        f'<button class="fbtn active" data-v="ALL" onclick="setFilter(\'vrm\',\'ALL\',this)">All</button>'
        f'<button class="fbtn" data-v="yes" onclick="setFilter(\'vrm\',\'yes\',this)">Found</button>'
        f'<button class="fbtn" data-v="no"  onclick="setFilter(\'vrm\',\'no\',this)">Missing</button>'
        f'</div>'
        f'<div class="filter-group">'
        f'<span class="filter-label">Profit \u00a3</span>'
        f'<div class="profit-range">'
        f'<input type="number" id="prof-min" placeholder="min" oninput="setProfitRange()">'
        f'<span style="color:var(--dim)">\u2013</span>'
        f'<input type="number" id="prof-max" placeholder="max" oninput="setProfitRange()">'
        f'</div></div>'
        f'<div class="filter-group">'
        f'<span class="filter-label">Sort</span>'
        f'<select class="sort-sel" onchange="setSort(this)">'
        f'<option value="profit_desc">Profit \u2193</option>'
        f'<option value="profit_asc">Profit \u2191</option>'
        f'<option value="price_asc">Price \u2191</option>'
        f'<option value="price_desc">Price \u2193</option>'
        f'<option value="pmot_desc">p_MOT \u2193</option>'
        f'</select></div>'
        f'<span id="cart-summary" data-budget="{capital:.0f}" style="font-size:.8em;color:var(--dim);min-width:220px"></span>'
        f'<button class="cart-open" id="cart-open-btn" type="button" onclick="toggleCartPanel()">Open cart (0)</button>'
        f'<button class="cart-clear" type="button" onclick="clearCart()">Clear cart</button>'
        f'<span id="result-count" style="font-size:.8em;color:var(--dim);margin-left:auto"></span>'
        f'</div>'
    )

    # ---- Main card grid ----
    main_cards = "\n".join(
        _card_html(i, l, d, o, target_margin, basket_selected=(l.item_id in basket_ids))
        for i, (l, d, o) in enumerate(rows, 1)
    )
    basket_strip = ""
    if basket_ids:
        basket_strip = (
            f'<div class="section-title">Basket Plan '
            f'<span style="color:var(--dim);font-weight:400;font-size:.85em">'
            f'(\u00a3{basket_spend:.0f} spend \u00b7 \u00a3{max(0.0, basket_budget-basket_spend):.0f} left '
            f'\u00b7 ~\u00a3{basket_profit:.0f} projected profit)</span></div>'
        )
    main_grid_html = (
        f'{basket_strip}'
        f'<div class="section-title">All Listings '
        f'<span style="color:var(--dim);font-weight:400;font-size:.85em">({len(rows)})</span></div>'
        f'{filter_bar_html}'
        f'<div class="cart-panel hidden" id="cart-panel">'
        f'<div class="cart-panel-title">Cart review</div>'
        f'<div class="cart-panel-empty" id="cart-panel-empty">No listings selected yet.</div>'
        f'<div class="cart-panel-list" id="cart-panel-list"></div>'
        f'</div>'
        f'<div class="card-grid" id="main-grid">{main_cards}</div>'
    )

    # ---- Near-miss ----
    near_miss_html = ""
    if near_miss:
        nm_cards = "\n".join(
            _card_html(i, l, d, o, target_margin, compact=True)
            for i, (l, d, o) in enumerate(near_miss, 1)
        )
        near_miss_html = (
            f'<div class="section-title">Near-Miss \u2014 Worth Negotiating '
            f'<span style="color:var(--dim);font-weight:400;font-size:.85em">'
            f'(listed just above your max bid)</span></div>'
            f'<div class="nearmiss-grid">{nm_cards}</div>'
        )

    # ---- AVOID ----
    avoid_html = ""
    if avoid_rows:
        av_cards = "\n".join(
            _card_html(i, l, d, o, target_margin, compact=True)
            for i, (l, d, o) in enumerate(avoid_rows, 1)
        )
        avoid_html = (
            f'<details>'
            f'<summary class="avoid-toggle">'
            f'Review Recommended ({len(avoid_rows)} listings) \u2014 click to expand'
            f'</summary>'
            f'<p style="font-size:.85em;color:#7a4f10;margin:8px 0">'
            f'These listings need a second look (verification gaps, stronger risk signals, or margin pressure). '
            f'Use this section as a manual review queue before bidding.</p>'
            f'<div class="nearmiss-grid">{av_cards}</div>'
            f'</details>'
        )

    # ---- Assemble ----
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Dealerly {VERSION} \u2014 {ts}</title>
  <style>{_CSS}</style>
  <script>
    /* Apply saved/preferred theme before first paint (avoids flash) */
    (function(){{
      const s = localStorage.getItem('dealerly_theme');
      document.documentElement.setAttribute('data-theme', s || 'dark');
      const d = localStorage.getItem('dealerly_density') || 'comfortable';
      document.addEventListener('DOMContentLoaded', () => {{
        document.body.setAttribute('data-density', d);
      }});
    }})();
  </script>
</head>
<body>
  <div class="app-shell">
  <div class="page-header">
    <h1>Dealerly {VERSION} \u2014 Deal Intelligence</h1>
    <div class="header-actions">
      <button class="dark-toggle" id="density-btn" onclick="toggleDensity()">Dense View</button>
      <button class="dark-toggle" id="dark-btn" onclick="toggleDark()">\u263e Dark</button>
    </div>
  </div>
  <p class="meta">
    {html_lib.escape(ts)} &nbsp;|&nbsp; Capital: \u00a3{capital:.0f}
    &nbsp;|&nbsp; Band: \u00a3{price_min}\u2013\u00a3{price_max}
    &nbsp;|&nbsp; {html_lib.escape(mode)}
    &nbsp;|&nbsp; Sources: {plat_str}{at_badge}
  </p>
  {stat_html}
  {top3_html}
  {main_grid_html}
  {near_miss_html}
  {avoid_html}
  <div class="footer">
    Dealerly {VERSION} \u2014 SWAutos &nbsp;|&nbsp;
    Estimates only. Always verify before bidding.
  </div>
  </div>
  <script>{_JS}</script>
  <script>
    window.addEventListener('DOMContentLoaded', function() {{
      const density = localStorage.getItem('dealerly_density') || 'comfortable';
      document.body.setAttribute('data-density', density);
      const densBtn = document.getElementById('density-btn');
      if (densBtn) densBtn.textContent = density === 'dense' ? 'Comfortable View' : 'Dense View';
      _renderCartButtons();
      _renderCartSummary();
      applyFilters();
      const theme = document.documentElement.getAttribute('data-theme');
      const btn = document.getElementById('dark-btn');
      if (btn) btn.textContent = theme === 'dark' ? '\u2600 Light' : '\u263e Dark';
    }});
  </script>
</body>
</html>"""

    report_path.write_text(html, encoding="utf-8")
    return str(report_path)


# ---------------------------------------------------------------------------
# Console report — unchanged from v0.9.4
# ---------------------------------------------------------------------------

def print_report(rows, *, capital, price_min, price_max, mode,
                 target_margin, holding_cost, ebay_fee_rate, pay_fee_rate,
                 admin_buffer, transport_buffer,
                 basket_rows=None, basket_spend: float = 0.0, basket_profit: float = 0.0):
    print(f"\n=== Dealerly {VERSION} \u2014 SWAutos Flip Opportunities ===")
    print(datetime.now().strftime("%Y-%m-%d %H:%M"))
    print(f"Mode: {mode}")
    print(f"Capital: \u00a3{capital:.0f} | Margin target: \u00a3{target_margin:.0f} | Holding: \u00a3{holding_cost:.0f}")
    print(f"Band: \u00a3{price_min}\u2013\u00a3{price_max}")
    if basket_rows:
        print(
            f"Budget basket: {len(basket_rows)} car(s) | spend \u00a3{basket_spend:.0f} "
            f"| projected profit \u00a3{basket_profit:.0f} | cash left \u00a3{max(0.0, capital-basket_spend):.0f}"
        )
    print("-" * 50)

    if not rows:
        print("No candidates returned.")
        return

    for i, (listing, deal, out) in enumerate(rows, 1):
        ulez = (
            " \u2713ULEZ" if listing.ulez_compliant
            else (" \u2717ULEZ-FAIL" if listing.ulez_compliant is False else "")
        )
        print(f"\n{i}. {listing.title[:90]}{ulez}")
        print(f"   Buy: \u00a3{listing.price_gbp:.0f} | Resale: \u00a3{deal.expected_resale:.0f} | Profit: \u00a3{out.expected_profit:.0f}")
        repair_note = f" | {deal.repair_profile_notes[:60]}" if deal.repair_profile_notes else ""
        print(f"   Repairs: \u00a3{deal.base_repair_estimate:.0f}\u2013\u00a3{deal.worst_case_repair:.0f}{repair_note}")
        print(f"   Max bid: \u00a3{out.max_bid:.0f} | Shock: {out.shock_impact_ratio:.2f} | p_mot={out.p_mot:.0%}")
        print(f"   Decision: {out.decision} \u2014 {out.reason}")
        if out.decision == "OFFER":
            print(f"   >>> Offer: \u00a3{max(0.0, out.max_bid):.0f}")
        if is_vrm_displayable(listing.vrm, listing.vrm_confidence):
            vrm_line = f"{listing.vrm} [{listing.vrm_source} {listing.vrm_confidence:.0%}]"
        else:
            vrm_line = "no VRM"
        print(f"   VRM: {vrm_line} | {listing.url[:80]}")


# ---------------------------------------------------------------------------
# Deal log CSV — unchanged
# ---------------------------------------------------------------------------

def append_deal_log(rows, log_path=DEAL_LOG_PATH):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "timestamp,platform,item_id,title,vrm,vrm_source,vrm_confidence,"
        "buy_price,expected_resale,base_repair,worst_repair,fees_total,"
        "expected_profit,max_bid,shock_ratio,velocity,decision,ulez,url\n"
    )
    write_header = not log_path.exists() or log_path.stat().st_size == 0

    with log_path.open("a", encoding="utf-8") as f:
        if write_header:
            f.write(header)
        ts = now_utc_iso()
        for listing, deal, out in rows:
            ulez = (
                "yes" if listing.ulez_compliant
                else ("no" if listing.ulez_compliant is False else "?")
            )
            safe_title = listing.title.replace(",", ";").replace("\n", " ")[:80]
            f.write(
                f"{ts},{listing.platform},{listing.item_id},{safe_title},"
                f"{listing.vrm},{listing.vrm_source},{listing.vrm_confidence:.2f},"
                f"{listing.price_gbp:.0f},{deal.expected_resale:.0f},"
                f"{deal.base_repair_estimate:.0f},{deal.worst_case_repair:.0f},{deal.fees_total:.0f},"
                f"{out.expected_profit:.0f},{out.max_bid:.0f},{out.shock_impact_ratio:.2f},"
                f"{out.velocity_score:.1f},{out.decision},{ulez},{listing.url}\n"
            )

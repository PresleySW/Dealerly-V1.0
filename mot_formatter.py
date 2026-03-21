"""
dealerly/mot_formatter.py
=========================
Renders a DVSA MOT history payload as compact HTML for embedding in reports.

Single public function: format_mot_history_html()

Depends on: nothing from Dealerly (stdlib only).
No I/O. No DB. Pure transformation.
"""
from __future__ import annotations

import html as html_lib
from typing import Any, Optional


def format_mot_history_html(
    payload: Optional[dict[str, Any]],
    vrm: str,
) -> str:
    """
    Render a DVSA MOT history payload as a collapsible HTML <details> block.

    Shows up to 8 most recent tests with pass/fail status, mileage,
    failures (red), and advisories (amber).

    Returns an empty string if payload is None or contains no tests.
    """
    if not payload:
        return ""

    make   = payload.get("make", "").title()
    model  = payload.get("model", "").title()
    colour = payload.get("primaryColour", "").title()
    fuel   = payload.get("fuelType", "").title()
    first  = payload.get("firstUsedDate", "").replace(".", "-")

    # Use explicit greys so text stays readable inside dark-themed report <details>
    # (inherited --text can be near-white and vanishes on pale MOT card backgrounds).
    header = (
        f"<div style='font-size:0.95em;color:#475569;margin-bottom:10px;font-weight:600'>"
        f"{make} {model} &nbsp;|&nbsp; {colour} &nbsp;|&nbsp; {fuel}"
        f" &nbsp;|&nbsp; First used: {first}</div>"
    )

    tests = payload.get("motTests") or []
    if not tests:
        # DVSA confirmed vehicle but no MOT records (new vehicle, exempt, or pre-2005)
        no_records_msg = (
            "<div style='font-size:0.92em;color:#64748b;padding:10px 12px;background:#f8fafc;"
            "border:1px solid #e2e8f0;border-radius:6px'>"
            "No MOT records found — vehicle may be exempt, too new (&lt;3 years),"
            " or registered before digital records began."
            "</div>"
        )
        return f"{header}{no_records_msg}"

    cards = []
    for test in tests[:8]:
        result   = str(test.get("testResult", "")).upper()
        date     = str(test.get("completedDate", ""))[:10]
        odo      = test.get("odometerValue", "?")
        odo_unit = test.get("odometerUnit", "mi")
        defects  = test.get("defects") or []

        advisories = [
            d for d in defects
            if str(d.get("type", "")).upper() in ("ADVISORY", "MONITOR")
        ]
        failures = [
            d for d in defects
            if str(d.get("type", "")).upper() in ("FAIL", "MAJOR", "DANGEROUS")
        ]

        # PASSED with no notes: subtle green tint.
        # PASSED with advisories: subtle amber tint.
        # FAILED: red tint.
        if result == "PASSED":
            bg = "#fefce8" if advisories else "#f0fdf4"
            status_col = "#166534"
        else:
            bg = "#fee2e2"
            status_col = "#991b1b"

        detail_html = ""
        if failures:
            items = "".join(
                f"<li style='color:#991b1b;margin-bottom:3px'>"
                f"{html_lib.escape(d.get('text', '')[:140])}</li>"
                for d in failures[:3]
            )
            detail_html += f"<ul style='margin:8px 0 0 18px;padding:0;font-size:0.9em;line-height:1.5'>{items}</ul>"
        if advisories:
            items = "".join(
                f"<li style='color:#92400e;margin-bottom:3px'>"
                f"{html_lib.escape(d.get('text', '')[:140])}</li>"
                for d in advisories[:4]
            )
            detail_html += f"<ul style='margin:8px 0 0 18px;padding:0;font-size:0.9em;line-height:1.5'>{items}</ul>"

        try:
            odo_fmt = f"{int(odo):,}"
        except (ValueError, TypeError):
            odo_fmt = str(odo)

        status_chip = (
            f"<span style='color:{status_col};font-weight:700;font-size:0.95em'>"
            f"{html_lib.escape(result.title())}</span>"
        )
        # Date + odometer: explicit dark colours so they never inherit light theme text on pale rows.
        date_col = "#7f1d1d" if result != "PASSED" else "#0f172a"
        odo_col = "#334155"
        notes = detail_html or "<span style='color:#475569'>No advisories or failures noted.</span>"
        cards.append(
            "<div style='padding:12px 14px;border:1px solid #e2e8f0;border-radius:8px;"
            f"background:{bg};margin-bottom:10px;color:#0f172a'>"
            "<div style='display:flex;align-items:center;justify-content:space-between;gap:10px;"
            "font-size:0.95em;margin-bottom:6px;flex-wrap:wrap'>"
            f"<strong style='color:{date_col}'>{html_lib.escape(date)}</strong>"
            f"{status_chip}"
            f"<span style='color:{odo_col};font-weight:600'>{html_lib.escape(odo_fmt)} "
            f"{html_lib.escape(str(odo_unit))}</span>"
            "</div>"
            f"<div style='line-height:1.5;color:#0f172a'>{notes}</div>"
            "</div>"
        )

    timeline = (
        "<div style='display:block;font-size:0.82em'>"
        f"{''.join(cards)}"
        "</div>"
    )

    return f"{header}{timeline}"

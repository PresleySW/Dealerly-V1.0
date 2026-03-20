"""
dealerly/cli.py — v0.9.4

v0.9.4 changes:
  - Quickstart margin uses default_target_margin(capital) instead of hardcoded 400
  - Admin/transport buffers use new lower defaults (£30/£40)
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from dealerly.config import (
    Config, DEFAULT_COMPS_TTL_HOURS, DEFAULT_EBAY_FEE_RATE,
    DEFAULT_NEAR_MISS_BAND, DEFAULT_PAGES, DEFAULT_PAYMENT_FEE_RATE,
    DEFAULT_PRICE_MAX, DEFAULT_PRICE_MIN, DEFAULT_RESALE_DISCOUNT,
    DEFAULT_MISPRICE_RATIO, DEFAULT_SHORTLIST_ENRICH_N,
    DEFAULT_BUYER_POSTCODE, DEFAULT_SEARCH_RADIUS_MILES,
    DEFAULT_ADMIN_BUFFER, DEFAULT_TRANSPORT_BUFFER, DEFAULT_TOP_N,
    VERSION,
    default_holding_cost, default_target_margin, QUERY_PRESETS, DB_PATH,
    DEAL_LOG_PATH, MODE_PROFILES, ModeProfile,
)
from dealerly.db import db_connect, init_db, watchlist_list, get_verified_vehicle, upsert_verified_vehicle
from dealerly.offers import claude_api_key, openai_api_key
from dealerly.utils import load_dotenv, prompt_choice, prompt_float, prompt_int

_ACTIVE_WEB_SETUP_SERVER: HTTPServer | None = None


def _shutdown_active_web_setup_server() -> None:
    global _ACTIVE_WEB_SETUP_SERVER
    if _ACTIVE_WEB_SETUP_SERVER is None:
        return
    try:
        _ACTIVE_WEB_SETUP_SERVER.shutdown()
    except Exception:
        pass
    _ACTIVE_WEB_SETUP_SERVER = None


def _antigravity_cli_path() -> Path:
    """Best-effort local Antigravity CLI path detection."""
    p_env = os.environ.get("ANTIGRAVITY_CLI_PATH", "").strip()
    if p_env:
        return Path(p_env)
    # Expected local install path used in this workspace.
    return Path(__file__).resolve().parents[2] / "Antigravity" / "bin" / "antigravity.cmd"


def _open_url_preferred(url: str) -> None:
    """Open URLs in Antigravity by default, fallback to browser."""
    # Always open local setup pages in the system browser.
    # Antigravity can route these into Agent/Auth flows instead of serving localhost.
    if url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return

    disable = os.environ.get("DEALERLY_DISABLE_ANTIGRAVITY", "").strip().lower()
    if disable not in {"1", "true", "yes"} and _antigravity_cli_path().exists():
        try:
            subprocess.Popen(
                [str(_antigravity_cli_path()), url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _load_env() -> None:
    candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
        Path(".env"),
    ]
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=True)
            print(f"[env] Loaded {p}")
            return
    print("[env] WARNING: .env not found — credentials must be set in environment")


def _parse_mode_arg() -> str:
    """Return the value of --mode <name> from sys.argv, or empty string."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--mode" and i + 1 < len(args):
            return args[i + 1].strip().lower()
        if a.startswith("--mode="):
            return a.split("=", 1)[1].strip().lower()
    return ""


def _parse_flag_value(flag: str) -> str:
    """Return the value after `flag <value>` or `flag=<value>` in sys.argv, or ''."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1].strip()
        if a.startswith(f"{flag}="):
            return a.split("=", 1)[1].strip()
    return ""


def _do_vrm_lookup(vrm: str) -> None:
    """
    Quick VRM lookup: DB first, then DVSA API fallback.
    Usage: python -m dealerly.cli --vrm-lookup AB12CDE
    """
    from dealerly.mot import build_mot_provider

    vrm = vrm.upper().replace(" ", "")
    print(f"\n[VRM Lookup] {vrm}")

    init_db(DB_PATH)
    conn = db_connect(DB_PATH)

    # 1. Check local DB
    payload = get_verified_vehicle(conn, vrm, max_age_days=30)
    source = "local DB (≤30d)"
    if payload is None:
        # 2. Fetch live from DVSA
        provider = build_mot_provider("2")
        if provider is None:
            print("  DVSA provider not available — check DVSA credentials in .env")
            conn.close()
            return
        print(f"  Not in local DB — fetching from DVSA...")
        payload = provider.fetch(vrm)
        if payload is not None:
            upsert_verified_vehicle(conn, vrm, payload)
            source = f"DVSA live ({provider.provider_name})"
        else:
            print(f"  Not found in DVSA. VRM may be invalid or pre-digital records.")
            conn.close()
            return

    conn.close()
    make   = payload.get("make", "?").title()
    model  = payload.get("model", "?").title()
    colour = payload.get("primaryColour", "?").title()
    fuel   = payload.get("fuelType", "?").title()
    first  = payload.get("firstUsedDate", "?").replace(".", "-")
    tests  = payload.get("motTests") or []
    last_result = tests[0].get("testResult", "?") if tests else "no records"
    last_date   = str(tests[0].get("completedDate", ""))[:10] if tests else "-"
    last_miles  = tests[0].get("odometerValue", "?") if tests else "-"

    print(f"\n  Source : {source}")
    print(f"  Vehicle: {make} {model}  |  {colour}  |  {fuel}")
    print(f"  First used: {first}")
    print(f"  MOT tests: {len(tests)}")
    if tests:
        print(f"  Last test : {last_date}  {last_result.upper()}  @ {last_miles} mi")
        advisories = [
            d for t in tests[:1]
            for d in (t.get("defects") or [])
            if str(d.get("type", "")).upper() in ("ADVISORY", "MONITOR")
        ]
        failures = [
            d for t in tests[:1]
            for d in (t.get("defects") or [])
            if str(d.get("type", "")).upper() in ("FAIL", "MAJOR", "DANGEROUS")
        ]
        if failures:
            print(f"  Failures ({len(failures)}):")
            for d in failures[:3]:
                print(f"    ✗ {d.get('text', '')[:80]}")
        if advisories:
            print(f"  Advisories ({len(advisories)}):")
            for d in advisories[:4]:
                print(f"    ⚠ {d.get('text', '')[:80]}")
    print()


def _apply_profile(cfg: Config, profile: ModeProfile) -> Config:
    """
    Override Config fields with values from a ModeProfile.
    Only affects fields the profile explicitly governs.
    """
    from dataclasses import replace as dc_replace
    cap = cfg.capital if cfg.capital != 3000.0 else profile.capital_default
    margin = max(150.0, cap * profile.target_margin_pct)
    return dc_replace(
        cfg,
        capital=cap,
        price_min=profile.price_min,
        price_max=profile.price_max,
        target_margin=margin,
        enrich_n=profile.enrich_n,
    )


def _web_setup_config() -> Config | None:
    """
    Local browser-based setup form.

    Opens a temporary localhost page, collects selections, then returns Config.
    Returns None if cancelled/timeout.
    """
    global _ACTIVE_WEB_SETUP_SERVER
    _shutdown_active_web_setup_server()
    result: dict = {}
    done = threading.Event()

    defaults = {
        "capital": 3000,
        "price_min": DEFAULT_PRICE_MIN,
        "price_max": DEFAULT_PRICE_MAX,
        "lock_price_to_capital": "1",
        "target_margin": int(default_target_margin(3000)),
        "target_margin_scale": "1.00",
        "pages": str(DEFAULT_PAGES),
        "preset": "10",
        "input_mode": "all",
        "enrich_n": str(DEFAULT_SHORTLIST_ENRICH_N),
        "buyer_postcode": DEFAULT_BUYER_POSTCODE,
        "search_radius_miles": str(DEFAULT_SEARCH_RADIUS_MILES),
        "fast_mode": "1",
        "use_autotrader": "0",
    }

    loading_target = (
        Path(__file__).resolve().parent.parent / "reports" / "dealerly_loading.html"
    ).resolve()
    loading_state_target = (
        Path(__file__).resolve().parent.parent / "reports" / "dealerly_loading_state.json"
    ).resolve()

    class _Handler(BaseHTTPRequestHandler):
        def _send_html(self, body: str, status: int = 200) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/loading-state"):
                try:
                    payload = loading_state_target.read_text(encoding="utf-8")
                except Exception:
                    payload = '{"pct":2,"stage":"Initializing Dealerly","done":false,"report_url":""}'
                body = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/open-report"):
                try:
                    qs = parse_qs(urlparse(self.path).query or "")
                    target = (qs.get("u", [""])[0] or "").strip()
                    if not target:
                        st = loading_state_target.read_text(encoding="utf-8")
                        target = str((json.loads(st) or {}).get("report_url", "") or "")
                    local_path = ""
                    if target.startswith("file://"):
                        parsed = urlparse(target)
                        local_path = unquote(parsed.path or "")
                        # Windows file URI path can begin with /D:/...
                        if len(local_path) >= 3 and local_path[0] == "/" and local_path[2] == ":":
                            local_path = local_path[1:]
                    else:
                        local_path = target
                    p = Path(local_path).resolve()
                    if not p.exists():
                        raise FileNotFoundError(str(p))
                    self._send_html(p.read_text(encoding="utf-8"))
                except Exception as exc:
                    self._send_html(
                        "<!doctype html><html><head><meta charset='utf-8'></head><body>"
                        "<h2>Report not ready</h2>"
                        "<p>Could not open report from loading state. "
                        "Please open the latest file in reports/ manually.</p>"
                        f"<pre>{str(exc)[:240]}</pre></body></html>",
                        500,
                    )
                return
            if self.path == "/loading":
                try:
                    body = loading_target.read_text(encoding="utf-8")
                    if not body.strip():
                        raise ValueError("loading HTML empty")
                    self._send_html(body)
                except Exception:
                    self._send_html(
                        "<!doctype html><html><head><meta charset='utf-8'>"
                        "<meta http-equiv='refresh' content='0.7'>"
                        "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;"
                        "background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif}"
                        ".shell{width:min(620px,92vw);padding:24px;border-radius:14px;border:1px solid #30363d;"
                        "background:#161b22} .bar{height:10px;border-radius:999px;overflow:hidden;background:#0b1324;"
                        "border:1px solid #1e293b;margin-top:10px} .fill{width:18%;height:100%;background:linear-gradient(90deg,#58a6ff,#22c55e,#58a6ff);"
                        "background-size:220% 100%;animation:flow 1.3s linear infinite}@keyframes flow{from{background-position:0% 50%}to{background-position:200% 50%}}</style>"
                        "</head><body><div class='shell'><div>Starting Dealerly...</div><div class='bar'><div class='fill'></div></div></div></body></html>"
                    )
                return
            if self.path != "/":
                self._send_html("<h1>Not Found</h1>", 404)
                return
            preset_opts = "".join(
                f'<option value="{k}" {"selected" if k == defaults["preset"] else ""}>{k}: {v["desc"]}</option>'
                for k, v in QUERY_PRESETS.items()
            )
            html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dealerly Setup</title>
<style>
body{{font-family:'Segoe UI',sans-serif;background:#0b1220;color:#e2e8f0;margin:0;padding:22px}}
.wrap{{max-width:900px;margin:0 auto;background:#111827;border:1px solid #1f2937;border-radius:14px;padding:22px}}
h1{{margin:0 0 10px 0}} .sub{{color:#94a3b8;margin-bottom:18px}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(220px,1fr));gap:12px}}
label{{display:block;font-size:.85rem;color:#93c5fd;margin-bottom:4px}}
input,select{{width:100%;padding:9px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0}}
.readout{{width:100%;padding:9px 11px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-weight:700}}
.toggles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;margin-top:14px}}
.toggle{{display:flex;gap:10px;align-items:center;background:#0f172a;border:1px solid #334155;border-radius:10px;padding:10px 12px}}
.toggle input{{width:18px;height:18px;accent-color:#22c55e;cursor:pointer}}
.toggle label{{margin:0;color:#cbd5e1;font-size:.88rem;cursor:pointer}}
.help{{font-size:.79rem;color:#93a3b8;margin-top:8px}}
.actions{{margin-top:16px;display:flex;justify-content:flex-end}}
button{{background:#22c55e;color:#052e16;border:0;border-radius:10px;padding:10px 14px;font-weight:700;cursor:pointer}}
</style></head>
<body><div class="wrap">
<h1>Dealerly Run Setup</h1>
<div class="sub">Select core run options. Dealerly will auto-tune margins from your capital.</div>
<form method="post" action="/start">
<div class="grid">
  <div><label>Capital (£)</label><input name="capital" type="number" min="500" step="100" value="{defaults["capital"]}"></div>
  <div><label>Target Margin (£, auto-scaled)</label><div class="readout" id="target-margin-readout">{defaults["target_margin"]}</div><input type="hidden" id="target-margin-hidden" name="target_margin" value="{defaults["target_margin"]}"></div>
  <div><label>Profit Scale</label><div class="readout" id="margin-scale-readout">{defaults["target_margin_scale"]}x</div><input type="range" id="margin-scale-slider" min="0.60" max="1.60" step="0.05" value="{defaults["target_margin_scale"]}"><input type="hidden" id="margin-scale-hidden" name="target_margin_scale" value="{defaults["target_margin_scale"]}"></div>
  <div><label>Price Min (£)</label><input name="price_min" type="number" min="100" step="50" value="{defaults["price_min"]}"></div>
  <div><label>Price Max (£)</label><input name="price_max" type="number" min="500" step="50" value="{defaults["price_max"]}"></div>
  <div><label>Buyer Postcode</label><input name="buyer_postcode" type="text" maxlength="10" value="{defaults["buyer_postcode"]}"></div>
  <div><label>Search Radius (miles)</label><div class="readout" id="radius-readout">{defaults["search_radius_miles"]}</div><input type="range" id="radius-slider" min="10" max="200" step="5" value="{defaults["search_radius_miles"]}"><input type="hidden" id="radius-hidden" name="search_radius_miles" value="{defaults["search_radius_miles"]}"></div>
  <div><label>Preset</label><select name="preset">{preset_opts}</select></div>
  <div><label>Input Mode</label><select name="input_mode">
    <option value="all">All platforms</option><option value="ebay">eBay only</option>
    <option value="motors">Motors only</option><option value="facebook">Facebook Marketplace</option></select></div>
  <div><label>Pages per term</label><div class="readout" id="pages-readout">{defaults["pages"]}</div><input type="range" id="pages-slider" min="1" max="8" step="1" value="{defaults["pages"]}"><input type="hidden" id="pages-hidden" name="pages" value="{defaults["pages"]}"></div>
  <div><label>Enrich N</label><div class="readout" id="enrich-readout">{defaults["enrich_n"]}</div><input type="range" id="enrich-slider" min="5" max="120" step="1" value="{defaults["enrich_n"]}"><input type="hidden" id="enrich-hidden" name="enrich_n" value="{defaults["enrich_n"]}"></div>
</div>
<div class="toggles">
  <div class="toggle"><input type="checkbox" id="fast-mode" name="fast_mode" value="1" checked><label for="fast-mode">Fast mode (uses tighter time budgets, but keeps your selected sliders)</label></div>
  <div class="toggle"><input type="checkbox" id="use-at" name="use_autotrader" value="1"><label for="use-at">Use AutoTrader comps (higher confidence, slower)</label></div>
  <div class="toggle"><input type="checkbox" id="lock-price" name="lock_price_to_capital" value="1" checked><label for="lock-price">Lock max price to capital (default)</label></div>
</div>
<div class="help">Tip: AutoTrader improves confidence but increases runtime. Fast mode no longer force-clamps pages/enrichment.</div>
<div class="actions"><button type="submit">Start Dealerly Run</button></div>
</form></div>
<script>
(() => {{
  const cap = document.querySelector('input[name="capital"]');
  const pmax = document.querySelector('input[name="price_max"]');
  const lock = document.getElementById('lock-price');
  const marginReadout = document.getElementById('target-margin-readout');
  const marginHidden = document.getElementById('target-margin-hidden');
  const marginScaleSlider = document.getElementById('margin-scale-slider');
  const marginScaleReadout = document.getElementById('margin-scale-readout');
  const marginScaleHidden = document.getElementById('margin-scale-hidden');
  const fastMode = document.getElementById('fast-mode');
  const useAT = document.getElementById('use-at');
  const radiusSlider = document.getElementById('radius-slider');
  const radiusReadout = document.getElementById('radius-readout');
  const radiusHidden = document.getElementById('radius-hidden');
  const pagesSlider = document.getElementById('pages-slider');
  const pagesReadout = document.getElementById('pages-readout');
  const pagesHidden = document.getElementById('pages-hidden');
  const enrichSlider = document.getElementById('enrich-slider');
  const enrichReadout = document.getElementById('enrich-readout');
  const enrichHidden = document.getElementById('enrich-hidden');

  const calcMargin = (capital) => {{
    return Math.round(Math.max(150, Math.min(600, capital < 5000 ? capital * 0.08 : (capital < 10000 ? capital * 0.10 : capital * 0.12))));
  }};

  const sync = () => {{
    if (!cap || !pmax || !lock) return;
    const capVal = parseInt(cap.value || "0", 10);
    if (lock.checked && Number.isFinite(capVal) && capVal > 0) {{
      pmax.value = String(capVal);
      pmax.setAttribute('disabled', 'disabled');
    }} else {{
      pmax.removeAttribute('disabled');
    }}
    const marginScale = marginScaleSlider ? parseFloat(marginScaleSlider.value || "1") : 1;
    const baseMargin = calcMargin(Number.isFinite(capVal) ? capVal : 0);
    const margin = Math.max(100, Math.round(baseMargin * (Number.isFinite(marginScale) ? marginScale : 1)));
    if (marginReadout) marginReadout.textContent = String(margin);
    if (marginHidden) marginHidden.value = String(margin);
    if (marginScaleSlider && marginScaleReadout && marginScaleHidden) {{
      const sc = parseFloat(marginScaleSlider.value || "1");
      marginScaleReadout.textContent = `${{(Number.isFinite(sc) ? sc : 1).toFixed(2)}}x`;
      marginScaleHidden.value = String(Number.isFinite(sc) ? sc : 1);
    }}
    // Fast mode no longer hard-clamps user-selected sliders.
    if (radiusSlider && radiusReadout && radiusHidden) {{
      radiusReadout.textContent = String(radiusSlider.value);
      radiusHidden.value = String(radiusSlider.value);
    }}
    if (pagesSlider && pagesReadout && pagesHidden) {{
      pagesReadout.textContent = String(pagesSlider.value);
      pagesHidden.value = String(pagesSlider.value);
    }}
    if (enrichSlider && enrichReadout && enrichHidden) {{
      enrichReadout.textContent = String(enrichSlider.value);
      enrichHidden.value = String(enrichSlider.value);
    }}
  }};

  if (cap) cap.addEventListener('input', sync);
  if (pmax) pmax.addEventListener('input', sync);
  if (lock) lock.addEventListener('change', sync);
  if (fastMode) fastMode.addEventListener('change', sync);
  if (useAT) useAT.addEventListener('change', sync);
  if (marginScaleSlider) marginScaleSlider.addEventListener('input', sync);
  if (radiusSlider) radiusSlider.addEventListener('input', sync);
  if (pagesSlider) pagesSlider.addEventListener('input', sync);
  if (enrichSlider) enrichSlider.addEventListener('input', sync);
  sync();
}})();
</script>
</body></html>"""
            self._send_html(html)

        def do_POST(self):  # noqa: N802
            if self.path != "/start":
                self._send_html("<h1>Not Found</h1>", 404)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length).decode("utf-8", errors="ignore")
            parsed = {k: (v[0] if v else "") for k, v in parse_qs(body).items()}
            result.update(parsed)
            # Precreate loading screen and mark as already opened in browser so
            # pipeline doesn't spawn a second window for the same page.
            os.environ["DEALERLY_LOADING_ALREADY_OPEN"] = "1"
            try:
                from dealerly.report import write_loading_screen
                write_loading_screen(progress_pct=2, stage_text="Initializing Dealerly")
            except Exception:
                pass
            done.set()
            self._send_html(
                "<!doctype html><html><head>"
                "<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;"
                "color:#e6edf3;font-family:'Segoe UI',sans-serif}.shell{width:min(620px,92vw);padding:24px;"
                "border-radius:14px;border:1px solid #30363d;background:#161b22}.bar{height:10px;border-radius:999px;"
                "overflow:hidden;background:#0b1324;border:1px solid #1e293b;margin-top:10px}"
                ".fill{width:32%;height:100%;background:linear-gradient(90deg,#58a6ff,#22c55e,#58a6ff);background-size:220% 100%;"
                "animation:flow 1.3s linear infinite}@keyframes flow{from{background-position:0% 50%}to{background-position:200% 50%}}</style>"
                f"</head><body><div class='shell'><div>Launching Dealerly...</div><div class='bar'><div class='fill'></div></div></div>"
                f"<script>setTimeout(function(){{window.location.replace('/loading');}},120);</script></body></html>"
            )

        def log_message(self, format, *args):  # noqa: A003
            return

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    _ACTIVE_WEB_SETUP_SERVER = httpd
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"
    print(f"\n[Web Setup] Opened at {url}")
    _open_url_preferred(url)
    if not done.wait(timeout=600):
        _shutdown_active_web_setup_server()
        print("[Web Setup] Timed out or cancelled.")
        return None

    def _f(name: str, default: float) -> float:
        try:
            return float(result.get(name, default))
        except Exception:
            return float(default)

    def _i(name: str, default: int) -> int:
        try:
            return int(float(result.get(name, default)))
        except Exception:
            return int(default)

    capital = _f("capital", 3000.0)
    if result.get("lock_price_to_capital", "1") == "1":
        price_max_val = max(500, int(capital))
    else:
        price_max_val = _i("price_max", DEFAULT_PRICE_MAX)
    target_margin_default = default_target_margin(capital)
    ai_back = "openai" if openai_api_key() else ("claude" if claude_api_key() else "none")
    input_mode = result.get("input_mode", "all")
    fast_mode = result.get("fast_mode", "") == "1"
    use_autotrader = result.get("use_autotrader", "") == "1"
    os.environ["DEALERLY_FAST_MODE"] = "1" if fast_mode else "0"
    target_margin_scale = max(0.6, min(1.6, _f("target_margin_scale", 1.0)))
    target_margin_val = _f("target_margin", target_margin_default * target_margin_scale)
    if target_margin_val <= 0:
        target_margin_val = target_margin_default * target_margin_scale
    return Config(
        capital=capital,
        price_min=_i("price_min", DEFAULT_PRICE_MIN),
        price_max=price_max_val,
        target_margin=target_margin_val,
        holding_cost=default_holding_cost(capital),
        ebay_fee_rate=DEFAULT_EBAY_FEE_RATE,
        pay_fee_rate=DEFAULT_PAYMENT_FEE_RATE,
        admin_buffer=DEFAULT_ADMIN_BUFFER,
        transport_buffer=DEFAULT_TRANSPORT_BUFFER,
        mot_mode="2",
        category_ids="9801",
        pages=max(1, min(8, _i("pages", DEFAULT_PAGES))),
        near_miss_band=DEFAULT_NEAR_MISS_BAND,
        auction_only=False,
        store_comps=True,
        comps_ttl=DEFAULT_COMPS_TTL_HOURS,
        resale_discount=DEFAULT_RESALE_DISCOUNT,
        preset=result.get("preset", "10"),
        enrich_mode="1",
        enrich_n=max(5, min(120, _i("enrich_n", DEFAULT_SHORTLIST_ENRICH_N))),
        sort="endingSoonest",
        misprice_ratio=DEFAULT_MISPRICE_RATIO,
        require_comps=False,
        open_html_report=True,
        ai_backend=ai_back if ai_back in ("openai", "claude", "none") else "openai",
        use_autotrader=use_autotrader,
        generate_offer_msgs=(ai_back != "none"),
        input_mode=input_mode if input_mode in ("all", "ebay", "motors", "facebook") else "all",
        buyer_postcode=(result.get("buyer_postcode", DEFAULT_BUYER_POSTCODE) or DEFAULT_BUYER_POSTCODE).strip().upper(),
        search_radius_miles=max(10, min(200, _i("search_radius_miles", DEFAULT_SEARCH_RADIUS_MILES))),
    )


def main() -> None:
    _load_env()

    _ebay_ok   = bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))
    _claude_ok = bool(os.environ.get("CLAUDE_API_KEY", "").strip().startswith("sk-ant-"))
    _dvsa_ok   = bool(os.environ.get("DVSA_MOT_CLIENT_ID"))
    _vision_ok = bool(os.environ.get("PLATE_RECOGNIZER_TOKEN", "").strip())
    _anti_ok   = _antigravity_cli_path().exists()

    mode_name = _parse_mode_arg()
    profile: ModeProfile | None = MODE_PROFILES.get(mode_name)
    if profile:
        print(f"\n[Mode] {profile.name.upper()} — {profile.description}")
    elif mode_name:
        print(f"\n[Mode] Unknown mode '{mode_name}'. Valid: {', '.join(MODE_PROFILES)}. Using defaults.")

    print(f"\n[Dealerly {VERSION}] Credentials: "
          f"eBay={'OK' if _ebay_ok else 'MISSING'}  "
          f"Claude={'OK (offer msgs)' if _claude_ok else 'NOT SET'}  "
          f"DVSA={'OK' if _dvsa_ok else 'NOT SET'}  "
          f"Vision={'OK (OCR)' if _vision_ok else 'NOT SET'}  "
          f"Antigravity={'OK' if _anti_ok else 'NOT SET'}")
    if not _vision_ok:
        print("  -> Add PLATE_RECOGNIZER_TOKEN to .env for photo-based VRM extraction."
              "\n     Free at https://platerecognizer.com/ (2,500 lookups/month)\n")

    debug_mode = "--debug" in sys.argv or "-d" in sys.argv
    if debug_mode:
        print("  [debug] Debug mode ON — JSON log will be written to reports/")

    if "--watchlist" in sys.argv or "-w" in sys.argv:
        init_db(DB_PATH)
        conn = db_connect(DB_PATH)
        show_watchlist(conn)
        conn.close()
        return

    # --vrm-lookup <VRM>: quick VRM check without running the full pipeline
    _vrm_lookup = _parse_flag_value("--vrm-lookup")
    if _vrm_lookup:
        _do_vrm_lookup(_vrm_lookup)
        return

    use_web = input("Use web setup UI? (Y/n) [default Y]: ").strip().lower() != "n"
    if use_web:
        cfg = _web_setup_config()
        if cfg is None:
            print("  Falling back to Quickstart.")
            cfg = quickstart_config()
    else:
        use_quick = input("Quickstart mode? (Y/n) [default Y]: ").strip().lower() != "n"
        cfg = quickstart_config() if use_quick else _full_config_prompt()

    if profile:
        cfg = _apply_profile(cfg, profile)
        print(f"  Profile applied: price £{cfg.price_min}–£{cfg.price_max}, "
              f"margin £{cfg.target_margin:.0f}, enrich_n={cfg.enrich_n}")

    from dataclasses import replace as dc_replace
    cfg = dc_replace(cfg, debug_mode=debug_mode)

    from dealerly.pipeline import run
    try:
        run(cfg)
    finally:
        _shutdown_active_web_setup_server()


def show_watchlist(conn) -> None:
    items = watchlist_list(conn)
    if not items:
        print("\nWatchlist is empty.")
        return
    print(f"\n=== WATCHLIST ({len(items)} items watching) ===")
    for w in items:
        print(f"\n  [{w['id']}] {w['title'][:70]}")
        print(f"       Buy: {w['buy_price']:.0f} | Max bid: {w['max_bid']:.0f}"
              f" | Profit: {w['expected_profit']:.0f} | {w['decision']}")
        print(f"       VRM: {w['vrm'] or '-'} | {w['url']}")


def quickstart_config() -> Config:
    print("\n" + "=" * 60)
    print(f"  DEALERLY {VERSION} -- SWAutos Flip Tool")
    print("=" * 60)
    print("\nQuickstart: Enter for defaults, or comma-separated values:")
    print("  capital, price_min, price_max, margin, holding, auction(0/1), sort, preset")
    s = input("\n> ").strip()

    cap     = 3000.0
    pmin    = DEFAULT_PRICE_MIN
    pmax    = DEFAULT_PRICE_MAX
    # v0.9.4: use tiered default instead of hardcoded 400
    margin  = default_target_margin(cap)
    holding = default_holding_cost(cap)

    # v0.9.6: auto-calibrate margin from deal log if enough data exists
    try:
        from dealerly.calibration import calibrate
        cal = calibrate(path=DEAL_LOG_PATH, capital=cap)
        if cal.rows_analysed < 30:
            raise ValueError("not enough rows")
        rec = cal.get_recommendation("target_margin")
        if rec and rec.confidence in ("high", "medium"):
            margin = rec.suggested_value
            print(f"\n  [calibration] {cal.rows_analysed} log rows → "
                  f"suggested margin £{rec.suggested_value:.0f} "
                  f"({rec.confidence}, {rec.reason[:60]})")
        else:
            b = cal.buckets.get("BUY")
            if b and b.count >= 10:
                print(f"\n  [calibration] {cal.rows_analysed} rows — "
                      f"margin £{margin:.0f} looks well-calibrated "
                      f"(BUY avg profit £{b.avg_profit:.0f})")
    except (ValueError, FileNotFoundError, TypeError):
        pass  # not enough data, no log file, or older calibration.py
    auction_only = False
    sort    = "endingSoonest"
    preset  = "8"

    if s:
        parts = [x.strip() for x in s.split(",")]
        try:
            if len(parts) > 0 and parts[0]: cap          = float(parts[0])
            if len(parts) > 1 and parts[1]: pmin         = int(parts[1])
            if len(parts) > 2 and parts[2]: pmax         = int(parts[2])
            if len(parts) > 3 and parts[3]: margin       = float(parts[3])
            else:                            margin       = default_target_margin(cap)
            if len(parts) > 4 and parts[4]: holding      = float(parts[4])
            else:                            holding      = default_holding_cost(cap)
            if len(parts) > 5 and parts[5]: auction_only = bool(int(parts[5]))
            if len(parts) > 6 and parts[6]: sort         = parts[6]
            if len(parts) > 7 and parts[7]: preset       = parts[7]
        except Exception:
            pass

    # Prefer OpenAI by default to reduce Claude usage.
    ai = "openai" if openai_api_key() else ("claude" if claude_api_key() else "none")

    input_mode = "all"

    print(f"\n  Config: capital=£{cap:.0f} margin=£{margin:.0f} "
          f"admin=£{DEFAULT_ADMIN_BUFFER:.0f} transport=£{DEFAULT_TRANSPORT_BUFFER:.0f} "
          f"preset={preset}")

    return Config(
        capital=float(cap), price_min=pmin, price_max=pmax,
        target_margin=float(margin), holding_cost=float(holding),
        ebay_fee_rate=DEFAULT_EBAY_FEE_RATE, pay_fee_rate=DEFAULT_PAYMENT_FEE_RATE,
        admin_buffer=DEFAULT_ADMIN_BUFFER, transport_buffer=DEFAULT_TRANSPORT_BUFFER,
        mot_mode="2", category_ids="9801",
        pages=4,
        near_miss_band=DEFAULT_NEAR_MISS_BAND,
        auction_only=auction_only, store_comps=True,
        comps_ttl=DEFAULT_COMPS_TTL_HOURS, resale_discount=DEFAULT_RESALE_DISCOUNT,
        preset=preset, enrich_mode="1", enrich_n=DEFAULT_SHORTLIST_ENRICH_N,
        sort=sort if sort in ("newlyListed", "endingSoonest") else "endingSoonest",
        misprice_ratio=DEFAULT_MISPRICE_RATIO, require_comps=False,
        open_html_report=True, ai_backend=ai,
        use_autotrader=True, generate_offer_msgs=(ai != "none"),
        input_mode=input_mode,
    )


def _full_config_prompt() -> Config:
    print("\nFull Config (app-style)")
    print("Core only: capital, price band, margin, transport, platform mode.")
    print("Advanced economics stay on proven defaults unless explicitly changed.\n")

    profile_raw = prompt_choice(
        "Operating profile",
        {"1": "flipper (lean, tighter shortlist)", "2": "dealer (broader, batch sourcing)", "3": "custom"},
        "2",
    )
    chosen_profile: ModeProfile | None = {
        "1": MODE_PROFILES.get("flipper"),
        "2": MODE_PROFILES.get("dealer"),
        "3": None,
    }.get(profile_raw)

    cap_default = chosen_profile.capital_default if chosen_profile else 2500.0
    capital = prompt_float("Capital", cap_default)
    price_min_default = chosen_profile.price_min if chosen_profile else DEFAULT_PRICE_MIN
    price_max_default = chosen_profile.price_max if chosen_profile else DEFAULT_PRICE_MAX
    price_min = prompt_int("Price min", price_min_default)
    price_max = prompt_int("Price max", price_max_default)

    target_margin_default = max(default_target_margin(capital), min(1200.0, price_max * 0.20))
    target_margin = prompt_float("Target margin", target_margin_default)
    transport_buffer = prompt_float("Transport buffer", DEFAULT_TRANSPORT_BUFFER)
    holding_cost = default_holding_cost(capital)
    ebay_fee_rate = DEFAULT_EBAY_FEE_RATE
    pay_fee_rate = DEFAULT_PAYMENT_FEE_RATE
    admin_buffer = DEFAULT_ADMIN_BUFFER
    mot_mode         = prompt_choice("MOT mode", {"0": "off", "1": "mock-json", "2": "DVSA"}, "2")
    category_ids     = input("eBay categoryIds [default 9801]: ").strip() or "9801"
    pages            = prompt_int("Pages per term", DEFAULT_PAGES)
    near_miss_band   = prompt_float("Near-miss band", DEFAULT_NEAR_MISS_BAND)
    auction_only     = (prompt_choice("Search mode", {"0": "all", "1": "AUCTION only"}, "0") == "1")
    store_comps      = (prompt_choice("Store comps", {"0": "off", "1": "on"}, "1") == "1")
    comps_ttl        = DEFAULT_COMPS_TTL_HOURS
    resale_discount  = DEFAULT_RESALE_DISCOUNT
    misprice_ratio   = DEFAULT_MISPRICE_RATIO
    require_comps    = (prompt_choice("Require comps", {"0": "no", "1": "yes"}, "0") == "1")
    use_at           = (prompt_choice("AutoTrader comps", {"0": "off", "1": "on"}, "1") == "1")
    at_postcode      = input("AutoTrader postcode [TW200AY]: ").strip() or "TW200AY"
    ai_back_raw      = prompt_choice("AI backend", {"0": "none", "1": "claude", "2": "openai"}, "2")
    ai_back          = {"0": "none", "1": "claude", "2": "openai"}.get(ai_back_raw, "none")
    gen_msgs         = (prompt_choice("Generate offer messages", {"0": "no", "1": "yes"}, "1") == "1")
    input_mode_raw   = prompt_choice(
        "Input mode",
        {
            "1": "All platforms (recommended)",
            "2": "eBay only",
            "3": "Motors only",
            "4": "Facebook Marketplace",
        },
        "1",
    )
    input_mode = {
        "1": "all",
        "2": "ebay",
        "3": "motors",
        "4": "facebook",
    }.get(input_mode_raw, "all")

    print("Presets:")
    for k, v in QUERY_PRESETS.items():
        print(f"  ({k}) {v['desc']}")
    preset      = input("Preset [6]: ").strip() or "6"
    enrich_mode = prompt_choice("Enrich mode", {"0": "off", "1": "shortlist", "2": "all"}, "1")
    enrich_default = chosen_profile.enrich_n if chosen_profile else DEFAULT_SHORTLIST_ENRICH_N
    enrich_n    = prompt_int("Enrich N", enrich_default)

    return Config(
        capital=capital, price_min=price_min, price_max=price_max,
        target_margin=target_margin, holding_cost=holding_cost,
        ebay_fee_rate=ebay_fee_rate, pay_fee_rate=pay_fee_rate,
        admin_buffer=admin_buffer, transport_buffer=transport_buffer,
        mot_mode=mot_mode, category_ids=category_ids, pages=pages,
        near_miss_band=near_miss_band, auction_only=auction_only,
        store_comps=store_comps, comps_ttl=comps_ttl, resale_discount=resale_discount,
        preset=preset, enrich_mode=enrich_mode, enrich_n=enrich_n,
        sort="endingSoonest", misprice_ratio=misprice_ratio, require_comps=require_comps,
        ai_backend=ai_back, use_autotrader=use_at,
        autotrader_postcode=at_postcode, generate_offer_msgs=gen_msgs,
        input_mode=input_mode,
    )


if __name__ == "__main__":
    main()

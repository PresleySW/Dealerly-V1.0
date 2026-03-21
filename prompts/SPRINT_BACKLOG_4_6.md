# Dealerly 1.0 — Sprint 4–6 backlog (Opus → Sonnet)

**Purpose:** Refined sprint definitions for Sprints 4–6 based on actual S1–3 deliverables + latest run (2026-03-20 20:39) + `NEXT_VERSION.md` directives.
**Read with:** `SPRINT_RUN.md`, `prompts/RELEVANT.md`, `prompts/PIPELINE_REPORT.md`, `CLAUDE.md`, `prompts/NEXT_VERSION.md`.

---

## Ground truth (do not re-plan)

| Sprint | Status | What shipped (summary) |
|--------|--------|-------------------------|
| **1** | Done | `pipeline.py` profit-order + `DEALERLY_PRIORITY_ENRICH_N`; `scoring.py` MOT confidence-tier BUY exception; `db.py` `completed_trades`; **`trades.py`** + `cli.py` **`--log-trade` / `--trades`** |
| **2** | Done | `--import-trades`, `--seed-demo-trades`; `calibration.py` from trades; Obsidian `Trades/` notes |
| **3** | Done | `pipeline.py` ANPR skip when `vrm_confidence ≥ 0.92`; `vrm.py` expanded UK label patterns + windows; `scoring.py` deal-specific **reason** strings on cards |

**Latest run (post S1–3):** 1 BUY, 9 OFFER, 11 PASS, 16 AVOID | Phase1 244s Phase2 96s Phase3 41s Phase4 33s | VRMs 19/40 | ANPR 3 calls, 14 budget-skipped | DVLA 3+0, 3 top-slice skipped | AT comps: P2 56, P4 40

**Already shipped (do not rebuild):**
- AutoTrader comp persistence (`autotrader_comps` table, 6h TTL, 3-tier cache in `fetch_for_key`)
- Priority enrichment (profit-ordered Phase 3)
- MOT confidence-tier BUY exception
- Trade CLI + calibration + Obsidian trades
- ANPR high-conf skip (vrm_confidence ≥ 0.92)
- VRM label expansion ("my reg", "private reg/plate", etc.)
- Deal-specific reason strings on cards

**Guardrails unchanged:** SQLite WAL; never edit `dealerly_log.csv`; preserve Facebook unauthenticated fallback + cookie hygiene; `CLAUDE.md` + `.cursorrules` win.

---

## Sprint 4 — NEXT_VERSION directives

**Theme:** Execute `NEXT_VERSION.md` directives — DVLA reach, FB fast mode, VRM badge, BUY count push.

**Scope correction:** Phase 2 is already 96s (down from 149s); comp caching already works. Sprint 4 targets the `NEXT_VERSION.md` directives instead of redundant Phase 2 work.

- **Goal:** (1) Loosen DVLA top-slice +4 for listings with prelim profit ≥ £200; (2) add `--no-facebook` CLI flag; (3) "No VRM — contact seller" badge on BUY/OFFER cards; (4) log AutoTrader cache hit/miss count in Phase 2 output. Push BUY count toward ≥3.
- **Primary files:** `pipeline.py` (~L1179/1284 DVLA gate), `cli.py` (platform flag), `report.py` (~L142/1321 VRM display), `autotrader.py` (cache hit counter), `config.py`.
- **Risks:** DVLA loosening adds ~4 API calls max per run — acceptable. No risk gate changes.
- **DoD:** DVLA admits +4 high-profit rows; `--no-facebook` skips FB cleanly; BUY/OFFER cards without VRM show actionable badge; Phase 2 logs cache hit/miss; `compileall` clean.

---

## Sprint 5 — Facebook measurement + gather economics

**Theme:** Measure FB quality before rewriting. Add counters, early-exit, configurable cap.

- **Goal:** (a) Phase 1 logs `fb_total`, `fb_titles_good`, `fb_mileage_found`, `fb_thumb_found`; (b) early-exit when <5 new listings in last 2 scroll cycles; (c) `DEALERLY_FB_MAX_LISTINGS` cap (default 400); (d) Phase 1 ≤ 200s with cap active.
- **Primary files:** `facebook.py` (scroll loop + card parser), `pipeline.py` Phase 1, `config.py`.
- **Risks:** Facebook DOM drift — do **not** break unauthenticated fallback or `fb_cookies.json` workflow.
- **DoD:** One full run produces logged quality percentages; Phase 1 time ≤ 200s or documented tradeoff; `compileall` clean.

---

## Sprint 6 — VRM yield + prediction-vs-outcome

**Theme:** Title/description VRM regex + wire calibration into visible summary.

- **Goal:** (a) VRM coverage in top-40 pool ≥ 25 (up from 19) via text regex before ANPR; (b) `--trades` output includes predicted vs realised profit comparison; (c) false positive rate ≤ 5% (DVLA verify where budget allows); (d) optional Obsidian VRM cache warm (`DEALERLY_OBSIDIAN_VRM_WARM=1`).
- **Primary files:** `vrm.py` (new `extract_vrm_from_text()`), `pipeline.py` Phase 3, `calibration.py`, `trades.py` / `analytics.py`, `obsidian_brain.py`.
- **Risks:** Regex false positives on model codes / postcodes / engine codes — reject known patterns.
- **DoD:** Measurable VRM increase vs baseline; prediction vs outcome visible in `--trades`; docs updated.

---

## Future sprints — checklist (Sprint 7+)

- [ ] **Sprint 7 — Report v2 depth:** Full scoring breakdown on expand, optional PDF export — **deps:** S4–6 stable data; **risk:** scope creep on PDF.
- [ ] **Sprint 8 — Motors hardening:** Payload drift tests, fewer Playwright fallbacks, mileage reliability — **deps:** none; **risk:** site structure changes. DoD: ≥80% Motors with make+model+mileage.
- [ ] **Sprint 9 — Obsidian intelligence:** "Seen before / price trend" badges from `dealerly.db` — **deps:** S6 VRM + item identity; **risk:** noisy badges on thin data. DoD: badge on ≥30% repeat vehicles.
- [ ] **Sprint 10 — Packaging:** `pip install` + `--quickstart` demo — **deps:** config surface frozen; **risk:** env var explosion. DoD: install + quickstart with only API keys.

---

## Opus prompt hygiene (paste near `Opus Goal`)

When planning **post–Sprint 6** work, add one line:

> **Sprints 1–6 complete on Dealerly 1.0 (`1.0.0-rc.1`).** Use `prompts/SPRINT_BACKLOG_4_6.md` (historical) + `SPRINT_RUN.md` for ground truth. Do not duplicate: priority enrichment, MOT tier, trade CLI/calibration, ANPR high-conf skip, VRM label expansion, reason strings, DVLA top-slice loosening, --no-facebook, VRM badge, FB quality counters/cap/early-exit, text VRM extraction, or prediction-vs-outcome wiring.

---

## Mapping external sprint lists → this file

| External / ad-hoc label | Here |
|-------------------------|------|
| "Sprint 3 — Phase 2 ≤80s" (standalone list) | **Deferred** — Phase 2 already 96s; comp cache already live |
| "Sprint 4 — NEXT_VERSION directives" | **Sprint 4** (DVLA loosen, --no-facebook, VRM badge, AT cache logging) |
| "Sprint 5 — FB quality" | **Sprint 5** (counters + cap + early-exit) |
| "Sprint 6 — VRM yield + calibration" | **Sprint 6** (text regex + prediction vs outcome) |
| "Sprint 7 — Report v2 + PDF" | **Future — Sprint 7** |
| "Sprint 8 — Motors hardening" | **Future — Sprint 8** |
| "Sprint 9 — Obsidian badges" | **Future — Sprint 9** |
| "Sprint 10 — pip install" | **Future — Sprint 10** |
| "Sprint 12 — Craigslist UK cars" | **Backlog — Sprint 12** (`SPRINT_RUN.md`); spike CL UK vs Gumtree UK |

---

## Sprint 12 (backlog — not scheduled)

- **Craigslist UK cars** — classifieds adapter epic: `CraigslistAdapter`, pipeline + report. **Spike:** UK URL coverage is thin on Craigslist; **Gumtree UK** may substitute if volume dictates. See `SPRINT_RUN.md` § Sprint 12, `SPRINT_PLAN.md`.

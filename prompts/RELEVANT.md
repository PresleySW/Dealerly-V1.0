# Dealerly ROI Queue (Live)

## 2026-03-21 — Web setup + map + FB diagnostics
- **`cli.py` web form:** `auction_only` + fixed `use_autotrader` checkbox semantics; AT default on.
- **`report.py`:** 3D section copy + jsdelivr Three.js; larger eBay/platform logos.
- **`facebook.py`:** Logs when 0 listings (block vs empty).
- **Figma:** See `prompts/FIGMA_WEBSITE_SYNC.md` — dashboard remains demo until assets/data wired.

## Sprint 16 (2026-03-21) — Dealerly Agent Loop — SHIPPED
- **`agent.py`** (new): Full observe→think→act→update→repeat loop. 3 actions: `search` (calls adapters), `score` (price-band heuristic), `finish`. Claude (or OpenAI/fallback) decides each step. Prints reasoning.
- **`config.py`**: `agent_mode: bool = False` added to Config.
- **`pipeline.py`**: `if cfg.agent_mode:` branch wraps Phase 1 — calls `run_dealerly_agent()`, uses returned listings. Existing Phase 1 path unchanged.
- **`cli.py`**: `--agent` flag activates via `dc_replace(cfg, agent_mode=True)`.
- **Architecture:** Tool layer (adapter wrappers) + Agent loop (10-step budget) + Model layer (Claude via offers.py, deterministic fallback). Phase 2–7 unchanged.

## Sprint 15 (2026-03-21) — Performance + Sources + Auction Intelligence — SHIPPED
- **Auction detection:** `is_auction` field on `Listing`; eBay reads `buyingOptions`; PistonHeads reads `saleType`. Report: amber `🔨 Auction` badge, left border, "Type" filter (All/Auction/Buy Now).
- **ULEZ text fix:** `detect_ulez_from_text()` in `vrm.py` scans listing titles/descriptions for explicit ULEZ phrases; overrides year+fuel inference. `_resolve_ulez()` helper in `pipeline.py` applied at all 3 ULEZ assignment sites + Phase 1 in `ebay.py`/`ingestion.py`.
- **Phase 1 speed 2×:** `ThreadPoolExecutor(max_workers=4)` runs adapters concurrently (~200s → ~70-80s). eBay per-query fetches parallelised with `max_workers=3`.
- **PistonHeads adapter:** `pistonheads.py` — `PistonHeadsAdapter(BaseIngestionAdapter)`. Three-tier parse: `__NEXT_DATA__` JSON → JSON script blocks → BeautifulSoup. 1.5s polite sleep. Wired into `ENABLED_PLATFORMS` + `_build_adapter_list()`.
- **Loading screen:** 9 phase chips with `gap: 22px 28px`, active/done/pending states, blue-glow pulse.
- **Manheim spike:** GATED — trade dealer login required for all data. No public scraping path. Future sprint if trade credentials obtainable.
- **Next:** AutoTrader UK full search adapter OR Cazoo scraper OR auto-documentation sprint.

## Sprint 14 (2026-03-21) — Report UI Polish — SHIPPED
- **`report.py`:** 3D map → Apple Maps style: animated sea shimmer, filled land polygons, glowing pin bars, warm lighting. Canvas 520px, FOV 38°. Map moved to **bottom** of report.
- **`report.py`:** Images enlarged — default thumb 260px/178px-min, featured 300px, compact 120px, dense 200px/240px.
- **`report.py`:** Header simplified — runtime banner hidden, condensed `h1`, meta uses `·` separators, 3 buttons only.
- **`report.py`:** Stat cards trimmed — VRM + MOT merged into one card; basket/platform-mix removed.
- **`report.py`:** Table: "DB rows" column removed, 15-row cap, `<details>` closed by default.
- **Cazoo:** Confirmed live (user correction 2026-03-21). Previous "defunct" note removed. Candidate for future scraper sprint.
- **Next:** AutoTrader UK search adapter OR Cazoo scraper.

## Sprint 13 (2026-03-21) — Gumtree spike (CLOSED: infeasible) + UX fixes — SHIPPED
- **Gumtree:** INFEASIBLE. `robots.txt` `Disallow: /*?` blocks all parameterized search. No path-based price filtering. Volume without params <10 relevant listings. Sprint closed per acceptance criteria.
- **`pipeline.py` + `cli.py`:** Antigravity changed from opt-out to **opt-in** (`DEALERLY_USE_ANTIGRAVITY=1`). Reports now open in default browser by default.
- **`report.py`:** 3D map camera target corrected to UK centre `(5, 3, 6)` (was `(0, 3, 0)` — Atlantic Ocean). Camera position `(5, 38, 58)`.
- **Motors:** Static tier returns landing pages (JS-rendered site). Playwright fallback required. Run `playwright install chromium` if Motors yields 0.
- **Cazoo:** Site is **live and accessible** — confirmed by user 2026-03-21 (previous "defunct 2023" note was wrong). `cazoo.png` logo in `Logos/` is valid. No scraper built yet — candidate for future sprint.
- **Next:** Sprint 14 — AutoTrader UK search adapter (public results, structured JSON, high UK volume) OR Phase 4 consolidation.

## Sprint 12 (2026-03-21) — ANPR profit-weighted budget + Motors pagination — SHIPPED
- **`pipeline.py`:** `_anpr_top_slice` replaced by `_anpr_budget_cap/used`; gate = `profit >= 1.5×anpr_min_profit` AND budget not exhausted. ANPR skip threshold 0.92 → 0.88. `anpr_profit_weight` imported.
- **`config.py`:** `DEFAULT_ANPR_PROFIT_WEIGHT = 1.5`, `anpr_profit_weight()`, `DEALERLY_ANPR_PROFIT_WEIGHT` env.
- **`motors.py`:** `_MAX_LOW_SIGNAL_PAGES` 2 → 3; hard single-page break removed (paginate up to threshold). `_jsonld_entity_to_listing` extracts `vehicleModelDate`, `mileageFromOdometer`, extra images.
- **Target:** >=8 ANPR calls utilized (was 5/20), Motors >=70 listings (was 40-51). Validate with next full run.

## Sprint 11b (2026-03-21) — image quality + VRM slice
- **eBay images:** `upgrade_ebay_image_url` → `s-l1600`; ANPR ranks larger `s-l*` higher; non-eBay ANPR slice 12/16.

## Sprint 12 backlog (eventual)
- **Craigslist UK cars** — new adapter + pipeline wiring (`SPRINT_RUN.md` § Sprint 12, `SPRINT_PLAN.md`). Feasibility spike: CL UK volume vs **Gumtree UK** alternative.

## Sprint 11 Handoff (2026-03-21) — Non-eBay ANPR + Marketplace UI + MOT BUY tiers
- **Problem:** VRM from clear listing photos failed on Facebook — Phase 3 only ran full enrichment (incl. ANPR) for **eBay**; FB CDN downloads used eBay `Referer` and failed.
- **`vision.py`:** `Referer` by host (facebook/fbcdn, motors.co.uk, eBay).
- **`pipeline.py`:** Phase 3 **non-eBay ANPR** pass on `first_image_url` + `extra_image_urls` (gated by prelim profit, not AVOID, slice cap).
- **`scoring.py`:** Tiered **keep BUY** when DVSA MOT missing but VRM + profit strong (repairs already in model).
- **`report.py`:** Marketplace = icon badge + `marketplace` in mix/Sources; filter button icon-only.
- **`Logos/` + `report.py`:** Optional `ebay.png`, `motors.png`, `marketplace.png` (data-URI badges); `cazoo.png` footer — see `Logos/README.md`.
- **Next:** Re-run pipeline; expect higher FB VRM rate when `PLATE_RECOGNIZER_TOKEN` set.

## Sprint 10 Handoff (2026-03-21) — v1.0.0-rc.1 UX polish + bug fixes — VALIDATED
- **Pipeline run**: 187 listings, 2 BUY / 7 OFFER / 6 PASS / 10 AVOID. VRM 89%. Zero crashes.
- **`mot_formatter.py`**: All font sizes bumped (0.82→0.95em header, 0.8→0.9em defects, 0.83→0.95em status chip). Card padding 8px→12px. Line-height 1.5.
- **`report.py`**: `_normalise_title()` fixes common seller typos (diesal, petral, mannual etc). `_applyVrm()` JS hides "Verification pending" label. `.expand-body` font 0.82→0.92em. Loading screen flex-wrap fix.
- **`pipeline.py` + `cli.py` + `report.py`**: All Unicode chars replaced with ASCII for Windows cp1252 console. `console_safe()` wraps dynamic strings.
- **`db.py`**: SQLite timeout 10→30s + busy_timeout pragma. **`insert_comps()`**: `BEGIN IMMEDIATE` + `executemany` + 12-attempt backoff (Phase 2 lock fix on large/Spyder runs). **`autotrader.py`**: same pattern for `autotrader_comps` writes. `config.py`: `DEALERLY_DB_FILE` env var.
- **`report.py`**: Fixed `NameError` on `_sm` free variable — moved definition before first use in top-3 strip.
- **Report**: `reports/archive/report_20260321_044003.html` (312 KB). All fixes confirmed in output.
- **Next**: Consider Motors.co.uk scraper overhaul (no `__NEXT_DATA__`, static fallback only gets 1-4 per query). ANPR budget review (2 calls made, 7 skipped). Phase 4 consolidation still pending.

## Sprint 5 Handoff (2026-03-20) — v1.0.0-rc.1 FB quality counters / image gallery / dealer pitch
- **`config.py`**: `fb_max_listings()` / `DEALERLY_FB_MAX_LISTINGS` (default 400, min 10).
- **`facebook.py`**: `FB_QUALITY` dict (reset per run) — `fb_total`, `fb_titles_good`, `fb_mileage_found`, `fb_thumb_found`. Scroll loop early-exit (<5 new × 2 cycles) + cap. `_title_is_good()` make-token check against `_KNOWN_MAKES` frozenset.
- **`pipeline.py`**: FB quality logged after Phase 1 adapter call; added to `_budget_counters`; surfaced in PIPELINE_REPORT `_fb_quality_line`. `fb_max_listings` imported from config.
- **`models.py`**: `extra_image_urls: str = ""` on `Listing` (Sprint 5 gallery).
- **`pipeline.py`** enrichment: gallery extras populated from `collect_image_urls` — always runs, stores `listing.extra_image_urls = ",".join(extras[:4])`.
- **`report.py`**: `_gallery_wrap_html()` — full `.thumb-wrap` div with `data-gallery`/`data-idx`, prev/next `gal-btn`, `gal-counter` hover badge. CSS + `galNav()` JS added.
- **`prompts/DEALER_PITCH.md`**: cold email, in-person script, objections, pricing tiers, demo checklist.
- **Next**: Sprint 8 — Phase 4 consolidation (merge Phase 4/4.5/4.6 into one pass, smoother loading bar) + Obsidian VRM cache warm (optional).

## Sprint 7 Handoff (2026-03-20) — v1.0.0-rc.1 item_vrm cache pre-fetch / AVOID ANPR gate / label expansion
- **`pipeline.py`** `_enrich_single_listing`: `get_item_vrm` now runs BEFORE `ebay_get_item()` — saves one eBay item API call per cache hit. `listing.raw` not set on pre-fetch cache returns (same as obsidian-cache path).
- **`pipeline.py`** Phase 3 loop: `anpr_for_listing` gate now excludes `prelim_out.decision == "AVOID"` — AVOID listings can't become BUY/OFFER regardless of VRM, so ANPR credits are saved.
- **`vrm.py`** `_LABEL_PATTERN`: Added "personal plate/reg/number", "cherished reg/plate/number", "has a reg", "own reg", "includes reg", "selling with reg", "has its reg" variants.
- **Next**: Sprint 8 — Phase 4 consolidation.

## Sprint 6 Handoff (2026-03-20) — v1.0.0-rc.1 VRM text pre-pass / prediction vs outcome
- **`vrm.py`**: `extract_vrm_from_text(text, year) -> list[tuple[str, float]]` — label scan (conf ≥ 0.96) + SAFE body scan (conf ≤ 0.88); deduplicated, sorted by confidence desc.
- **`pipeline.py`** Phase 3 pre-pass: runs on ALL `to_enrich` listings before ANPR sort; sets `listing.vrm` / `vrm_source="text_prepass"` when best candidate conf ≥ 0.70 + year-plausible. Reduces ANPR spend on Motors/FB.
- **`calibration.py`**: `prediction_vs_outcome(conn) -> list[dict]` — joins deal log CSV with `completed_trades` DB on normalised VRM. `format_prediction_vs_outcome()` renders plain-text table with mean delta + beat-forecast summary.
- **`trades.py`**: `print_trades_summary` now calls `prediction_vs_outcome` after manual accuracy section; cross-reference table shown when ≥1 VRM match exists.
- **Not implemented (deferred)**: Obsidian VRM cache warm (`DEALERLY_OBSIDIAN_VRM_WARM=1`, scan `Database/VRMs/*.md`).
- **Next**: NEXT_VERSION.md directives — VRM hit-rate improvement, ANPR credit efficiency.

## Sprint 4 Handoff (2026-03-20) — v1.0.0-rc.1 DVLA top-slice / --no-facebook / VRM badge / AT cache log
- **`pipeline.py`**: DVLA top-slice +4 for listings with `expected_profit >= 200` (per-listing `_dvla_slice_cap`). No blanket raise.
- **`pipeline.py`**: `DEALERLY_NO_FACEBOOK=1` env var filters "facebook" from `_enabled_platforms` before adapter build. Fast eBay+Motors path available without code changes.
- **`pipeline.py`**: AT comp cache hit/miss printed after Phase 2 and Phase 4; included in PIPELINE_REPORT line.
- **`cli.py`**: `--no-facebook` flag → sets env var before `run()`.
- **`report.py`**: BUY/OFFER no-VRM cards show `.tag-no-vrm` badge ("No VRM — contact seller"); light + dark CSS added.
- **`autotrader.py`**: `_cache_hits`/`_cache_misses` counters on `AutoTraderComps` instance — run-cache, DB-cache, and live-fetch paths all tracked.
- **Next**: Sprint 5 — Phase 4 consolidation (merge Phase 4 / 4.5 / 4.6 into one pass with smoother loading bar progression and shorter runtime).

## Sprint 3 Handoff (2026-03-20) — v1.0.0-rc.1 VRM + ANPR + Reason Strings
- **`pipeline.py`**: ANPR skipped when `vrm_confidence ≥ 0.92` (labelled text = as reliable as ANPR). Counter now tracks confident-skip + verified-skip together. Obsidian-cache paths already skipped ANPR naturally but were uncounted.
- **`vrm.py`**: Label pattern: added "my reg", "private reg/plate", "comes/sold with reg"; group cap 4–10; trail window 20 chars. No false positives on engine codes (tested).
- **`scoring.py`**: All 4 base reason strings enriched — BUY (profit £ + VRM hint), OFFER (max bid £ + net £), AVOID (worst-case repair £), PASS (asking £ + max bid £).
- **`NEXT_VERSION.md`**: Updated with Sprint 4 directives (Phase 1 runtime, DVLA top-slice, VRM badge, BUY count target).
- **Latest run**: 1 BUY, 9 OFFER, 11 PASS, 16 AVOID (BUY gate confirmed working post Sprint 1).

## Sprint 2 Handoff (2026-03-20) — v1.0.0-rc.1 SWAutos Integration
- **`trades.py`**: `import_trades_from_csv()`, `seed_demo_trades()`, `write_trade_to_obsidian()`.
- **`calibration.py`**: `calibrate_from_trades(conn, capital)` — real-outcome accuracy stats.
- **`cli.py`**: `--import-trades <csv>`, `--seed-demo-trades`; quickstart shows live-flips insight.
- **Meriva seeded**: KT06YNX → realised £515. Obsidian note written to `Trades/`.
- **Obsidian brain**: `_Dealerly_Brain.md` updated (Sprint 1–2 trade/SWAutos section + `Trades/` path documented).
- **Next**: Sprint 3 — NEXT_VERSION.md directives (VRM hit-rate, ANPR efficiency, Facebook title validation, scoring reason clarity). Run pipeline from `Dealerly 1.0/` to validate BUY unlock.

## Sprint 1 Handoff (2026-03-20) — v1.0.0-rc.1
- **Root cause fixed:** high-profit eBay candidates were credit-starved in Phase 3; ANPR/DVLA caps applied by position, not profit. Now sorted by `expected_profit` desc + priority pass (top-N always enriched).
- **scoring.py MOT gate:** confidence-tiered BUY exception — `profit >= 1.5 × target` + `vrm_confidence >= 0.8` stays BUY with "MOT pending" note. Blanket downgrade preserved for all other cases.
- **Acceptance target:** ≥1 BUY with verified MOT on next run (eBay+Motors+FB, £3k flipper).
- **Untouched:** `risk.py`, `report.py`, `facebook.py`. No risk gates weakened.
- **Env:** `DEALERLY_PRIORITY_ENRICH_N` (default 5) controls priority pass depth.

## Latest Session Handoff (2026-03-20)
- **`prompts/MASTER_PROMPT_OPUS.md`:** **Opus only** — lightweight planning; **deliverable** = two **Sonnet code prompts** (Sprint 1–2) from `MASTER_PROMPT_SONNET_CODE.md` + **Sprint 3+ backlog** for future Sonnet. Routine coding on **Sonnet**, not Opus (saves Opus %).  
- **`prompts/MASTER_PROMPT_SONNET_CODE.md`:** Canonical **Sonnet** paste template (Opus fills `[[placeholders]]`). Shorter variant: `prompts/SONNET_OPUS_SESSION_PROMPT.md`. Opus copy-paste + optional metrics: `prompts/MASTER_PROMPT_OPUS_COPYPASTE.md`.
- **Claude desktop blank window:** Anthropic app issue (WebView2 / firewall / reinstall) — **not** Dealerly. Use browser claude.ai or IDE; for offer text use OpenAI or local Ollama. See `prompts/TROUBLESHOOTING.md`.
- **Obsidian brain:** Full env + ANPR + local AI + Claude-app note written to vault root `_Dealerly_Brain.md` (local). Repo copy of ops troubleshooting: `prompts/TROUBLESHOOTING.md`.
- Facebook can now yield high volume when runtime path is healthy; quality (title/VRM/mileage) still lags — see snapshot below.
- **New:** Facebook cards pull `aria-label` / image `alt` for titles when span text is junk; main report grid round-robins platforms for BUY/OFFER; cards show scoring **reason** snippet; `fb_cookies.json` gitignored.
- **Windows integration (no Linux):** `DEALERLY_ANPR_MAX_IMAGES`, `DEALERLY_ANPR_MIN_PROFIT_GBP`; local OpenAI-compatible offers via `OPENAI_BASE_URL` on localhost; HTML report **runtime banner**; `DEALERLY_AI_BACKEND=local`. Details: `README.md`, `CLAUDE.md`, vault `_Dealerly_Brain.md`.
- **Cursor:** **Figma**, **Slack**, **GitLab** listed as relevant when integrations are on — see `.cursorrules` and *Cursor — integrated tools* in `README.md`.
- Runtime/API pressure in Phase 2 + Phase 3 has been reduced with stricter top-slice gates and lower candidate caps.
- Report now has explicit cart access (`Open cart`) plus a dedicated cart review panel.
- Visible-plate VRM recovery has been improved with ANPR OCR confusion repair.
- Seller fallback now prefers explicit platform labels for missing/masked seller location:
  - `ebay seller`
  - `motors seller`
- Zip export utility added for repeatable GitHub handoff (`export_dealerly_zip.ps1`).
- Maintain minimal token and API usage by default.

## Latest Run Snapshot (2026-03-20 12:11)
- Platforms now active: eBay `98`, Motors `32`, Facebook `943` (Facebook runtime path fixed).
- Decision output shifted to Facebook-heavy shortlist: BUY `0`, OFFER `18`, PASS `4`, AVOID `23`.
- Runtime hotspots moved to gather + preliminary scoring:
  - Phase1 `287.1s`
  - Phase2 `148.7s`
  - Phase3 `52.8s`
  - Phase4 `23.7s`
- API counters from latest run:
  - ANPR calls `3` (skipped by budget `24`)
  - DVLA calls `3` (+ `0` validation calls)
  - AutoTrader candidates: phase2 `96`, phase4 `48`
- Quality blockers:
  - Facebook cards still often show weak titles/locations; **mitigation in code:** prefer `aria-label` / image `alt` over span soup — needs fresh-run validation.
  - Facebook mileage often missing (hints now included in mileage regex scan).
  - Facebook VRM coverage remains weak.
  - Positive-profit top case (`~£444`) remained OFFER (no BUY); decision-gate explainability needs explicit validation (MOT/DVSA/risk-gate effects).
  - ANPR call budget may be suppressing VRM recovery in top rows; verify against credit availability and gating thresholds.

## Cursor — tools available (when connected)
- **Figma** — design / UX reference for report & setup UI.
- **Slack** — team coordination (no secrets in posts).
- **GitLab** — issues, MRs, pipelines (same hygiene as git; org may use GitLab alongside or instead of GitHub).
- Canonical wording: `.cursorrules` → *Cursor — relevant tools*.

## GitHub / Claude Code
- Source of truth: remote repo (e.g. `PresleySW/Dealerly-V1.0`). Clone → open folder in editor; AI uses **local clone**, not the website.
- **Git hygiene:** `dealerly/__pycache__/` removed from version control (2026-03-20); keep it untracked. **Obsidian:** vault path configurable via `DEALERLY_OBSIDIAN_VAULT`; entry doc `Dealerly_Vault/_Dealerly_Brain.md` (local only).
- Do **not** commit `dealerly/.env` (gitignored). Restore secrets locally after clone; rotate leaked keys.
- Master planning prompt (Opus): `prompts/MASTER_PROMPT_OPUS.md`. Sonnet implementation template: `prompts/MASTER_PROMPT_SONNET_CODE.md`.

## Current Goal
Outcompete sourcing-only tools by keeping Dealerly's USP as a decision-intelligence engine:
- stronger deal validation
- faster runtime per useful signal
- tighter risk filtering with actionable outputs

## Latest Run Findings
- Latest full run now shows Motors contributing listings (`Success: 42` in pipeline report).
- eBay + enrichment pipeline is producing BUY/OFFER, with Obsidian lead and VRM outputs active.
- Runtime drag remains concentrated in Motors fallback churn and enrichment/phase-7 overhead.

## Implemented This Pass
1. Motors static parsing strengthened (`_parse_anchor_context`) to reduce Playwright dependence.
2. Motors candidate URL list narrowed to avoid low-yield redirect paths.
3. Cazoo redirect pages treated as non-results for faster route progression.
4. Added runtime metrics:
   - Obsidian VRM cache hits
   - ANPR calls avoided via verified/cache paths
5. Added Motors fail-fast on generic landing pages to avoid repeated long Playwright fallback loops.
6. Added MOT confidence gate: when DVSA mode is enabled, listings without verified MOT history are downgraded from `BUY` to `OFFER`.
7. Added deep Motors extractor for inline `vehicles: [...]` Store payloads on generic landing pages (restores non-zero parsing path without JS-heavy fallback).
8. Added Obsidian graph-linking notes:
   - `Database/Items/*.md` and `Database/VRMs/*.md` auto-generated/updated
   - `Leads/_Leads_Index.md` index links for BUY lead notes
   - cross-links between leads, VRM scans, item notes, and VRM notes
9. Runtime tuning applied:
   - Motors per-page sleep reduced and warmup waits shortened.
   - Motors hard per-query time budget and low-signal-page cap added.
   - Obsidian historical graph backfill now runs only when needed (or if explicitly enabled with `DEALERLY_OBSIDIAN_BACKFILL=1`).
10. Offer generation defaults tuned for cost + quality:
   - OpenAI is now default backend in CLI prompts/quickstart.
   - Offer prompt refined to be more specific and buyer-conscious.
11. UI/UX quality pass:
   - Report styling upgraded for more SaaS-like presentation and clearer visual hierarchy.
   - MOT history rendering switched from dense table to readable test cards.
12. Motors listing quality:
   - Richer Motors titles (make/model + key attributes) and better image extraction from payload fields.
13. Obsidian graph/brain quality:
   - Added deterministic graph index renderer with overview + mermaid preview.
   - Added note tags for color grouping (`node/item`, `node/vrm`, `lead/buy`, risk tags).
   - Fixed historical scan-row parser to handle pipe-heavy item IDs and restore clean node backfills.
14. Antigravity integration:
   - CLI now detects Antigravity availability.
   - Optional report opening hook via `DEALERLY_OPEN_REPORT_IN_ANTIGRAVITY=1`.
15. Image quality:
   - Added `rank_images_for_display` heuristic and wired it to Phase 3 hero image selection.
16. Runtime + UX pass (latest):
   - Phase 2 candidate input capped for faster small-batch runs.
   - Phase 4 candidate limit reduced and Phase 4.5 rounds/batch size tightened.
   - Near-miss now reuses existing scored rows (removes extra full scoring pass).
   - Loading progress updates are now incremental within enrichment loops.
   - Motors image URLs normalized to absolute HTTPS for stronger report thumbnail reliability.
   - Report visuals softened to cleaner SaaS tone (less hazard-heavy risk messaging), wider thumbnails, and dark-first consistency with loading UI.
17. Budget UX + platform consolidation:
   - Added web-setup toggle to lock `price_max` to capital for simple "single budget" runs.
   - Added budget basket selection in scoring/report flow (portfolio of affordable BUY/OFFER listings under capital).
   - Replaced legacy `facebook_csv` / `facebook_paste` run modes with native `facebook` Marketplace adapter mode.
   - Enabled Facebook adapter in default `ENABLED_PLATFORMS` (self-checks Playwright/cookies and skips safely if unavailable).
18. Report UI polish (latest):
   - Increased listing card width and thumbnail size for clearer vehicle images.
   - Refined typography, spacing, and filter controls for a cleaner modern SaaS look.
   - Softened risk language and warning styling (less hazard-heavy, more review-oriented).
   - Reframed AVOID section to "Review Recommended" with calmer copy.
19. Runtime + VRM integration pass:
   - Raised standard shortlist enrichment baseline to `DEFAULT_SHORTLIST_ENRICH_N = 28`.
   - Added Phase 3 enrichment time-budget guard so runtime stays predictable even with higher enrich baseline.
   - Reduced enrichment overhead by removing per-listing artificial sleeps and tightening scrape timeout.
   - Made slow HTML page scrape fallback conditional on higher-profit candidates only.
   - Kept Phase 4 candidate scoring capped (`50`) to avoid runtime growth when enrich baseline increases.
   - Improved Obsidian brain linkage by adding item-id alias normalization (`v1|...|0` ↔ `v1-...-0`) for higher cache-hit compatibility.
   - Improved labelled VRM extraction robustness by scanning a trailing text window with full VRM patterns.
20. Report usability toggle:
   - Added persistent `Dense / Comfortable` view toggle in the report header.
   - View preference is stored in localStorage (`dealerly_density`) and applied on load.
   - Dense mode tightens card layout for faster scanning; comfortable mode keeps larger media-first cards.
21. Setup + loading UX cleanup:
   - Web setup now defaults lock-to-capital ON and uses auto-scaled target margin from capital/price band.
   - Setup form simplified to core controls (fewer advanced toggles/options shown by default).
   - Checkbox rows replaced with cleaner toggle-card styling.
   - Loading screen CSS fixed (`@keyframes` scope) and row layout hardened to prevent text overlap.
22. Runtime + reliability follow-up:
   - Fixed Phase 4.5 crash (`UnboundLocalError` on missing `o` variable in targeted enrichment loop).
   - Loading handoff now routes setup POST to a local `/loading` endpoint (avoids file URI redirect issues and preserves visible progress UI).
   - Reduced late enrichment cost (`MAX_ENRICH_ROUNDS=1`, round cap 4 listings, disabled expensive page-scrape fallback in Phase 4.5).
   - Tightened Phase 4 candidate scoring cap to keep runtime predictable.
   - Added description/title mileage fallback extraction for cases where item specifics are missing.
   - Added cleaner mileage pill in listing cards (`mileage n/a` fallback badge) for report readability.
23. API budget + UX + extraction pass (latest):
   - Tightened Phase 2/4 candidate caps and applied stricter ANPR/DVLA top-slice gating in enrichment loops.
   - Added API budget counters in logs/reports for ANPR calls/skips, DVLA calls/skips, item VRM cache hits, and AutoTrader scored candidate budgets.
   - Facebook adapter now continues without cookies, attempts consent dismissal, and retries deeper fallback scroll extraction.
   - Added explicit report cart panel with `Open cart` access while preserving add/remove behavior.
   - Added masked/missing seller location fallback labels (`ebay seller`, `motors seller`) in card metadata.
   - Added ANPR OCR confusion repair for visible plate recovery (0/O, 1/I, 5/S, 8/B).
   - Added repeatable zip export utility: `powershell -ExecutionPolicy Bypass -File .\export_dealerly_zip.ps1`.
24. Facebook + report UX pass (2026-03-20):
   - Facebook scrape captures `aria-label` and image `alt`/`title`; card parser prefers these over location-only span text.
   - Pipeline main-grid selection round-robins BUY/OFFER across platforms so mixed runs surface eBay/Motors beside Facebook.
   - HTML report cards show a truncated scoring **reason** (tooltip = full string) for BUY/OFFER/PASS.
   - `.gitignore` now excludes `fb_cookies.json`.
25. Windows-friendly integration pass (2026-03-20): env-tunable ANPR (`DEALERLY_ANPR_MAX_IMAGES`, `DEALERLY_ANPR_MIN_PROFIT_GBP`); local OpenAI-compatible offer AI (Ollama/LM Studio) via `OPENAI_BASE_URL` + optional omit `OPENAI_API_KEY` on localhost; HTML report runtime banner; `DEALERLY_AI_BACKEND=local` alias. (NemoClaw/OpenClaw still needs Linux/WSL.)
26. **Cursor tooling:** **Figma**, **Slack**, **GitLab** documented as in-scope when connected in Cursor (`.cursorrules`, `README.md`, `CLAUDE.md`, `RELEVANT.md`, session prompts, Obsidian `_Dealerly_Brain.md`, `dealerly-intel` skill).

## Runtime Notes
- Playwright Chromium is installed and launchable (`chromium_ok=True`).
- Motors issue was payload format drift on generic landing pages, not missing Chromium.

## Next ROI Steps
1. Run end-to-end validation to confirm Facebook **title** quality after aria/alt hinting (yield already high when Playwright path is healthy).
2. Re-check BUY/OFFER quality and runtime after stricter top-slice caps; tune only if decision quality drops.
3. Validate loading-screen to report handoff and cart panel behavior in a full report run.
4. Continue reducing Phase 7 cost (Obsidian write batching) if runtime remains above target.

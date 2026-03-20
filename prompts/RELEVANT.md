# Dealerly ROI Queue (Live)

## Latest Session Handoff (2026-03-20)
- Latest logs still show Facebook often `0`; fallback flow has now been strengthened for unauthenticated and consent-gated sessions.
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
  - Facebook cards still frequently show generic title/location strings (`London, United Kingdom`).
  - Facebook mileage often missing.
  - Facebook VRM coverage remains weak.
  - Positive-profit top case (`~£444`) remained OFFER (no BUY); decision-gate explainability needs explicit validation (MOT/DVSA/risk-gate effects).
  - ANPR call budget may be suppressing VRM recovery in top rows; verify against credit availability and gating thresholds.

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

## Runtime Notes
- Playwright Chromium is installed and launchable (`chromium_ok=True`).
- Motors issue was payload format drift on generic landing pages, not missing Chromium.

## Next ROI Steps
1. Run end-to-end validation to confirm Facebook yield improvement on a live session/cookie state.
2. Re-check BUY/OFFER quality and runtime after stricter top-slice caps; tune only if decision quality drops.
3. Validate loading-screen to report handoff and cart panel behavior in a full report run.
4. Continue reducing Phase 7 cost (Obsidian write batching) if runtime remains above target.

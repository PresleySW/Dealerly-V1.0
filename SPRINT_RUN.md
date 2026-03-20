## Status: Active — sprint continuation
Current runs are producing high-value BUY/OFFER opportunities, but Facebook yield, cart usability, and runtime/API efficiency remain critical.

## Latest Verified Run Snapshot
- Report source: `prompts/PIPELINE_REPORT.md` (2026-03-20 10:35)
- Platforms: eBay `151`, Motors `64`, Facebook `0`
- Decisions: BUY `1`, OFFER `13`, PASS `14`, AVOID `7`
- Runtime hotspots: Phase 2 (`125.9s`) and Phase 3 (`75.7s`)

## Completed In This Iteration
- **API/runtime gating pass:** Tightened Phase 2/4 candidate limits and applied stricter top-slice gates for Phase 3/4.5 ANPR and DVLA enrichment.
- **API budget counters:** Added explicit call/skip counters for ANPR, DVLA (including validation pass), item VRM cache hits, and AutoTrader scored candidate budgets.
- **Facebook fallback yield pass:** Adapter now runs unauthenticated fallback when cookies are missing, with cookie-consent dismissal and stronger fallback scroll cycles.
- **Cart access panel:** Added explicit `Open cart` access in report filter bar with a dedicated review panel of selected listings.
- **Seller fallback labels:** Missing or masked location labels now show platform-safe fallback text (`ebay seller`, `motors seller`) in cards.
- **VRM extraction robustness:** Added OCR-confusion repair for ANPR outputs (0/O, 1/I, 5/S, 8/B in expected plate positions) to recover visible-plate misses.
- **Zip handoff utility:** Added `export_dealerly_zip.ps1` for repeatable project zip export excluding DB/log/cache-heavy artifacts.

## Active Tasks (Priority Ordered)
1. **Facebook reliability (Critical)**
   - Validate non-zero listing yield in a live run after fallback changes.
   - Keep cookie/session setup deterministic for stable authenticated runs.
2. **API minimization and runtime control (Critical)**
   - Re-check BUY/OFFER quality after stricter caps.
   - Tune caps further only if runtime remains high vs decision quality.
3. **Cart accessibility (High)**
   - Validate panel behavior in browser on a fresh report run.
4. **VRM extraction on visible plates (High)**
   - Verify Skoda Octavia-type visible-plate case in next full run.
5. **Seller/dealer label fallback (Medium)**
   - Confirm all masked postcode-style labels are replaced consistently.
6. **Packaging + handoff (Medium)**
   - Zip export command added; verify archive contents before upload handoff.
   - Keep Obsidian/context handoff docs minimal-token and up to date.

## Guardrails
- Surgical diffs only.
- Do not modify `dealerly_log.csv`.
- Keep SQLite WAL mode enabled.
- Read `prompts/PIPELINE_REPORT.md` and `prompts/NEXT_VERSION.md` before changing thresholds.
- Minimize token usage in planning, prompts, and patch notes.
- Minimize external/API usage unless expected decision value is high.

## AI Next Steps Prompt
1. Stabilize Facebook yield and session handling before adding feature scope.
2. Add explicit cart-access UX (not only inline add/remove buttons).
3. Tighten VRM extraction where plate is visibly present in images.
4. Replace missing seller/dealer display with platform-specific fallback labels.
5. Add root-directory zip export task and Obsidian-context refresh docs.
6. Keep API and token usage minimal by default.
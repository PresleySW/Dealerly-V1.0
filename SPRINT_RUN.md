## Status: Active — sprint continuation
Latest runs show strong OFFER volume and Facebook ingest scale; BUY gating, Facebook card quality (title/VRM/mileage), and Phase 1/2 runtime remain priorities.

## Latest Verified Run Snapshot
- Report source: `prompts/PIPELINE_REPORT.md` (2026-03-20 **12:11** — refresh if newer run exists)
- Platforms: eBay `98`, Motors `32`, Facebook `943`
- Decisions: BUY `0`, OFFER `18`, PASS `4`, AVOID `23`
- Runtime hotspots: Phase 1 (`287.1s`), Phase 2 (`148.7s`); Phase 3/4 lower (`52.8s` / `23.7s`)

## Completed In This Iteration
- **API/runtime gating pass:** Tightened Phase 2/4 candidate limits and applied stricter top-slice gates for Phase 3/4.5 ANPR and DVLA enrichment.
- **API budget counters:** Added explicit call/skip counters for ANPR, DVLA (including validation pass), item VRM cache hits, and AutoTrader scored candidate budgets.
- **Facebook fallback yield pass:** Adapter now runs unauthenticated fallback when cookies are missing, with cookie-consent dismissal and stronger fallback scroll cycles.
- **Facebook card title hints:** Scrape now captures `aria-label` / image `alt`/`title` and prefers them over span soup when location-only titles appear; mileage regex also scans those hints.
- **Report top-row diversity:** Main display selection round-robins BUY/OFFER picks across platforms (by expected profit within each platform) so Facebook volume does not crowd out eBay/Motors in the primary grid.
- **Decision explainability on cards:** BUY/OFFER/PASS rows show a truncated scoring `reason` snippet (full text on hover) below the numeric row.
- **Cart access panel:** Added explicit `Open cart` access in report filter bar with a dedicated review panel of selected listings.
- **Seller fallback labels:** Missing or masked location labels now show platform-safe fallback text (`ebay seller`, `motors seller`) in cards.
- **VRM extraction robustness:** Added OCR-confusion repair for ANPR outputs (0/O, 1/I, 5/S, 8/B in expected plate positions) to recover visible-plate misses.
- **Zip handoff utility:** Added `export_dealerly_zip.ps1` for repeatable project zip export excluding DB/log/cache-heavy artifacts.
- **Git hygiene:** `fb_cookies.json` added to `.gitignore` (session export must not land in a public repo). `dealerly/__pycache__/` must **not** be tracked — use `git rm -r --cached dealerly/__pycache__` if it was committed by mistake.
- **Obsidian vault path:** `obsidian_vault_path()` in `config.py` + `DEALERLY_OBSIDIAN_VAULT` env; pipeline Phase 7 uses same root as brain load. Vault root doc: `Dealerly_Vault/_Dealerly_Brain.md`.

## Active Tasks (Priority Ordered)
1. **Facebook reliability (Critical)**
   - Re-run live scrape to measure **title quality** after aria/alt hinting (vs generic location strings).
   - Keep cookie/session setup deterministic for stable authenticated runs; keep `fb_cookies.json` local-only.
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
   - GitHub: source pushed to remote; `.env` gitignored — no secrets in repo.
   - Zip export (`export_dealerly_zip.ps1`) optional for offline handoff only.
   - Keep Obsidian/context handoff docs minimal-token and up to date.

## Guardrails
- Surgical diffs only.
- Do not modify `dealerly_log.csv`.
- Keep SQLite WAL mode enabled.
- Read `prompts/PIPELINE_REPORT.md` and `prompts/NEXT_VERSION.md` before changing thresholds.
- Minimize token usage in planning, prompts, and patch notes.
- Minimize external/API usage unless expected decision value is high.

## AI Next Steps Prompt
1. Facebook **quality** (title/VRM/mileage): validate aria/alt title picks in production HTML; iterate selectors if FB changes markup.
2. VRM yield vs ANPR budget / credits; labelled extraction and cache-first paths.
3. BUY vs OFFER explainability: card `reason` line is live — tune `scoring.py` copy if snippets are too noisy or cryptic.
4. Phase 1/2 runtime if gather + prelim scoring remain dominant.
5. Obsidian/context docs stay minimal-token; surgical edits + `python -m impeccable`.
6. Keep API and token usage minimal by default; production offer messages off unless explicitly enabled.
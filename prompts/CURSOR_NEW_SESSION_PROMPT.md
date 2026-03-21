You are continuing development on Dealerly.

## Context and Paths
- Root: `D:/RHUL/Dealerly/Dealerly 0.9/`
- Core package: `D:/RHUL/Dealerly/Dealerly 0.9/dealerly/`
- Reports: `D:/RHUL/Dealerly/Dealerly 0.9/reports/`
- Runtime logs: `D:/RHUL/Dealerly/Dealerly 0.9/prompts/PIPELINE_REPORT.md`, `D:/RHUL/Dealerly/Dealerly 0.9/prompts/NEXT_VERSION.md`
- Obsidian vault: `D:/RHUL/Dealerly/Dealerly_Vault/` — start at **`_Dealerly_Brain.md`** for env, ANPR tuning, local AI, Claude desktop vs Dealerly.

## Cursor tools (use when connected)
**Figma** · **Slack** · **GitLab** — available in Cursor when MCP/apps are enabled: design alignment, team comms (no `.env`), GitLab issues & pipelines. See `.cursorrules` → *Cursor — relevant tools*.

## Source-of-Truth Order
1. `CLAUDE.md`
2. `SPRINT_PLAN.md`
3. `SPRINT_RUN.md`
Then read:
- `.cursorrules`
- `.cursor/skills/dealerly-intel.md`
- `prompts/RELEVANT.md`
- `prompts/TROUBLESHOOTING.md` (Claude blank window, offer AI, ANPR, vault)

## Mandatory Constraints
- Surgical diffs only.
- Do not modify `dealerly_log.csv`.
- Keep SQLite WAL mode behavior intact.
- Minimize token usage in all analysis/outputs.
- Minimize API usage by default (cache first, top-slice gating, explicit budgets).

## Start By Auditing Latest Logs/Report
Read latest artifacts before coding:
- `prompts/PIPELINE_REPORT.md`
- `prompts/NEXT_VERSION.md`
- newest `reports/report_*.html`

Expected current state:
- Facebook still often `0` listings.
- AutoTrader is enabled but Phase 2/3 runtime remains expensive.
- Add-to-cart works, but cart is not clearly accessible as a view/panel.
- Some visible-plate listings still miss VRM extraction (example: Skoda Octavia top listing report case).
- Seller fallback currently shows masked IDs (e.g. `LS15***`) where explicit platform fallback is preferred.

## Development Tasks (Priority Order)
1. **API + runtime efficiency**
   - Reduce expensive fan-out in Phase 2/3.
   - Apply strict top-slice gates for AutoTrader/ANPR/DVSA calls.
   - Add/expand counters in logs for calls made vs skipped vs cache hits.
2. **Facebook yield**
   - Improve practical listing yield from Facebook setup/session flow and scraper fallback.
   - Ensure Facebook appears in report when listings are found.
3. **Cart access UX**
   - Keep current add/remove behavior.
   - Add an explicit cart access/panel/section so selected items can be reviewed in one place.
4. **VRM extraction improvement**
   - Improve extraction for listings with visibly readable plates (Skoda Octavia-type cases).
5. **Seller label fallback**
   - If seller/dealer unavailable:
     - eBay: show `ebay seller`
     - Motors: show `motors seller`
   - Only apply fallback when actual seller/dealer value is missing.
6. **Zip export utility**
   - Add a safe repeatable command/workflow to zip full Dealerly root for GitHub upload handoff.
   - Exclude heavy/cache files where appropriate.
7. **Plan completion sweep**
   - Check for any still-uncompleted work vs `SPRINT_PLAN.md` and update `SPRINT_RUN.md`.

## Validation
- Run lint/compile checks on changed files.
- Confirm report loads reliably from loading screen.
- Confirm cart is accessible and usable.
- Confirm seller fallback text behavior.
- Confirm no regressions to existing BUY/OFFER logic.

## Required Outputs
1. Implement code changes.
2. Update `SPRINT_RUN.md` with completed and remaining tasks.
3. Update `prompts/RELEVANT.md` with latest run + blockers.
4. Return concise summary: findings, changes, validation, remaining risks.

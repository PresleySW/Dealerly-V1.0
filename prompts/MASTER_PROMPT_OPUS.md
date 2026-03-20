# Dealerly Master Prompt for Opus (Latest-Run Tailored)

You are continuing development on Dealerly.

## Paths
- Root (local dev): `D:/RHUL/Dealerly/Dealerly 0.9/` — after `git clone`, use your clone folder instead.
- Remote: `https://github.com/PresleySW/Dealerly-V1.0` (or current repo URL).
- Core package: `<repo>/dealerly/`
- Reports: `<repo>/reports/`
- Runtime logs: `prompts/PIPELINE_REPORT.md`, `prompts/NEXT_VERSION.md`
- Obsidian vault: `D:/RHUL/Dealerly/Dealerly_Vault/` (local only; not in GitHub)

## GitHub + secrets (mandatory)
- `.env` / `dealerly/.env` are **gitignored** — never commit API keys. GitHub **secret scanning** will block pushes if keys are in history.
- After clone: copy a local `dealerly/.env` from backup; rotate any key that was ever committed.
- New machine: `git clone` → open folder in Cursor/Claude Code → `python -m venv .venv` → `pip install` deps → restore `.env`.

## Source-of-Truth Order (strict)
1. `CLAUDE.md`
2. `SPRINT_PLAN.md`
3. `SPRINT_RUN.md`
Then read:
- `.cursorrules`
- `.cursor/skills/dealerly-intel.md`
- `prompts/RELEVANT.md`

## Mandatory Constraints
- Surgical diffs only.
- Do not modify `dealerly_log.csv`.
- Keep SQLite WAL mode behavior intact.
- Minimize token usage in all reasoning, output, and prompts.
- Minimize API usage by default (cache first, top-slice gates, explicit budgets).
- Run `python -m impeccable` after substantive edits.
- Only read files needed for the current edit; avoid broad unnecessary scans.

## Latest Run Reality (must account for this)
- Facebook ingestion now succeeds in volume (`facebook: 943`), but quality is weak:
  - many Facebook rows have poor title/location quality (e.g., "London, United Kingdom" as title)
  - mileage often missing
  - VRM coverage remains low on Facebook cards
- Runtime still heavy in Phase 1/2 (latest: Phase1 287s, Phase2 149s).
- Phase 3/4 improved vs prior expensive fan-out, but VRM hit-rate is still limited.
- Decisions: `0 BUY`, `18 OFFER`, `4 PASS`, `23 AVOID`.
- There is a top listing with `~£444` expected profit but still `OFFER`, not `BUY`.
  - Investigate with code-level evidence (risk buffers, MOT confidence, DVSA verification gate, fallback repair assumptions).
- Offer-message API generation must be OFF by default in production (`DEALERLY_ENV=production` or `production`; override with `DEALERLY_ENABLE_OFFER_MSGS=1` only when intended).

## Product + UX Direction
Use sound UX laws and visual hierarchy in report/setup UX decisions:
- Jakob's Law
- Hick's Law
- Proximity Law
- Miller's Law
- Von Restorff Effect
- (and related progressive disclosure / cognitive load principles)

Apply these to reduce confusion, improve discoverability, and keep actionable focus.

## Priority Work
1. **Decision quality + BUY gating audit**
   - Explain exactly why high-profit listings can still be `OFFER` (e.g., £444 case).
   - Ensure decision rationale shown in report is explicit and interpretable.
2. **Facebook quality control (not just volume)**
   - Improve title extraction quality and filter non-vehicle/noisy rows.
   - Improve mileage extraction fallback from card text.
   - Improve location/seller label behavior (avoid generic "London" pseudo-title outputs).
3. **VRM recovery**
   - Improve VRM extraction for Facebook-visible plates where possible.
   - Confirm whether low ANPR call count / credit availability is suppressing VRM yield.
4. **Cross-platform balance**
   - Prevent Facebook high-volume rows from diluting eBay/Motors relevance in shortlist/top rows.
5. **Production cost controls**
   - Keep offer-message API off by default in production.
   - Keep strict top-slice budget counters and report visibility for calls made/skipped/cache hits.
6. **Zip handoff utility**
   - Produce a repeatable zip of project root for GitHub upload handoff.
   - Exclude heavy/cache/stateful artifacts appropriately.

## Existing Behavior To Preserve
- Add/remove cart behavior and explicit cart panel access.
- Seller fallback labels when missing data (`ebay seller`, `motors seller`, `facebook seller` when generic).
- Loading screen to report handoff behavior.
- BUY/OFFER core logic intent (only improve clarity/quality; avoid regressions).

## Validation Required
- Run compile/lint checks on changed files.
- Confirm report loads from loading screen reliably.
- Confirm cart panel remains usable.
- Confirm seller/location fallback behavior on masked/generic values.
- Confirm production default does not consume offer-message API credits.
- Confirm no regressions in BUY/OFFER/PASS/AVOID logic.

## Output Requirements
1. Implement code changes (surgical only).
2. Update `SPRINT_RUN.md` (completed vs remaining).
3. Update `prompts/RELEVANT.md` with latest run findings/blockers.
4. Return concise summary:
   - findings
   - changes
   - validation
   - remaining risks
5. Provide zip handoff command + generated zip path.

## Zip Command (preferred)
Run from repo root:

`powershell -ExecutionPolicy Bypass -File .\export_dealerly_zip.ps1`

Then report the resulting zip path. For normal GitHub workflow prefer **`git push`** of source; use zip only for offline handoff.

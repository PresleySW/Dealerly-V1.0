---
name: dealerly-intel
description: Enforces Dealerly-specific arbitrage intelligence for Motors Cloudflare fallback, Category S fuzzy detection, and Obsidian lead workflow. Use when editing motors.py, vrm.py, scoring.py, report.py, pipeline.py, or workflow.py.
---

# Dealerly Intel Skill

## Core Intent
Optimize Dealerly for UK automotive arbitrage with strict risk handling and high signal density.

## Mandatory Rules
1. **Fuzzy Category S recognition**
   - Always treat these tokens as Category S structural risk equivalents:
     - `Category S`
     - `Cat S`
     - `categorys`
     - `structural`
   - Apply this to scraped title, description, and listing metadata text.

2. **Motors.co.uk anti-bot fallback**
   - Preserve requests-first scraping.
   - On Cloudflare/challenge detection, use Playwright headless Chromium fallback.
   - Follow the browser/session/context implementation shape from `dealerly/facebook.py`.

3. **Obsidian strategy gate before scoring-weight changes**
   - Before changing scoring weights in `dealerly/scoring.py`, read relevant strategy notes in:
     - `D:/RHUL/Dealerly/Dealerly_Vault/`
   - Treat this as mandatory for constants/thresholds (for example resale multipliers, decision thresholds, Cat S penalties).
   - If notes conflict with code intent, prefer the documented strategy and annotate rationale in commit/worklog.

4. **BUY recommendation export**
   - Ensure BUY recommendations are exportable as Markdown into:
     - `D:/RHUL/Dealerly/Dealerly_Vault/Leads/`

## Safety and Quality
- Use surgical diffs only.
- Never edit `dealerly_log.csv`.
- Keep SQLite in WAL mode.
- Run `python -m impeccable` after substantive edits.

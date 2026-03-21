# Copy-paste: Claude **Opus** (master — architecture & Sonnet handoff)

**Keep short.** Full Sonnet template: **`MASTER_PROMPT_SONNET_CODE.md`**. Optional metrics appendix at bottom (refresh from `prompts/PIPELINE_REPORT.md`).

---

You are the **lead architect** on **Dealerly**: a Python CLI for UK used-car arbitrage — multi-platform ingest (eBay, Motors.co.uk, Facebook via Playwright), scoring, VRM enrichment (regex + Plate Recognizer ANPR + DVLA where enabled), DVSA MOT, HTML report with filters/cart, optional Obsidian export, SQLite (WAL).

**Opus Goal:** `<planning outcome — e.g. two Sonnet sprint prompts for X + Sprint 3+ backlog>`

**Repository root:** `D:/RHUL/Dealerly/Dealerly 0.9/` (or your clone). **Package:** `dealerly/`.

**Paths:** `dealerly/` · `dealerly.db` · `reports/` · `prompts/` · `dealerly_log.csv` (read-only) · Obsidian `Dealerly_Vault/` (`_Dealerly_Brain.md`, `Leads/`) · **`.cursorrules`** for full map · **`CLAUDE.md`** for modules.

**Read (lightweight; stop when you can plan):** `CLAUDE.md` (skim) → `SPRINT_RUN.md` → `prompts/RELEVANT.md` → skim `prompts/PIPELINE_REPORT.md` → optional `SPRINT_PLAN.md`. **No** full-repo scan. **Do not** write application code unless explicitly asked.

**Your deliverable (one reply):**
1. **Architecture & sequencing** (short).  
2. **Sonnet code prompt — Sprint 1** — full copy-paste block using **`prompts/MASTER_PROMPT_SONNET_CODE.md`** (fill `[[...]]`).  
3. **Sonnet code prompt — Sprint 2** — same.  
4. **Backlog — Sprint 3+** — bullets for future **Sonnet** work.  

**Sprints 1–2** = prompts for **Sonnet** to execute. **Sprint 3+** = deferred Sonnet backlog (same implementation tier; reserve Opus for the next planning pass).

**After Opus:** paste each Sonnet block into **Claude Sonnet (code)** — not Opus — to implement.

---

## Appendix — latest pipeline snapshot (optional context for Opus; refresh from `prompts/PIPELINE_REPORT.md`)

**Version (`CLAUDE.md`):** v0.9.9  

**Source:** `prompts/PIPELINE_REPORT.md` — run **2026-03-20 12:11**. *Synced to that file: 2026-03-20.*

| Field | Value |
|--------|--------|
| Mode / capital | Flipper / £3000 |
| Platforms (success) | eBay 98, Motors 32, Facebook 943 |
| Candidates | 1073 |
| VRMs (top 48 pool) | 28 |
| Obsidian VRM cache hits | 23 |
| ANPR avoided (verified/cache paths) | 0 |
| ANPR calls made | 3 (skipped budget 24, verified 0, cache 4) |
| DVLA | 3 + 0 validations; skipped top-slice 5 |
| AutoTrader scored | phase2 96, phase4 48 |
| DVSA verified | 27/48 |
| Decisions | BUY 0, OFFER 18, PASS 4, AVOID 23 |
| Phase timings | P1 287.1s, P2 148.7s, P3 52.8s, P4 23.7s |
| Errors | No critical ingestion errors detected |

Do **not** paste this whole table into Sonnet prompts unless a metric is decision-critical — point Sonnet to `prompts/PIPELINE_REPORT.md` instead.

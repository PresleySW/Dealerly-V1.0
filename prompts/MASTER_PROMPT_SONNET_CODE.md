# Dealerly — Sonnet code prompt (template)

**Sonnet** implements. **Opus** fills `[[PLACEHOLDERS]]`. Engineering rules are **global** in the repo — **do not** duplicate them at length here; **follow the `.md` files** below.

---

## Paste block (Opus: replace `[[...]]`)

### Architecture constraints (Opus — short)

[[modules, risks, non-goals — 2–6 lines]]

---

You are a senior software engineer on **Dealerly**: UK used-car arbitrage CLI — ingest (eBay, Motors, Facebook), scoring, VRM/MOT enrichment, HTML report, optional Obsidian, SQLite (WAL).

**Sprint:** [[Sprint 1 | Sprint 2]]

**Goal:** [[one concrete outcome + acceptance line]]

**Repo:** `D:/RHUL/Dealerly/Dealerly 0.9/` (or your clone) · package **`dealerly/`**

**Directory map (from repo root — for targeting):** **`dealerly/`** Python package · **`dealerly.db`** SQLite (WAL) · **`reports/`** HTML output · **`prompts/`** handoff + **`PIPELINE_REPORT.md`** · **`dealerly_log.csv`** read-only (never modify) · **`export_dealerly_zip.ps1`** zip utility (repo root). **Obsidian:** default vault **`Dealerly_Vault/`** (machine path often `D:/RHUL/Dealerly/Dealerly_Vault/`) — brain **`_Dealerly_Brain.md`**, leads **`Leads/`**, override **`DEALERLY_OBSIDIAN_VAULT`**. **Absolute paths on this machine:** **`.cursorrules`** (authoritative map).

**Follow repo rules (global — not repeated here):** **`CLAUDE.md`**, **`.cursorrules`**, **`README.md`** (env/setup), **`SPRINT_RUN.md`**, **`prompts/RELEVANT.md`**, **`prompts/PIPELINE_REPORT.md`** (latest run; numbers may be stale). Optional: **`SPRINT_PLAN.md`**. Obsidian/env: **`Dealerly_Vault/_Dealerly_Brain.md`**. **Conflict:** `CLAUDE.md` + `.cursorrules` override guesses.

**Read order (stop when you have enough context):** skim **1 → 5** in that list as needed, **then** open **only** the `dealerly/*.py` / `prompts/*.md` files this Goal requires — **no** full-repo scans.

**Deliverables:** ship the Goal; short **`SPRINT_RUN.md`** + **`prompts/RELEVANT.md`** updates; compact reply (files touched, validate, risks). If behaviour/env changes materially, update **`README.md` / `CLAUDE.md` / vault brain** as those docs already describe.

**Validation:** per **`CLAUDE.md`** / **`.cursorrules`** — e.g. `python -m compileall dealerly`, `python -m impeccable` when available.

---

## Paste block ends

## Notes for Opus

- Keep Sonnet prompts **short**; the repo `.md` files are the rulebook.  
- **[[Architecture constraints]]**: bullets + file paths only; **preserve the operator’s tone** in any prose you add there.  
- Avoid long numbered “implementation approach” lists — Sonnet will design from code; only add 2–3 hints if a trap must be avoided.  
- Do not paste large **`PIPELINE_REPORT`** tables unless a number blocks the Goal.  
- **`CLAUDE.md`** lists key **`dealerly/*.py`** modules — use with the directory map in the paste block.  
- If **`MASTER_PROMPT_OPUS.md`** scoped work to **`Dealerly 1.0/`**, state **`Repo:`** and bootstrap-from-0.9 in **[[Goal]]** or constraints, not as a wall of steps.

# Dealerly

UK car flip intelligence CLI: multi-platform ingest, scoring, VRM/MOT enrichment, HTML report.

## Quick start (after `git clone`)

1. **Python 3.10+** — create a venv and install deps (see `CLAUDE.md` / your usual `pip` set).
2. **Secrets** — copy `dealerly/.env` locally (file is **gitignored**; never commit API keys). GitHub will reject pushes that include secrets.
3. **Run** — from repo root: `python -m dealerly.cli` (or your existing entrypoint per `CLAUDE.md`).

## Docs

| File | Purpose |
|------|--------|
| `CLAUDE.md` | Architecture + coding standards |
| `SPRINT_PLAN.md` / `SPRINT_RUN.md` | Roadmap + current status |
| `.cursorrules` | Editor/agent rules |
| `prompts/RELEVANT.md` | Live ROI / handoff queue |
| `prompts/MASTER_PROMPT_OPUS.md` | Full-session prompt for Opus / large tasks |

## Optional zip handoff

```powershell
powershell -ExecutionPolicy Bypass -File .\export_dealerly_zip.ps1
```

Prefer **git push** for GitHub; use zip for offline backup only.

## License / usage

Private project — adjust as needed.

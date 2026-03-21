# GitHub project template — Dealerly v1.0.0-rc.1

Use this when creating a **new GitHub repository** or aligning an existing one with the current app (not the ECC plugin subtree). Pairs with **`.github/workflows/dealerly-ci.yml`** at the workspace root.

## Project metadata

| Field | Value |
|--------|--------|
| **Name** | `Dealerly` or `dealerly-v1` |
| **Description** | UK car-flip intelligence CLI: eBay/Motors/Marketplace/PistonHeads ingestion, ANPR+VRM+DVSA+MOT scoring, HTML reports + Vite/React dashboard. Python 3.10+. Version **v1.0.0-rc.1**. |
| **Topics** | `python` `sqlite` `playwright` `vite` `react` `uk` `automotive` `arbitrage` |
| **Default branch** | `main` |
| **Visibility** | Private until you intend to open-source |

## README snippet (GitHub homepage)

```markdown
# Dealerly v1.0.0-rc.1

Car-flip intelligence for the UK: multi-source listings, profit scoring, MOT/DVLA verification, interactive HTML reports.

- **Python package:** `Dealerly 1.0/dealerly/`
- **Dashboard:** repo root — `npm install` / `npm run build` (Vite + React + Three.js map chunk)
- **Sprint log:** `Dealerly 1.0/SPRINT_RUN.md`
- **CI:** `.github/workflows/dealerly-ci.yml` (Python compile + Vite build; ECC tests allowed to fail)

**Secrets:** never commit `.env`, `fb_cookies.json`, or API keys — GitHub **push protection** will block many patterns.

**Cursor MCP (when connected):** Figma, Slack, GitLab, Chrome DevTools — see repo `.cursorrules` / `Dealerly 1.0/prompts/FIGMA_WEBSITE_SYNC.md`.
```

## Suggested labels (Issues / PRs)

| Label | Colour (hex) | Use |
|--------|----------------|-----|
| `pipeline` | `#1f75cb` | Phase 1–7 orchestration |
| `scraping` | `#c17d10` | Adapters, rate limits, Cloudflare |
| `report` | `#8fbc8f` | `report.py`, HTML, 3D map |
| `dashboard` | `#6366f1` | `src/` Vite app, Figma parity |
| `windows` | `#6b7280` | Path/encoding issues |
| `security` | `#dc2626` | Secrets, `.env`, credentials |

## CI (implemented)

The workspace includes **`.github/workflows/dealerly-ci.yml`**:

- **Python:** `python -m compileall dealerly -q` from `Dealerly 1.0/`
- **Vite:** `npm run build` at repo root
- **ECC tests:** `npm test` with `continue-on-error: true` (many cases are integration/posix-specific)

To tighten CI later: add `Dealerly 1.0/requirements.txt` and `pip install -r requirements.txt` before compileall; split ECC into a separate optional workflow.

## Branch protection (recommended)

- Require PR reviews for `main` (or your release branch).
- Require status checks: at least **vite** (and **python** when stable).
- Block force-push on `main`.

## Upload checklist

1. **Exclude secrets:** `.env`, `fb_cookies.json`, API keys — confirm `.gitignore`.
2. **Choose scope:** whole workspace vs `Dealerly 1.0/` only (smaller clone for Python-only work).
3. **Mirror:** GitHub ↔ GitLab — use `prompts/GITLAB_PROJECT_TEMPLATE.md` for GitLab-specific CI labels.
4. **Tags:** `v1.0.0-rc.1` etc. — document in `SPRINT_RUN.md` when cutting releases.

## Version sync

When bumping **`dealerly/config.py` `VERSION`** or git tag:

- Update `Dealerly 1.0/SPRINT_RUN.md` (top sprint section).
- Update this file’s title line and README snippet.
- Sync `src/app/demoRun.ts` from `prompts/PIPELINE_REPORT.md` if the dashboard should mirror the latest run.

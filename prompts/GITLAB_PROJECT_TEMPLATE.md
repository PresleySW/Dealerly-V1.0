# GitLab project template — Dealerly v1.0.0-rc.1

Use this when creating a **new GitLab project** or mirroring from GitHub. Keeps description, labels, and CI aligned with the current app (not the ECC plugin subtree).

**GitHub equivalent:** `prompts/GITHUB_PROJECT_TEMPLATE.md` and **`.github/workflows/dealerly-ci.yml`** at the workspace root.

## Project metadata

| Field | Value |
|--------|--------|
| **Name** | `dealerly` or `Dealerly-V1` |
| **Slug** | `dealerly` |
| **Description** | UK car-flip intelligence CLI: eBay/Motors/Marketplace/PistonHeads ingestion, ANPR+VRM+DVSA+MOT scoring, HTML reports + optional React dashboard. Python 3.10+. Version **v1.0.0-rc.1**. |
| **Topics** | `python` `sqlite` `playwright` `vite` `react` `uk` `automotive` `arbitrage` |
| **Default branch** | `main` |
| **Visibility** | Private until you intend to open-source |

## README snippet (GitLab homepage)

```markdown
# Dealerly v1.0.0-rc.1

Car-flip intelligence for the UK: multi-source listings, profit scoring, MOT/DVLA verification, interactive HTML reports.

- **Code path:** `Dealerly 1.0/dealerly/` (Python package)
- **Dashboard:** repo root `npm run build` (Vite + React)
- **Sprint log:** `Dealerly 1.0/SPRINT_RUN.md`
- **Obsidian vault:** optional local knowledge layer; set `DEALERLY_OBSIDIAN_VAULT`

**Cursor MCP (when connected):** Figma, Slack, GitLab, Chrome DevTools — see `.cursorrules`.
```

## Suggested labels

| Label | Colour | Use |
|--------|--------|-----|
| `pipeline` | #1f75cb | Phase 1–7 orchestration |
| `scraping` | #c17d10 | Adapters, rate limits, Cloudflare |
| `report` | #8fbc8f | `report.py`, HTML, 3D map |
| `windows` | #6b7280 | Path/encoding/Spyder issues |
| `security` | #dc2626 | Secrets, `.env`, credentials |

## Minimal `.gitlab-ci.yml` (optional)

Place at **repository root** if the remote tracks the whole Dealerly workspace; use **`Dealerly 1.0/.gitlab-ci.yml`** if the GitLab project only contains `Dealerly 1.0/`.

```yaml
# Dealerly — minimal verify (adjust paths if repo is Dealerly-only subfolder)
stages: [verify]

variables:
  PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip"

python_compile:
  stage: verify
  image: python:3.11-slim
  script:
    - cd "Dealerly 1.0"  # remove this line if project root IS Dealerly 1.0
    - if [ -f requirements.txt ]; then python -m pip install -r requirements.txt; fi
    - python -m compileall dealerly -q

node_ecc_tests:
  stage: verify
  image: node:20-bookworm
  script:
    - npm ci || npm install
    - npm test
  allow_failure: true  # ECC harness: many tests are integration/posix-specific

vite_build:
  stage: verify
  image: node:20-bookworm
  script:
    - npm ci || npm install
    - npm run build
```

**Note:** Add `requirements.txt` under `Dealerly 1.0` if you want deterministic CI installs; until then `compileall` alone validates syntax.

## Upload checklist

1. **Exclude secrets:** `.env`, `fb_cookies.json`, API keys — confirm `.gitignore`.
2. **Choose scope:** whole workspace vs `Dealerly 1.0/` only (smaller, clearer for app issues).
3. **Mirror:** GitHub `Dealerly-V1.0` → GitLab pull mirror, or push `Dealerly 1.0` as single project.
4. **Protected branches:** protect `main`; require MR for production tags `v1.*`.

## Version sync

When bumping **`dealerly/config.py` `VERSION`** or tag:

- Update `Dealerly 1.0/SPRINT_RUN.md` (top sprint section).
- Update `Dealerly_Vault/Prompts/CLAUDE.md` (or replace body with pointer to `Dealerly 1.0/CLAUDE.md`).
- Update this file’s title line and README snippet.

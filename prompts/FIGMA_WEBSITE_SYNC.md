# Figma → Dealerly dashboard (`src/`)

The Vite app at the repo root is **not** auto-synced from Figma. With **Figma** and **Antigravity** (or Cursor) installed, use this flow so the UI actually picks up your file.

## Quick path

1. **Export raster/vector** — In Figma: select logo frames → right **Export** (SVG for icons, PNG @2x for photos). Drop files into **`public/logos/`** or **`public/hero/`** (see `public/README.md`). Vite serves them as `/logos/...`.
2. **Wire in React** — Use `<img src="/logos/your-export.svg" alt="" />` or import from `public/` URLs in `App.tsx`, `SourceStrip`, `ListingCard` (`platformLogo` prop).
3. **Design tokens** — Paste **Dev Mode → CSS** variables into a new file `src/styles/figma-tokens.css`, then `@import` it from the commented slot at the top of `src/styles/theme.css` (keep Tailwind v4 valid tokens).
4. **Demo numbers** — Dashboard copy that mirrors the pipeline lives in **`src/app/demoRun.ts`** (sync from `Dealerly 1.0/prompts/PIPELINE_REPORT.md` when you refresh the marketing view).
5. **Python HTML report** — Still uses **`Dealerly 1.0/Logos/`** and `report.py`; `public/` is **Vite-only**.

## Cursor — Figma MCP (make integration work)

The **official Figma MCP** ships with Cursor’s MCP directory (`plugin-figma-figma`). Use this checklist so agents can call **`get_design_context`**, **`get_screenshot`**, **`get_metadata`**, and (optional) Code Connect tools.

### 1. Turn the server on

1. Open **Cursor Settings → MCP** (or **Features → MCP** depending on build).
2. Enable **Figma** / **Figma MCP** if it appears as a bundled server. If you add it manually, use Figma’s documented MCP endpoint and complete **browser login** when Cursor opens the OAuth / Figma tab.
3. **Restart Cursor** after enabling if tools do not appear under the MCP list.

### 2. Permissions and file access

- The Figma file must be openable by the **same Figma account** you authenticated to MCP (team seat / file share as usual).
- If calls return **403** or empty data: open the file in the browser while logged in, confirm you are not on a **view-only** link that blocks Dev Mode/API-style access, and retry.
- Use the MCP **`whoami`** tool to confirm which Figma user is authenticated.

### 3. URL → `fileKey` + `nodeId` (for agents)

| URL pattern | Use |
|-------------|-----|
| `figma.com/design/:fileKey/...?node-id=12-34` | `fileKey` from path; `nodeId` = `12:34` (hyphens in query → colons) |
| `.../design/:fileKey/branch/:branchKey/...` | Use **`branchKey` as `fileKey`** |
| `figma.com/make/:makeFileKey/...` | Use **`makeFileKey`** per Figma Make rules |

Always pass **`clientLanguages`** / **`clientFrameworks`** when the tool asks (e.g. `typescript`, `react`).

### 4. Design-to-code workflow (Dealerly)

1. **`get_design_context`** on the target frame → reference layout + suggested code.
2. **Adapt** to this repo: Vite + React + Tailwind v4 under **`src/`**; reuse `ListingCard`, `SourceStrip`, `theme.css` tokens.
3. **Export** logos/hero images to **`public/logos/`** or **`public/hero/`**; commit them — Vite does not load binary assets from MCP alone.
4. **Tokens**: paste Dev Mode CSS into **`src/styles/figma-tokens.css`**, then uncomment **`@import './figma-tokens.css';`** in **`src/styles/theme.css`**.
5. **HTML report** (`report.py`) still uses **`Dealerly 1.0/Logos/`**; mirror critical marks there if the report must match Figma.

### 5. Optional: canonical file for the team

Add your production file link (and main dashboard frame `node-id`) to the project wiki or a private note — agents only need the **URL** on each session. Do **not** commit secrets; file URLs are fine if the file is already shared with the team.

### 6. Cursor rule

Repo rule **`.cursor/rules/dealerly-figma-mcp.mdc`** reminds the agent to use Figma MCP when `src/` or report UI is in scope.

## Antigravity

Use Antigravity for layout exploration or motion; **ship** assets by exporting from Figma into `public/` and committing — the running app only reads what is in the repo.

## Still separate

- **Live pipeline JSON** in the dashboard = future sprint (small API or static snapshot from `reports/`). Until then, sync **`src/app/demoRun.ts`** from **`Dealerly 1.0/prompts/PIPELINE_REPORT.md`** after each run you want reflected in the UI.

## 3D map (both surfaces)

- **HTML report:** `Dealerly 1.0/reports/report_*.html` — WebGL UK coverage (triple-CDN Three.js loader in `report.py`).
- **React dashboard:** `src/app/components/dealerly/PipelineCoverageMap.tsx` — real **Three.js r128** + OrbitControls, lazy-loaded chunk; uses **`public/uk_gbr_outline.json`**. Keep visual language aligned with the report when changing either file; Figma MCP can inform layout/legend copy for both.

# HTML report logos (optional)

Files here are **base64-embedded** into generated HTML (no external URLs).

## Bundled assets (current)

| File | Use |
|------|-----|
| `ebay-logo-png_seeklogo-269395.png` | eBay — cards + filter |
| `facebook-marketplace-logo.png` | Marketplace (facebook slug) — cards + filter |
| Any `*marketplace*.png` / `*facebook*marketplace*.png` | If several exist, the **newest file by date** is used (so you can drop a refresh without deleting backups). |
| `Motors-1.png` | Motors.co.uk — cards + filter |
| `Cazoo.png` (or any `*cazoo*` image) | Optional footer mark |

## Resolution rules (`dealerly/report.py`)

1. **Explicit list** per platform (case-insensitive basename match).
2. **Glob fallback** if none match: e.g. `ebay*.png`, `*marketplace*.png`, `motors*.png`.
3. **Footer:** `cazoo.png` / `Cazoo.png` / `CAZOO.png`, or any file matching `*cazoo*` with extension **.png**, **.webp**, **.jpg**, or **.jpeg**.

You can still use short names such as `ebay.png` or `marketplace.png` — they are tried after the filenames above.

Fallback when no file matches: text label, or inline SVG for Marketplace only.

# Dealerly

## What this is
A modular Python CLI tool for car flip intelligence.
Scans eBay UK and Motors.co.uk for undervalued cars, scores them, extracts VRMs via regex + ANPR, checks MOT history via DVSA, estimates repair costs and profit, and outputs an interactive HTML report.

## Current version
v0.9.9

## Coding Standards & Patterns
* **Type Hinting:** Mandatory for all function signatures (Python 3.10+).
* **Ingestion Architecture:** All marketplace scrapers must inherit from `BaseIngestionAdapter` in `ingestion.py` and return a `List[Listing]`. Look at `facebook.py` for how to implement Playwright, and `ebay.py` for API handling.
* **Scraping Safety:** Always use `User-Agent` rotation (`_random_headers()`). Handle Cloudflare 403s/429s gracefully with a Playwright fallback or a clear error log, never a hard crash.
* **Data Models:** Always map scraped data to the `Listing` dataclass immediately. No loose dictionaries passed between pipeline phases.
* **Logging:** Use `--debug` JSON logging for all API calls (DVSA, Plate Recognizer). Do not clutter stdout.
* **File Edits:** Do not rewrite entire files. Use surgical diffs via Oh My Claude. Run Impeccable for linting after edits.

## Key modules (18 files)
### Pipeline & orchestration
- `pipeline.py` — 7-phase orchestration (gather → score → enrich → rescore → offers → analytics → workflow)
- `cli.py` — CLI entry point, flags (`--mode`, `--debug`, `--vrm-lookup`, `--watchlist`)
- `config.py` — all constants, mode profiles (flipper/dealer), query presets

### Ingestion adapters
- `ingestion.py` — BaseIngestionAdapter ABC + EbayIngestionAdapter wrapper
- `ebay.py` — eBay Browse API search + comps + item details
- `motors.py` — Motors.co.uk adapter (JSON/BS4/regex tri-tier parsing)
- `facebook.py` — Facebook Marketplace adapter (Playwright, optional)

### VRM & vehicle verification
- `vrm.py` — VRM extraction (regex patterns + DVLA validation + year plausibility)
- `vision.py` — ANPR integration (Plate Recognizer API) with image ranking
- `dvla.py` — DVLA vehicle enquiry API
- `mot.py` — MOT provider interface (mock-json + DVSA OAuth2)
- `mot_formatter.py` — DVSA payload → HTML table rendering

### Scoring & risk
- `scoring.py` — deal scoring pipeline with MOT integration + fraud detection
- `repair.py` — repair cost estimation + MOT confidence (p_mot)
- `risk.py` — shock ratio + decision logic (BUY/OFFER/PASS/AVOID)

### Output & workflow
- `report.py` — HTML report generation (cards, filters, dark mode, thumbnails, MOT dropdowns)
- `workflow.py` — CRM lead pipeline (auto-create leads for BUY/OFFER)
- `sheets.py` — Google Sheets export
- `analytics.py` — price trends + demand signals

## Stack & Environment
Python 3.10+, SQLite (WAL mode), Playwright (optional, for Facebook/Motors), gspread (Google Sheets), Plate Recognizer API, Anthropic/OpenAI APIs, requests, BeautifulSoup4.
- **Never touch:** `dealerly_log.csv` (production data, read only).
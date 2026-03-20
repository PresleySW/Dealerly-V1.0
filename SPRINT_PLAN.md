# Dealerly v0.10 — Development Plan

## Competitive Context
Dealerly's moat is decision intelligence — verifying deals via MOT history, DVLA validation, ANPR plate reading, repair cost estimation, ULEZ compliance, and risk scoring. No competitor does this for the UK market. Double down on this advantage while closing the sourcing gap.

## Sprint Breakdown & Definition of Done (DoD)

### Sprint 1 (ship first — highest ROI, lowest risk)
* Smarter ANPR image selection (`Priority 1`)
* Report car thumbnails (`Priority 2a`)
* Image URL persistence in pipeline
* **Definition of Done:** `rank_images_for_anpr()` is implemented and called before the ANPR API. `first_image_url` is attached to the `Listing` object and renders properly as a thumbnail in the HTML report.

### Sprint 2 (visual + workflow)
* Report UI full overhaul (`Priority 2b-2d`)
* Google Sheets export (`Priority 4a`)
* Dealer/Flipper mode profiles (`Priority 5`)
* **Definition of Done:** Report is fully CSS-styled with a card layout, dark mode toggle, and manual VRM fallback. Pipeline pushes a row to Google Sheets. Running `--mode flipper` overrides config defaults.

### Sprint 3 (multi-marketplace)
* Ingestion adapter architecture (`Priority 3a` + `3d`)
* Facebook Marketplace automated scraping (`Priority 3b`)
* Motors.co.uk adapter (`Priority 3c`)
* **Definition of Done:** `FacebookAdapter` and `MotorsAdapter` inherit from `BaseIngestionAdapter`. Pipeline orchestrates a multi-platform gather phase, deduplicates cross-platform listings, and logs sources in the HTML report.
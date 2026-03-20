# Dealerly v0.9.3 — Changelog

## Patch 1: AVOID Flooding Fix + Enrichment Budget

### Problem
Results 5–15 in the report were all AVOIDs — fraud flags and shock warnings that
passed the MIN_DISPLAY_PROFIT filter because they had high theoretical "profit"
but were flagged as scams/high-risk. The main display was unusable.

### Changes
- **pipeline.py**: Phase 4 now fetches a wider pool (4× DEFAULT_TOP_N), splits
  results into `top_rows` (BUY/OFFER/PASS only, capped at 15) and `avoid_rows`
  (AVOID, capped at 10). AVOIDs never appear in the main table.
- **report.py**: New `avoid_rows` parameter on `generate_html_report()`. AVOID
  listings render in a **collapsed `<details>` section** below the main table —
  visible for reference but not flooding the view.
- **config.py**: `DEFAULT_SHORTLIST_ENRICH_N` raised from 18 → 25. At 2 images
  per listing, that's ~50 ANPR calls/run × 2,500/month = ~50 runs. Comfortable.

---

## Patch 2: Targeted Searching + Model-Aware Risk

### Problem 1: Unprofitable models wasting search slots
Preset 6 searched 8 models including VW Golf (tier 4: DPF/DSG at this price band)
and Renault Clio (EDC gearbox failures). These rarely produced BUY decisions.

### Changes
- **config.py**: Preset 6 trimmed from 8 → 6 models. Removed VW Golf and
  Renault Clio. Added Honda Jazz (tier 1, consistently profitable).
  New preset: Fiesta, Corsa, Polo, Focus, Jazz, Yaris.

### Problem 2: No location filtering
eBay search used `itemLocationCountry:GB` only — returning cars from Scotland
when you're based in Egham. Transport costs make distant cars unprofitable.

### Changes
- **config.py**: New `DEFAULT_BUYER_POSTCODE` (TW200AY) and
  `DEFAULT_SEARCH_RADIUS_MILES` (75). Added to Config dataclass.
- **ebay.py**: `ebay_search()` now accepts `buyer_postcode` and
  `search_radius_miles`. Sends `X-EBAY-C-ENDUSERCTX` header with
  `contextualLocation=country=GB,zip=TW200AY` to bias results toward Egham.
  Note: eBay Browse API uses this for **relevance ranking** (closer items
  rank higher), not hard distance filtering.
- **pipeline.py**: `fetch_paged()` passes `cfg.buyer_postcode` and
  `cfg.search_radius_miles` to `ebay_search()`.

### Problem 3: Shock threshold too aggressive for reliable models
At £3k capital, the 30% shock threshold was blocking Honda Jazz and Toyota Yaris
listings where the worst-case repair is well-understood and low-risk.

### Changes
- **config.py**: New `MODEL_RELIABILITY_TIERS` dict mapping (make, model) → tier.
  Tier 1 (Jazz, Yaris, Auris, Sandero, Swift): +6pp shock allowance.
  Tier 2 (Fiesta, Corsa, Polo, Civic, Fabia): +3pp.
  Tier 3 (Focus, Leon, Astra): no change.
  Tier 4 (Golf, Mini, Juke, Clio, 500): −3pp (stricter).
- **risk.py**: `allowed_shock_threshold()` now accepts optional `make`/`model`.
  New `model_shock_adjustment()` helper. Result clamped to [0.12, 0.45].
  `evaluate_deal()` accepts `make`/`model` and passes to shock threshold.
- **scoring.py**: Passes `guess.make` and `guess.model` to `evaluate_deal()`.

### Shock threshold examples at £3,000 capital:
| Model           | Tier | Threshold |
|-----------------|------|-----------|
| Honda Jazz      | 1    | 0.36      |
| Toyota Yaris    | 1    | 0.36      |
| Ford Fiesta     | 2    | 0.33      |
| Ford Focus      | 3    | 0.30      |
| VW Golf         | 4    | 0.27      |
| Renault Clio    | 4    | 0.27      |
| Unknown         | 3    | 0.30      |

---

## Files Changed (drop-in replacements)

1. `config.py` — version, enrichment, presets, postcode, reliability tiers
2. `risk.py` — model-aware shock threshold
3. `ebay.py` — postcode location bias
4. `scoring.py` — pass make/model to evaluate_deal
5. `pipeline.py` — AVOID split, postcode pass-through
6. `report.py` — collapsed AVOID section

All other files (models.py, db.py, vrm.py, repair.py, etc.) are unchanged.

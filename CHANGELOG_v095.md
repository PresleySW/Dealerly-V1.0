# Dealerly v0.9.5 — Changelog

## VRM + MOT overhaul: from broken to competitive

### Problem
v0.9.4 report showed "no VRM" and a flat 92% p_MOT for virtually every listing.
The VRM extraction cascade was failing silently, and the few "hits" were false
positives (e.g. `1242CC` from engine size, `2013IN` from year fragments). Without
VRMs, DVSA MOT lookups never fired, so p_MOT was always the hardcoded 0.92 default.

### Fix 1: False positive VRM cleanup (vrm.py)

- `_SPEC_PATTERN` extended to 4-digit engine codes: `1242CC` now rejected
- New `_YEAR_PREFIX_PATTERN` catches `2013IN`, `2015FO`, `2010VA` etc.
- New `_DIGIT_SPEC_SUFFIXES` set rejects `149VAN`, `100BHP`, `14TDCI` etc.
- All three patterns integrated into `looks_plausible_uk_vrm()` — eliminates
  the false positives that were polluting the log and triggering bad DVSA lookups.

### Fix 2: Signal-based p_MOT estimation (repair.py + scoring.py)

New `estimate_p_mot_from_signals()` replaces the hardcoded 0.92 default when no
DVSA data is available. Combines four signal sources:

1. **Title keywords**: "12 months MOT" → +6%, "no MOT" → −18%, "spares" → −20%
2. **Vehicle age**: 3yr old → +4%, 15yr old → −6%, 20yr+ → −10%
3. **Mileage**: <40k → +2%, 120k+ → −3%, 160k+ → −10%
4. **Model reliability tier**: Honda Jazz (tier 1) → +4%, Fiat 500 (tier 4) → −5%

Result: p_MOT now ranges from 55% to 96% instead of flat 92% — differentiates
a "fresh MOT, low-mileage Yaris" (94%) from a "spares/repair VW Golf, no MOT"
(55%). Makes the p_MOT column meaningful for decision-making.

### Fix 3: ANPR promoted in enrichment cascade (pipeline.py)

- **ANPR moved from step 5 to step 4.5** — now runs before DVLA, not after.
  For budget eBay listings where sellers rarely type the plate, photos are the
  most likely VRM source. DVLA can then validate the ANPR-found plate.
- Image limit increased from 4 to 6 per listing.
- **New DVLA validation step** (4.7): if ANPR or page scrape found a VRM with
  <90% confidence, DVLA validates it against official records before it's used
  for MOT lookups.

### Fix 4: Page scrape gate removed (pipeline.py)

- Removed the `desc_text < 200` condition that prevented page scraping when the
  eBay API description was longer than 200 chars. The full page HTML often has
  the VRM in structured data, breadcrumbs, or og:tags even when the API-level
  description doesn't include it.

### Fix 5: Report improvements (report.py)

- **p_MOT cell** now colour-coded (green ≥90%, amber ≥80%, red <80%) with
  source badge: "✓ DVSA" for verified, "est." for signal-estimated.
- **New stat cards** in header: "VRMs found" and "DVSA verified" counts show
  enrichment success at a glance.

## Enrichment cascade (new order):

```
Step 1:   Item specifics regex (highest confidence)
Step 2:   Seller description regex
Step 3:   Title regex (safe patterns only)
Step 4:   HTML page scrape (no length gate)         ← FIX 4
Step 4.5: Plate Recognizer ANPR (6 images)          ← FIX 3 (promoted)
Step 4.7: DVLA validation (new: validates ANPR/scrape results)
→ If VRM found: ULEZ inference, DVSA MOT lookup, real p_MOT
→ If no VRM:    signal-based p_MOT from title/age/mileage/tier  ← FIX 2
```

## Files changed (drop-in replacements)

1. **vrm.py** — false positive patterns, plausibility checks
2. **repair.py** — `estimate_p_mot_from_signals()` new function
3. **scoring.py** — wired signal-based p_MOT into scoring pipeline
4. **pipeline.py** — ANPR promotion, page scrape gate removed, DVLA validation
5. **report.py** — p_MOT colour/source badges, VRM/DVSA stat cards
6. **config.py** — version bump to 0.9.5

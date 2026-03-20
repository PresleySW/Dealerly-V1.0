# Dealerly v0.9.4 — Changelog

## Critical fix: Phase 4.5 second enrichment pass

### Problem
Phase 3 enriches the preliminary top-25 from Phase 2 scoring. But after Phase 4
re-scores with MOT + AutoTrader + AVOID split, *different* listings bubble to the
top. The displayed top-15 OFFERs were mostly unenriched — no VRM, no DVSA lookup,
p_mot stuck at the 0.92 default.

### Fix
New Phase 4.5 runs *after* the AVOID split. It identifies actionable top-N listings
that still lack VRMs and weren't in the Phase 3 batch, then enriches up to 10 of
them through the full cascade (regex → DVLA → ANPR).

If Phase 4.5 finds new VRMs, Phase 4.6 immediately re-scores those listings with
MOT enabled, so p_mot updates with real DVSA data.

### Pipeline flow (new):
```
Phase 2 → prelim scoring
Phase 3 → enrich top-25 (as before)
Phase 4 → final score + AVOID split → top_rows
Phase 4.5 → enrich top_rows missing VRMs (NEW)
Phase 4.6 → re-score with MOT if new VRMs found (NEW)
Phase 5+ → offer msgs, analytics, workflow
```

## Scoring calibration: tiered margin + reduced buffers

### Problem
At £3k capital, the old flat 14% margin produced a £400 target. The best OFFER had
£381 profit — £19 short of BUY. Combined with £50 admin + £60 transport buffers,
the model was too conservative for sub-£2500 cars.

### Fix
- `default_target_margin()` is now capital-tiered:
  - Under £5k: 10% (£3k → £300 target)
  - £5k-£10k: 12%
  - Over £10k: 14%
- Admin buffer: £50 → £30 (local pickup with postcode-biased search)
- Transport buffer: £60 → £40

Total overhead saved: £140 per deal. The Fiesta TDCi Van (£381 profit) now clears
the £300 target as a BUY.

## Console output fix

`print_report()` was using HTML entities (`&pound;`, `&ndash;`) which rendered as
literal text in the console. Fixed to plain `£` and `–`.

## Files changed (drop-in replacements)

1. **pipeline.py** — Phase 4.5 + 4.6, enrichment refactored into shared function
2. **config.py** — tiered margin, reduced buffers, version bump
3. **report.py** — console £ fix, AVOID section preserved from v0.9.3
4. **cli.py** — quickstart uses `default_target_margin()` not hardcoded 400
5. **scoring.py** — make/model passthrough (from v0.9.3)
6. **risk.py** — model-aware shock threshold (from v0.9.3)
7. **ebay.py** — postcode location bias (from v0.9.3)

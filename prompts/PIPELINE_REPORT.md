# Pipeline Run: 2026-03-21 17:14
**Mode:** Dealer | **Capital:** £5000
**Platforms Attempted:** Ebay (Success: 62), Pistonheads (Failed: 0 results), Motors (Success: 46), Facebook (Success: 545)

## Metrics
* Candidates Gathered: 653
* VRMs Found: 17 (in top 39 pool)
* Obsidian VRM cache hits: 5
* ANPR calls avoided (verified/cache paths): 1
* ANPR calls made: 6 (skipped budget: 3, verified: 1, cache: 1)
* DVLA calls made: 0 + 4 validations (skipped top-slice: 1)
* AutoTrader scored candidates: phase2 56, phase4 39
* AutoTrader comp cache: 104 hits / 134 lookups (77% hit rate)
* DVSA Verified: 17/39
* Final Decisions: 0 BUY, 6 OFFER, 21 PASS, 8 AVOID
* FB quality: 557/545 good titles, 552 mileage, 563 thumbnails (cap=400)

## Phase Timings
* Phase1: 160.8s
* Phase2: 75.9s
* Phase3: 83.8s
* Phase4: 41.2s

## Errors & Exceptions
* `pistonheads.py` - Platform returned 0 listings. Cloudflare challenge or auth issue likely.

## AI Next Steps Prompt
* Stabilize blocked sources first (pistonheads returned 0 listings) before new feature work.
* Improve VRM hit-rate in the scored pool via stronger labelled-reg extraction and earlier cache reuse before ANPR calls.
* Increase DVSA-verified coverage in top candidates so BUY/OFFER confidence is based on verified MOT history.
* Increase actionable yield by tuning near-miss conversion and Cat-S risk separation to keep true opportunities visible.
* Increase ANPR credit efficiency with stronger early exits and confidence thresholds on low-upside listings.

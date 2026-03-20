# Pipeline Run: 2026-03-20 12:11
**Mode:** Flipper | **Capital:** £3000
**Platforms Attempted:** Ebay (Success: 98), Motors (Success: 32), Facebook (Success: 943)

## Metrics
* Candidates Gathered: 1073
* VRMs Found: 28 (in top 48 pool)
* Obsidian VRM cache hits: 23
* ANPR calls avoided (verified/cache paths): 0
* ANPR calls made: 3 (skipped budget: 24, verified: 0, cache: 4)
* DVLA calls made: 3 + 0 validations (skipped top-slice: 5)
* AutoTrader scored candidates: phase2 96, phase4 48
* DVSA Verified: 27/48
* Final Decisions: 0 BUY, 18 OFFER, 4 PASS, 23 AVOID

## Phase Timings
* Phase1: 287.1s
* Phase2: 148.7s
* Phase3: 52.8s
* Phase4: 23.7s

## Errors & Exceptions
* No critical ingestion errors detected.

## AI Next Steps Prompt
* Improve VRM hit-rate in the scored pool via stronger labelled-reg extraction and earlier cache reuse before ANPR calls.
* Increase ANPR credit efficiency with stronger early exits and confidence thresholds on low-upside listings.

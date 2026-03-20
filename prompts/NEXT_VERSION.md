# Dealerly Next Version Directives

Generated: 2026-03-20 12:11

## Run Snapshot
- Platforms: ebay: 98, facebook: 943, motors: 32
- Timings: phase1 287.1s, phase2 148.7s, phase3 52.8s, phase4 23.7s
- Candidate pool: 48
- VRMs found in pool: 28
- DVSA verified in pool: 27
- Decisions: BUY 0, OFFER 18, PASS 4, AVOID 23

## Build Directives For Next Version
- Improve VRM hit-rate in the scored pool via stronger labelled-reg extraction and earlier cache reuse before ANPR calls.
- Increase ANPR credit efficiency with stronger early exits and confidence thresholds on low-upside listings.
- Validate Facebook listing titles after aria/alt hinting; tune heuristics if marketplace markup shifts.
- Review scoring `reason` strings for clarity now that the HTML report surfaces them on cards.

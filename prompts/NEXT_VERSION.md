# Dealerly Next Version Directives

Generated: 2026-03-21 17:14

## Run Snapshot
- Platforms: ebay: 62, facebook: 545, motors: 46, pistonheads: 0
- Timings: phase1 160.8s, phase2 75.9s, phase3 83.8s, phase4 41.2s
- Candidate pool: 39
- VRMs found in pool: 17
- DVSA verified in pool: 17
- Decisions: BUY 0, OFFER 6, PASS 21, AVOID 8

## Build Directives For Next Version
- Stabilize blocked sources first (pistonheads returned 0 listings) before new feature work.
- Improve VRM hit-rate in the scored pool via stronger labelled-reg extraction and earlier cache reuse before ANPR calls.
- Increase DVSA-verified coverage in top candidates so BUY/OFFER confidence is based on verified MOT history.
- Increase actionable yield by tuning near-miss conversion and Cat-S risk separation to keep true opportunities visible.
- Increase ANPR credit efficiency with stronger early exits and confidence thresholds on low-upside listings.

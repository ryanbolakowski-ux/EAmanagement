"""Scanner V2 (SCANNER-V2) — measured re-rank + fire gates, SHADOW-only.

Built beside V1 (app.engines.scanner.*) per docs/v2/01-scanner-forensics.md:
the V1 composite score is anti-predictive (score>=40 avg -1.20%/pick vs
score<25 avg +1.91%), so V2 rebuilds ranking from the measured feature->outcome
relationships and forward-tests in shadow before any promotion. Imported
lazily — nothing here can touch the live V1 pick path until promoted.
"""

# Scanner V2 Forensics — measured, not guessed (2026-07-01)

Data: email_signals_history, emitted picks (shadow=false), n=44 all-time (41 resolved), plus 130 resolved shadow rows.
Caveat: small samples — treat as directional evidence; every V2 change gets a forward-test gate before promotion.

## 1. The "decline" is real and coincides with the 6/24–6/26 scanner changes
- Before 6/18: 37 picks, 12W/24L, avg +1.32%/pick
- After 6/18: 7 picks, 1W/4L (+2 unresolved), avg −0.25%/pick
- The funnel switch (6/24) + gate loosening (6/26: $1M premkt $-vol hard reject dropped to $250k soft; HH×3 gate demoted to note; overextension 5%→7%) traded pick QUALITY for pick FREQUENCY. The no-pick streak ended — and quality fell.

## 2. The "−20% pick" = CAST (2026-07-01)
- Fired 06:00 ET pre-market, $6.72 microcap, momentum_breakout, score 56 (highest tier).
- Tracked outcome will resolve at the stop (−8.2%); the ticker itself fell ~20% intraday — both are true, different measures. Wide "London low" stop on a thin pre-market pump = oversized loss.

## 3. SMOKING GUN: the composite score is ANTI-predictive
| Score bucket | n | W–L | avg % |
|---|---|---|---|
| <25 | 24 | 9–15 | **+1.91** |
| 25–40 | 10 | 3–7 | +0.87 |
| ≥40 | 7 | 1–6 | **−1.20** |
Higher conviction score → worse result. The ranking system, not the thresholds, is the defect. (CAST 56 → dump; IRDM 53 → −1.6.)

## 4. Feature-level truth (winners n=13 vs losers n=28)
- REL-VOL is the real positive signal: winners avg 57.5x vs losers 21.1x.
- GAP SIZE is mildly NEGATIVE: winners 16.9% vs losers 19.5% — big gaps mean-revert; score treats gap as a linear bonus. Wrong sign at the tail.
- PRICE: under-$10 35% WR / avg +1.76 vs over-$30 0W–4L / −2.93. The edge lives in cheap high-RVOL names; liquid large-caps (AYI, ROKU) all lost. (Tail risk: pump-fades like CAST — control via liquidity/hard stops, not a price floor.)

## 5. Fire-time truth
| Fired | n | WR | avg % |
|---|---|---|---|
| Pre-market (<9:30) | 34 | 39% | +2.00 |
| Open (9:30–10:00) | 6 | 17% | −0.41 |
| Late (>10:00) | 4 | 0% | −4.05 |
- Pre-market fires were historically the BEST bucket — under the old strict $1M liquidity gate. CAST happened after loosening.
- Late-window fires (win extended to 12:00 on 6/26) are 0-for-4 −4.05 avg: "last-chance whatever's best" = the worst picks. HARD-CLOSE the fire window ≈10:00.
- Implication for staged fix B (no fires before 9:35): SAFE for now, but over-broad long-term. V2 gate: allow pre-9:30 fires ONLY with hard premkt liquidity (≥$1M premkt $-vol) + catalyst; block thin premarket microcaps specifically.

## 6. Shadow forward-test now has signal — the WRONG templates are enabled
Top (none enabled): ema_vwap_pullback +2.47 (4W/1L) · relative_strength_vs_index +1.29 (5W/2L) · fvg_vwap_bias +1.25 (4W/2L)
Enabled live: momentum_breakout +0.29 · high_relvol_breakout −1.00 · premarket_gap_continuation −1.34
Confirmed toxic: low_float_squeeze_strict −2.53 (2W/13L)
→ The live pick pool draws from mediocre/negative templates while the best performers sit in shadow (samples still < the 30-gate; keep accruing but reweight the roadmap toward them).

## V2 ranking-system directives (grounded in the above)
1. Rebuild scoring from measured feature→outcome relationships; validate walk-forward, out-of-sample. Rel-vol up-weighted; gap% becomes a capped/penalty curve (not linear bonus); add premkt $-vol quality, float rotation, relative strength vs QQQ/sector, market regime (SPY/QQQ trend + breadth), catalyst class.
2. Fire window: earliest with hard liquidity gate; hard-close ~10:00 ET. No "last-chance" tier.
3. Restore a HARD premarket liquidity floor for any pre-open fire.
4. Template pool: promote by measured shadow expectancy once ≥30 samples; auto-suspend measured-negative templates (low_float_squeeze_strict first candidate).
5. Every change ships as scanner_v2 (shadow, persist-only) beside V1 — promotion only on forward-test outperformance.

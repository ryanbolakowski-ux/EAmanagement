# Strategy V2 — walk-forward optimization (V1 params vs V2 candidate, out-of-sample)

_Generated 2026-07-03 03:40 UTC · window 2026-04-01 → 2026-06-13 · walk-forward split 2026-05-22 02:24 (train head / last 30% OOS) · train ranking: profit factor at optimizer slippage (1 tick/side), min 20 train trades, top 5 advance · OOS showdown: BOTH legs at 2 ticks/side slippage, V2 candidate needs ≥8 OOS trades · grid ≤36 combos (V1 params always included) · workers 3 · data: live candle cache / Polygon futures._

Verdicts: V2_BETTER = the best OOS combo beats the V1 params out-of-sample; V1_HOLDS = no grid combo beat V1 on the holdout; INSUFFICIENT_TRADES = either leg lacked the minimum OOS trades for an honest call. V2 columns are blank when no combo met the trade minimums. Rows append incrementally — a partial file means the run was killed mid-book.

| Strategy | Inst | Verdict | V1 Tr | V1 WR% | V1 PF | V1 DD% | V1 Net$ | V2 Tr | V2 WR% | V2 PF | V2 DD% | V2 Net$ | V2 train Tr | V2 train PF | Param diff |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| FVG Inversion Tap | ES | V2_BETTER | 43 | 34.9 | 2.75 | 2.7 | 20417.50 | 43 | 37.2 | 2.98 | 2.7 | 22907.50 | 91 | 7.09 | fvgmin 4→6 |
| Power of 3 (PO3) | ES | V2_BETTER | 56 | 35.7 | 1.99 | 10.2 | 22805.00 | 56 | 37.5 | 2.17 | 10.0 | 26045.00 | 99 | 8.19 | fvgmin 4→6 |
| ICT 2022 Model (AMD) | ES | V2_BETTER | 52 | 44.2 | 1.64 | 9.7 | 19957.50 | 56 | 37.5 | 2.17 | 10.0 | 26045.00 | 99 | 8.19 | BE 1R@structure→0.5R, fvgmin 4→6 |
| SMT Divergence Reversal | ES | V2_BETTER | 47 | 42.6 | 1.66 | 5.4 | 16382.50 | 46 | 39.1 | 2.85 | 2.6 | 25442.50 | 115 | 5.17 | rr 2→3, BE 1R@structure→0.5R, fvgmin 4→6 |
| Judas Swing | ES | V2_BETTER | 72 | 51.4 | 2.98 | 5.8 | 55585.00 | 73 | 49.3 | 3.93 | 3.5 | 56828.75 | 179 | 5.53 | rr 3→1.5, BE 1R@structure→0.5R, fvgmin 4→6 |
| London Sweep into NY | ES | V2_BETTER | 52 | 44.2 | 1.64 | 9.7 | 19957.50 | 56 | 37.5 | 2.17 | 10.0 | 26045.00 | 99 | 8.19 | BE 1R@structure→0.5R, fvgmin 4→6 |
| IOFED Precision Entry | ES | V2_BETTER | 72 | 51.4 | 2.98 | 5.8 | 55585.00 | 73 | 49.3 | 3.93 | 3.5 | 56828.75 | 179 | 5.53 | rr 4→1.5, BE 1R@structure→0.5R, fvgmin 4→6 |
| NY PM Reversal | ES | V2_BETTER | 72 | 51.4 | 2.75 | 6.0 | 49260.00 | 73 | 49.3 | 3.93 | 3.5 | 56828.75 | 179 | 5.53 | rr 2→1.5, BE 1R@structure→0.5R, fvgmin 4→6 |
| Reversal Swing | ES | V2_BETTER | 52 | 44.2 | 1.64 | 9.7 | 19957.50 | 56 | 37.5 | 2.17 | 10.0 | 26045.00 | 99 | 8.19 | BE 1R@structure→0.5R, fvgmin 4→6 |
| AMD Strategy | ES | V2_BETTER | 52 | 44.2 | 1.64 | 9.7 | 19957.50 | 56 | 37.5 | 2.17 | 10.0 | 26045.00 | 99 | 8.19 | BE 1R@structure→0.5R, fvgmin 4→6 |
| ICT Silver Bullet | ES | V2_BETTER | 72 | 51.4 | 2.98 | 5.8 | 55585.00 | 73 | 49.3 | 3.93 | 3.5 | 56828.75 | 179 | 5.53 | rr 3→1.5, BE 1R@structure→0.5R, fvgmin 4→6 |
| Liquidity Sweep + FVG | ES | V2_BETTER | 47 | 40.4 | 1.63 | 5.5 | 15645.00 | 46 | 39.1 | 2.85 | 2.6 | 25442.50 | 115 | 5.17 | rr 2.5→3, BE 1R@structure→0.5R, fvgmin 4→6 |
| FVG/VWAP Structure Bias (renamed) | NQ | V2_BETTER | 8 | 25.0 | 0.71 | 3.2 | -1490.00 | 8 | 25.0 | 1.47 | 2.2 | 1185.00 | 24 | 6.56 | rr 2.5→3, BE off→0.5R, fvgmin 4→6 |

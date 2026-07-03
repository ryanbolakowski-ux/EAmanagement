# Scanner V2 ("Bellwether") historical replay — 2026-05-19 .. 2026-07-02

Replay of the V2 pipeline (funnel coarse -> score_v2 -> 09:40 ET fire gates ->
structure levels) on historical Polygon data, scored with the prod resolver's
win/loss/expired rules, side-by-side with V1's actual emailed picks.

```
== SUMMARY: V2 replay vs V1 actual (2026-05-19 .. 2026-07-02) ==
V2 replay : trading days=31  picks=19  NO-TRADE days=12  no-data days=2
            W-L-E-O=9-9-1-0  WR=50.0%  expectancy/pick=+1.53%  avg win=+7.76%  avg loss=-4.80%
V1 actual : picks=23 (email_signals_history, shadow=false, deduped)
            W-L-E-O=5-16-2-0  WR=23.8%  expectancy/pick=-0.03%  avg win=+10.78%  avg loss=-3.49%
WR = wins/(wins+losses); expectancy = mean outcome_pct over resolved (win+loss+expired); open picks excluded from both.
```

## Per-day results

| date | V2 pick | score | entry / stop / target | V2 outcome | V2 pct | V1 pick | V1 outcome | V1 pct |
|---|---|---|---|---|---|---|---|---|
| 2026-05-19 | *NO-TRADE* — PAB 67.3: RTH window requires intraday confirmation (above VWAP + continuation) / PCPI 65.2: RTH window requires intrada | — | — | — | — | — | — | — |
| 2026-05-20 | **MWC** (low_float_squeeze_strict) | 78.7 | 13.6 / 12.24 / 16.32 | loss | -10.00% | VIDA | loss | -2.94% |
| 2026-05-21 | *NO-TRADE* — ATPC 73.2: RTH window requires intraday confirmation (above VWAP + continuation) / PCLA 72.8: RTH window requires intrad | — | — | — | — | BDTX | loss | -3.06% |
| 2026-05-22 | **UTI** (low_float_squeeze_strict) | 58.8 | 39.8 / 38.87 / 41.66 | loss | -2.34% | QUBT | loss | -3.01% |
| 2026-05-25 | *NO-DATA* — no grouped-daily data (market holiday or feed gap) | — | — | — | — | — | — | — |
| 2026-05-26 | *NO-TRADE* — EMSC 84.8: RTH window requires intraday confirmation (above VWAP + continuation) / EMEM 81.3: RTH window requires intrad | — | — | — | — | HLIT | win | +10.00% |
| 2026-05-27 | *NO-TRADE* — NHIC 84.1: RTH window requires intraday confirmation (above VWAP + continuation) / VTIX 82.3: RTH window requires intrad | — | — | — | — | AEMD | win | +10.20% |
| 2026-05-28 | **IALT** (vwap_reclaim_hold) | 44.4 | 28.26 / 27.84 / 29.1 | expired | +2.44% | — | — | — |
| 2026-05-29 | **FPS** (momentum_breakout) | 77.3 | 49.55 / 47.6 / 53.45 | win | +7.87% | PUSA | loss | -3.01% |
| 2026-06-01 | *NO-TRADE* — SSAC 71.8: RTH window requires intraday confirmation (above VWAP + continuation) / AIIO 66.5: RTH window requires intrad | — | — | — | — | EEIQ | win | +9.94% |
| 2026-06-02 | **SPPL** (momentum_breakout) | 40.5 | 3.62 / 3.57 / 3.74 | win | +3.31% | AIIO | loss | -3.08% |
| 2026-06-03 | **QURE** (low_float_squeeze_strict) | 51.1 | 27.75 / 27.14 / 28.97 | loss | -2.20% | URG | loss | -2.86% |
| 2026-06-04 | *NO-TRADE* — ROLR 81.7: RTH window requires intraday confirmation (above VWAP + continuation) / EDHL 74.0: RTH window requires intrad | — | — | — | — | — | — | — |
| 2026-06-05 | **MSOS** (bull_flag) | 57.6 | 5.56 / 5.26 / 6.16 | loss | -5.40% | SPRC | loss | -3.03% |
| 2026-06-08 | *NO-TRADE* — ABAT 85.7: RTH window requires intraday confirmation (above VWAP + continuation) / NRIX 84.7: RTH window requires intrad | — | — | — | — | BERZ | loss | -3.00% |
| 2026-06-09 | **PAVS** (vwap_reclaim_hold) | 71.1 | 3.08 / 3.03 / 3.18 | loss | -1.62% | — | — | — |
| 2026-06-10 | **CBRL** (high_relvol_breakout) | 58.8 | 47.58 / 43.11 / 56.51 | loss | -9.39% | VELO | win | +9.98% |
| 2026-06-11 | **LFST** (momentum_breakout) | 34.7 | 7.93 / 7.82 / 8.16 | win | +2.90% | CBRL | loss | -2.99% |
| 2026-06-12 | **AKAN** (momentum_breakout) | 71.7 | 22.83 / 22.11 / 24.27 | win | +6.31% | — | — | — |
| 2026-06-15 | *NO-TRADE* — PAYO 91.4: RTH window requires intraday confirmation (above VWAP + continuation) / NBDS 86.4: RTH window requires intrad | — | — | — | — | ROKU | loss | -3.00% |
| 2026-06-16 | **LNAI** (low_float_squeeze_strict) | 28.4 | 2.66 / 2.5 / 2.98 | win | +12.03% | MBAV | expired | +0.56% |
| 2026-06-17 | *NO-TRADE* — NEOV 88.9: RTH window requires intraday confirmation (above VWAP + continuation) / BFLX 74.8: RTH window requires intrad | — | — | — | — | — | — | — |
| 2026-06-18 | **CRVO** (momentum_breakout) | 59.9 | 5.47 / 5.31 / 5.79 | win | +5.85% | CPAC | expired | +0.67% |
| 2026-06-19 | *NO-DATA* — no grouped-daily data (market holiday or feed gap) | — | — | — | — | NUCL | loss | -3.02% |
| 2026-06-22 | *NO-TRADE* — CRMT 77.7: RTH window requires intraday confirmation (above VWAP + continuation) / MLTX 72.0: RTH window requires intrad | — | — | — | — | — | — | — |
| 2026-06-23 | **VRNS** (momentum_breakout) | 44.1 | 34.09 / 32.93 / 36.41 | win | +6.81% | — | — | — |
| 2026-06-24 | **WEN** (high_relvol_breakout) | 76.8 | 8.83 / 8.35 / 9.79 | loss | -5.44% | — | — | — |
| 2026-06-25 | **IEZ** (ema_vwap_pullback) | 24.2 | 26.92 / 26.52 / 27.72 | loss | -1.49% | — | — | — |
| 2026-06-26 | **CBIO** (momentum_breakout) | 33.1 | 18.0 / 17.04 / 19.92 | loss | -5.33% | AYI, SHPH | loss, win | -4.11%, +13.76% |
| 2026-06-29 | **ASTC** (low_float_squeeze_strict) | 52.7 | 10.25 / 9.23 / 12.29 | win | +19.90% | DMRA | loss | -7.18% |
| 2026-06-30 | *NO-TRADE* — BACC 84.2: RTH window requires intraday confirmation (above VWAP + continuation) / GVH 82.3: RTH window requires intrada | — | — | — | — | IRDM | loss | -1.61% |
| 2026-07-01 | *NO-TRADE* — CANF 74.8: RTH window requires intraday confirmation (above VWAP + continuation) / FSTB 72.1: RTH window requires intrad | — | — | — | — | CAST | loss | -8.18% |
| 2026-07-02 | **RIVN** (relative_strength_vs_index) | 59.6 | 18.23 / 17.79 / 19.11 | win | +4.83% | METU | loss | -1.72% |

## Methodology

- Universe/coarse: Polygon grouped-daily D vs prior trading day, filtered like
  `_fetch_market_snapshot` ($1M day $-vol floor) then `funnel._coarse` per equity
  template; provisional `score_v2` ranks; top 3/template, best 12 unique enriched.
- Enrichment (scan-time honest): 1m bars complete by 09:40 ET only -> price@09:40,
  gap vs prior close, time-of-day-matched rel-vol (cum vol 04:00-09:39 D / same
  window D-1, denominator floored at 2% of D-1 full-day volume), premarket $-vol,
  session VWAP, confirmation proxy = above VWAP + last-3 higher highs (shadow.py's
  proxy), 8-K catalyst from edgar_filings history (36h lookback at 09:40, mirroring
  `_get_8k_catalyst`). QQQ context likewise truncated at 09:40.
- Pick: highest score_v2 with `decide_fire(09:40)` allowed AND valid
  `compute_levels` (template rr/atr params); else NO-TRADE (counted).
- Outcome: 1m bars from 09:40 D through +5 trading sessions; per bar STOP checked
  before TARGET (prod resolver ordering); neither -> EXPIRED at session-5 close;
  insufficient future data -> OPEN (excluded from WR/expectancy).
- V1 comparison: actual emailed picks (email_signals_history, shadow=false),
  deduped per (day, ticker) — duplicate re-sends of the same pick collapse to one
  — with their prod-resolver outcomes.

## Limitations (read before quoting the numbers)

1. **Coarse-stage lookahead**: the stage-1 universe uses day D's FULL grouped row
   (close + full-day volume). A name that only qualified via afternoon action can
   enter the candidate set. All scoring/confirmation/levels use <=09:39 data, but
   the candidate *set* has residual lookahead.
2. **Live V2 sees different data**: prod's Polygon key is delayed-tier; the live
   scan's grouped 'day' is actually D-1 (prev = D-2). This replay assumes same-day
   pre-open universe knowledge — i.e. it measures 'V2 with a real-time snapshot
   feed', not V2 exactly as deployed on the delayed key.
3. **Rel-vol scale**: score_v2 was calibrated on full-day grouped rel-vol; the
   replay feeds a 04:00-09:39 time-matched ratio (scan-time knowable). Log scaling
   absorbs most of the difference but values can saturate when the prior-day
   premarket was dead (denominator floor).
4. **Session $-vol under-scaled**: liquidity_quality's session component sees the
   09:40 cumulative, not the full-day $-vol it was scaled for (premkt $-vol, 60%
   of the blend, is unaffected).
5. **Single decision point**: one pick/day at 09:40 (RTH window). Premarket-window
   fires (06:00-09:29) are NOT replayed, though live V2 could fire there.
6. **Outcome basis**: 1m UNADJUSTED bars (matches unadjusted entries) vs the
   resolver's adjusted daily bars; same-bar stop/target ambiguity resolved
   conservatively (stop first). Corporate actions inside the 5-day window would
   misprice (none observed in this window).
7. **Sample size**: ~1 month of trading days; wide confidence intervals — a
   handful of outcomes moves WR by several points.
8. **V1 rows scored by prod**: V1 stats inherit whatever biases the prod resolver
   has (e.g. daily-bar stop-first ordering). 2026-06-19 was a market holiday with
   no grouped data, yet V1 recorded a (delayed-key artifact) pick — kept in V1
   stats, shown as NO-DATA for V2.
9. **Catalyst feature is dead in prod**: every row of `edgar_filings` (8,352 rows
   since 2026-05-12) has an EMPTY ticker column, so `_get_8k_catalyst` can never
   match — catalyst_weight was 1.0 ('no catalyst') for every V1 pick AND every
   candidate in this replay. Faithful to live, but it means (a) the V2 catalyst
   component contributed a flat 2.8/100 to everyone, and (b) V2's premarket fire
   window (requires liquidity AND catalyst) can never open until this is fixed —
   flagged separately for repair.

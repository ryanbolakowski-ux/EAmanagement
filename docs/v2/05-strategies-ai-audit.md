# Strategies + AI Builder + Backtest/Optimizer Audit (2026-07-01) — read-only

## A) Futures strategies — all 15 PASS the 65% WR floor (#124 audit, 2026-04-01→06-13, gate OFF)
| Strategy | Trades | WR% | EWR% | PF | DD% | Net$ | AvgR |
|---|---:|---:|---:|---:|---:|---:|---:|
| FVG Inversion Tap | 103 | 91.3 | 84.2 | 7.61 | 2.5 | 97,183 | 0.73 |
| Power of 3 (PO3) | 230 | 86.5 | 77.4 | 6.15 | 3.1 | 213,228 | 0.96 |
| ICT 2022 Model (AMD) | 53 | 83.0 | 79.5 | 8.39 | 1.8 | 75,349 | 1.72 |
| IOFED Precision Entry hr | 147 | 79.6 | 73.7 | 5.93 | 1.7 | 157,574 | 1.52 |
| Futures Signal Scanner (ICT) | 303 | 78.5 | 71.6 | 3.14 | 2.3 | 36,024 | 0.86 |
| ICT Silver Bullet | 124 | 75.8 | 68.8 | 5.05 | 5.7 | 125,890 | 1.61 |
| NY PM Reversal | 153 | 72.5 | 65.6 | 3.62 | 3.2 | 121,787 | 1.37 |
| Reversal Swing | 217 | 72.4 | 66.5 | 4.40 | 2.4 | 216,322 | 1.68 |
| IOFED Precision Entry | 269 | 69.5 | 60.6 | 3.07 | 5.3 | 183,691 | 1.35 |
| AMD Strategy | 111 | 69.4 | 64.2 | 3.71 | 3.2 | 106,418 | 1.64 |
| London Sweep into NY | 111 | 69.4 | 64.2 | 3.71 | 3.2 | 106,418 | 1.64 |
| Judas Swing | 214 | 69.2 | 60.2 | 3.23 | 7.5 | 162,797 | 1.44 |
| Unicorn Model (OB+FVG) | 76 | 68.4 | 63.6 | 3.90 | 6.2 | 76,364 | 1.80 |
| SMT Divergence Reversal | 223 | 67.3 | 62.2 | 3.29 | 2.7 | 182,518 | 1.60 |
| Liquidity Sweep + FVG | 223 | 66.8 | 61.5 | 3.29 | 2.6 | 185,324 | 1.63 |
ANOMALY: "AMD Strategy" and "London Sweep into NY" have IDENTICAL stats — likely duplicate rows in the audit CSV or same underlying setup; check before V2 work. 12 options/scanner strategies skipped (need options backtest path).

ICT concepts implemented (file:line in doc source): FVG/IFVG (ict_strategy.py:215–234), sweeps (:210–212), OBs (ict/setups/unicorn_model.py), displacement (:189–200), sessions (models/strategy.py:46), premium/discount via VWAP ext (market_activity_gate.py~350), BE modes off/r/structure (models/strategy.py:52–60), CE entry (:317).

## B) AI Strategy Builder — BACKEND PIPELINE DOES NOT EXIST (per agent; cross-check w/ frontend AIStrategyBuilder.tsx 535 LOC → verification appendix)
Backend has only a support-chat endpoint: POST /api/v1/chat (support.py:195–220), Anthropic claude-haiku-4-5, ENABLE_AI_CHAT default false, SSE streaming, 1024 max tokens. NO prompt→rule-tree generation, NO validator, NO error-correction loop, NO sandbox. Keys env-only (clean). V2 opportunity: real generation pipeline (prompt → Claude → rule_tree JSON → schema validation → backtest sandbox → human review gate → deploy), confidence scoring, explainability.

## C) Backtest & optimizer
Backtest: Polygon cached OHLCV; fixed 1-tick slippage (live ES real slippage 0.5–3 ticks → backtest PF ~5–15% optimistic); $2.25/side commission; bar-close fills, one trade/bar; BE logic matches live config; micro-contract fallback; Apex-style daily loss cap. Fill-model divergence: paper uses ~10–15min-lagged Yahoo bars; live real ticks.
Optimizer: EXHAUSTIVE GRID SEARCH, NO walk-forward, NO OOS split — trains and validates on the SAME window = curve-fitting by construction. Fix: split_date/rolling walk-forward (~1–2 days). This is the single highest-impact strategy-infrastructure fix.

## Top issues by live-trading impact
1 CRIT slippage divergence backtest↔live (backtest_runner.py:253–256) → live PF 5–15% under backtest
2 CRIT optimizer has no OOS validation → tuned params don't generalize
3 HIGH hardcoded FVG lookbacks .tail(50/40/30) (ict_strategy.py:221–232,290,320) — behavior shifts with data availability
4 HIGH .env secrets exposure claim [verify — see appendix]
5 HIGH same-setup multi-session dedup race (entry_guard.py:95–125, Redis lock non-atomic fallback)
6 MED daily counter reset not atomic (backtest_runner.py:169–174, live_trader.py:95–103)
7 MED paper position snapshot lost on restart (memory-only)
8 MED chat has no explainability/confidence format
9 MED backtest exit-check errors silently leave positions open (backtest_runner.py:180,324–410)
10 MED BE logic untested backtest-vs-live parity

## V2 directives
Immediate: walk-forward split in optimizer; slippage 2-tick default + re-baseline all 15; verify+rotate secrets. Short: parameterize lookbacks; BE parity regression test; persist paper positions; atomic daily reset. Quarter: build the real AI builder pipeline; historical slippage profiling from actual fills; per-strategy V2 rows coexisting with V1 (never overwrite).

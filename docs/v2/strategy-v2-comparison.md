# Strategy V2 — execution-assumption comparison (V1 vs V2)

_Generated 2026-07-02 03:24 UTC · window 2026-04-01 → 2026-05-01 · V1 slippage 1 tick(s)/side vs V2 slippage 2 tick(s)/side · OOS fraction 0.3 (OOS starts 2026-04-22 00:00) · data: synthetic deterministic bars (seed 42)._

Both legs run identical strategy logic on identical bars — the delta columns isolate the cost of the V2 execution assumptions. Deltas are V2 − V1; blank delta = one side non-finite (e.g. PF=inf on a no-loss segment).

| Strategy | Inst | Segment | Trades V1 | Trades V2 | ΔTr | WR% V1 | WR% V2 | ΔWR | PF V1 | PF V2 | ΔPF | DD% V1 | DD% V2 | ΔDD | Net$ V1 | Net$ V2 | ΔNet$ | avgR V1 | avgR V2 | ΔavgR |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Synthetic Pulse Fast | ES | full | 1807 | 1807 | 0 | 20.4 | 20.4 | 0.0 | 0.34 | 0.24 | -0.10 | 1109.3 | 1350.3 | 240.9 | -1112458.50 | -1350252.00 | -237793.50 | 1.32 | 0.94 | -0.37 |
| Synthetic Pulse Fast | ES | in_sample | 1266 | 1266 | 0 | 20.1 | 20.1 | 0.0 | 0.33 | 0.24 | -0.09 | 784.9 | 952.8 | 167.9 | -787023.00 | -952776.00 | -165753.00 | 1.32 | 0.94 | -0.37 |
| Synthetic Pulse Fast | ES | out_of_sample | 541 | 541 | 0 | 20.9 | 20.9 | 0.0 | 0.35 | 0.25 | -0.10 | 325.4 | 397.5 | 72.0 | -325435.50 | -397476.00 | -72040.50 | 1.32 | 0.94 | -0.37 |
| Synthetic Pulse Mid | ES | full | 963 | 963 | 0 | 22.8 | 22.8 | 0.0 | 0.57 | 0.46 | -0.11 | 337.6 | 485.3 | 147.7 | -344151.00 | -488601.00 | -144450.00 | 1.92 | 1.57 | -0.36 |
| Synthetic Pulse Mid | ES | in_sample | 673 | 673 | 0 | 23.0 | 23.0 | 0.0 | 0.58 | 0.47 | -0.11 | 234.9 | 337.4 | 102.5 | -236571.00 | -337521.00 | -100950.00 | 1.92 | 1.57 | -0.36 |
| Synthetic Pulse Mid | ES | out_of_sample | 290 | 290 | 0 | 22.4 | 22.4 | 0.0 | 0.56 | 0.45 | -0.10 | 106.9 | 148.0 | 41.1 | -107580.00 | -151080.00 | -43500.00 | 1.92 | 1.57 | -0.36 |
| Synthetic Pulse Slow | NQ | full | 565 | 565 | 0 | 20.0 | 20.0 | 0.0 | 0.60 | 0.52 | -0.08 | 173.4 | 229.9 | 56.5 | -172325.00 | -228825.00 | -56500.00 | 2.39 | 2.06 | -0.32 |
| Synthetic Pulse Slow | NQ | in_sample | 398 | 398 | 0 | 20.9 | 20.9 | 0.0 | 0.63 | 0.54 | -0.09 | 114.9 | 152.8 | 37.9 | -110510.00 | -150310.00 | -39800.00 | 2.39 | 2.06 | -0.32 |
| Synthetic Pulse Slow | NQ | out_of_sample | 167 | 167 | 0 | 18.0 | 18.0 | 0.0 | 0.52 | 0.45 | -0.07 | 63.4 | 79.6 | 16.2 | -61815.00 | -78515.00 | -16700.00 | 2.39 | 2.06 | -0.32 |

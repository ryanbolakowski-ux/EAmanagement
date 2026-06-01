# Live Trading — Dark-Mode Contrast Fixes (2026-06-01)

Branch: `fix/livetrading-darkmode-2026-06-01`
Page: `/app/live` (frontend/src/pages/LiveTradingV2.tsx)
Component: `frontend/src/components/TradeMetrics.tsx`

This is **not a runtime test** — it's a manual-verification checklist
to spot-check after deploy.

## What was fixed

### 1) Trade-history table (the originally-reported bug)
The Entry / Exit / Qty columns had `<td>` with NO text-color class, so they
inherited the body text (slate-900 in light, slate-100 in dark — but with
the table cell on a dark slate-900 panel, the inherited light-mode dark text
was used inside the dark panel, hence "dark-on-dark"). Fixed by adding
`text-slate-900 dark:text-slate-100` to every value cell.

| Cell      | Before                                                | After                                                                              |
|-----------|-------------------------------------------------------|------------------------------------------------------------------------------------|
| Entry $   | `px-2 py-2 text-right tabular-nums`                   | `px-2 py-2 text-right tabular-nums text-slate-900 dark:text-slate-100`             |
| Exit $    | `px-2 py-2 text-right tabular-nums`                   | `px-2 py-2 text-right tabular-nums text-slate-900 dark:text-slate-100`             |
| Qty       | `px-2 py-2 text-right tabular-nums`                   | `px-2 py-2 text-right tabular-nums text-slate-900 dark:text-slate-100`             |

(P&L cell was already correct — uses `pnlColor()` which has explicit dark variants.)

### 2) Open-positions table (same Qty / Entry omission)
| Cell      | Before                                                | After                                                                              |
|-----------|-------------------------------------------------------|------------------------------------------------------------------------------------|
| Qty       | `px-2 py-2 text-right tabular-nums`                   | `+ text-slate-900 dark:text-slate-100`                                             |
| Entry $   | `px-2 py-2 text-right tabular-nums`                   | `+ text-slate-900 dark:text-slate-100`                                             |

### 3) PendingOrdersCard pill + row text
| Element            | Before                                                | After                                                                              |
|--------------------|-------------------------------------------------------|------------------------------------------------------------------------------------|
| BUY/SELL side pill | `bg-rose-100 text-rose-700` (and emerald twin)        | `+ dark:bg-rose-900/40 dark:text-rose-300` (and emerald twin)                      |
| Symbol             | `font-bold`                                           | `font-bold text-slate-900 dark:text-slate-100`                                     |
| Qty "X sh"         | `text-slate-500`                                      | `text-slate-500 dark:text-slate-400`                                               |
| Cancel button      | `text-rose-600`                                       | `text-rose-600 dark:text-rose-400`                                                 |
| Loading state      | `text-slate-400`                                      | `text-slate-400 dark:text-slate-500`                                               |

### 4) TradeMetrics MetricsGrid (8 cards: Net P&L, Win Rate, Profit Factor, Max DD, Avg Win/Loss, Largest Win/Loss)
Color classes upgraded with brighter dark-mode variants:
- `text-green-600` → `+ dark:text-green-400`
- `text-red-500`   → `+ dark:text-red-400`
- `text-amber-600` → `+ dark:text-amber-400`

### 5) TradeMetrics EquityCurve tooltip
Hard-coded white tooltip with default (black) text was unreadable on the dark
page. Switched to slate-950 bg with light text — works in both modes (recharts
inline-style; no theme switching available without a wrapper).

### 6) TradeMetrics TradeTable (used on /app/live/<account-id>)
- pnlColor classes for table rows now have dark-variant equivalents.
- Removed a duplicate `dark:hover:bg-slate-800` (legit typo, both 800/50 and 800 present).

## What was already correct (audit confirmed)

- `PortfolioHeader` — entirely on a dark-violet gradient with `text-white`,
  `text-slate-300/400/500`, and explicit `text-emerald-400`/`text-rose-400`
  for P&L. Confirmed all values readable in both modes.
- `BrokerCard` — equity, buying-power, account-name, broker label, account-type
  chip, status dot all have explicit `dark:text-slate-100/200/300/400` pairs.
- `SizingPreviewCard` — input fields, labels, dropdown values, per-account
  summary all have `bg-white dark:bg-slate-900`, `border-slate-300 dark:border-slate-600`,
  `text-slate-900 dark:text-slate-100` triads.
- `SizingModal` — already fully dark-aware (300+ lines, all classes paired).
- `TodayPickCard` / `OpenPositionsCard` (the dark-gradient ones) — `text-white`
  with `opacity-` modifiers and saturated `text-emerald-300`/`text-rose-300`.
  These were intentionally translucent-on-dark.
- Deploy-strategy modal — labels, selects, error banners, alternatives all
  have dark counterparts.
- `MiniStat` / `StatsStrip` (header metric cards above trade-history table:
  TOTAL TRADES, WIN RATE, PROFIT FACTOR, AVG WIN, AVG LOSS, EXPECTANCY,
  TOTAL P&L) — all values already use `text-slate-900 dark:text-slate-100`
  with conditional brand-color overrides (`text-emerald-600 dark:text-emerald-400`).
- `bg-white/5`, `bg-white/10`, `bg-white/20` overlays on dark gradient
  panels — intentionally translucent, left alone.

## Spot-check after deploy

1. Toggle to dark mode (system or app theme).
2. Navigate to `/app/live`.
3. Scroll to the **Trade history** table (bottom of page, after switching
   to the HISTORY tab if PENDING is open).
4. ✓ ENTRY, EXIT, QTY columns should be **bright white** (slate-100), not dark grey.
5. ✓ P&L column should be saturated emerald/rose.
6. ✓ TICKER (left) and REASON (right) columns readable.
7. Toggle to OPEN-POSITIONS tab → ✓ Qty + Entry now readable too.
8. (If there are pending orders) → ✓ Pending-orders card row text readable, BUY/SELL pill has dark-mode background.
9. Open the Deploy-Strategy modal → ✓ All labels, selects, warning callouts readable.
10. Open any broker-card Sizing modal → ✓ All inputs + risk preview readable.
11. Toggle back to light mode → ✓ Everything still looks right (no regressions).

## Not changed (out of scope this pass)

- `LiveTrading.tsx` (the classic `/app/live/classic` page) — same patterns
  exist; secondary priority. Recommend a follow-up patch if user still uses it.
- `LiveAccountDetail.tsx` (/app/live/<account-id>) — picked up the
  TradeMetrics improvements transitively but not independently audited.

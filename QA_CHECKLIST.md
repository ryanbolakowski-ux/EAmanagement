# Theta Algos — QA Checklist

Run after every backend deploy (container restart) and every frontend (Vercel) deploy.

## Automated (backend) — `docker exec -w /app edge_backend python -m pytest tests/ -q`
- [ ] All tests green (strategy status/publish, watcher-draft rule, signal idempotency,
      geometry validation, optimization start/poll/results, open-state 200s).

## Strategy lifecycle (Bug 1)
- [ ] Create strategy with `status:"active"` → returns `active` (not draft).
- [ ] Create with no status → defaults `active`.
- [ ] Create with `status:"draft"` → stays `draft`.
- [ ] `POST /strategies/{id}/activate` flips draft→active and persists (re-GET confirms).
- [ ] `POST /strategies/{id}/deactivate` flips active→draft.
- [ ] Invalid status → 422 with a clear message.

## Watchers on draft strategies (Bug 2)
- [ ] `POST /account-signals/watchers` for a DRAFT strategy → 409 "activate first".
- [ ] Same after activation → 201.
- [ ] A watcher whose strategy is draft does NOT emit signals (loop pauses, auto-resumes on activate).

## Email/Account signals naming (Bug 3)
- [ ] `/api/v1/account-signals/*` and `/api/v1/email-signals/*` both resolve (no 404).
- [ ] UI nav, dashboard card, and page header all say "Email Signals".

## Duplicate suppression (Bug 4)
- [ ] Re-firing the same setup (same watcher/strategy/instrument/direction/bar/entry/stop/tp)
      within `SIGNAL_DUP_COOLDOWN_MIN` (default 15m) creates ONE row.
- [ ] `duplicate_suppressed_count` increments; `duplicate_suppressed_at` set.

## Delivery tracking (Bug 5)
- [ ] New signal rows have `detected_at`, `queued_at`, and (on send) `provider_sent_at`,
      `provider_message_id`, `provider_status`, `latency_seconds`.
- [ ] Failed sends set `status='failed'` + `error_message`; suppressed set `status='suppressed'`.

## Signal validation (Bug 6)
- [ ] Long with stop>=entry or tp<=entry → rejected, no row, no email.
- [ ] Short with tp>=entry or stop<=entry → rejected.
- [ ] Tight ES stop (<2pt) logs a warning but still sends.

## Broken endpoints (Bug 7)
- [ ] `GET /trades/open-positions` with no broker/positions → 200 `[]` (not 500).
- [ ] `GET /backtests/{id}/trades` for a completed backtest → 200 (not 500).

## Optimization (Bug 8)
- [ ] `POST /optimization/` with valid grid → 202 queued; bad grid → 400 with message.
- [ ] `GET /optimization/{id}` → 200 with status, progress, completed/total, started_at,
      completed_at, error_message (not 405).
- [ ] A real small run reaches `completed` (or `failed` WITH a failure_reason) — never silent.
- [ ] Status is lowercase: queued/running/completed/failed.
- [ ] Apply-best-result updates the strategy params.

## Metrics & ranking (Bug 9 / 11)
- [ ] Backtest metrics include `expectancy` (= net_profit/total_trades), avg_win, avg_loss.
- [ ] `GET /backtests/ranking?sort_by=profit_factor|win_rate|max_drawdown|total_trades|expectancy`.
- [ ] `quality.is_good` only true when PF>=1.3, WR>=40%, |DD|<=25%, trades>=30, expectancy>0.
- [ ] `quality.small_sample` true for <30 trades (UI must show a sample-size warning).

## UI audit (Bug 10) — after Vercel deploy
- [ ] Each route shows a unique browser tab title.
- [ ] Dashboard paper P&L/trades match Paper Trading + Profile (API `/dashboard/summary` is correct).
- [ ] Dashboard bias cards render (no permanent "loading…"; `/dashboard/bias` returns 200).
- [ ] `/app/live` header shows the same user as other pages.
- [ ] `/app/live` open-positions failure shows a friendly fallback, not "0 open positions".
- [ ] Strategy descriptions render arrows (→) correctly (no `â†'` mojibake).

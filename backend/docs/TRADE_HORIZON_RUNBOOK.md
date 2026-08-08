# Trade Horizon (Day/Swing) — operator runbook

**TRADE-HORIZON-V1** (2026-08-08). Every entry email now carries an explicit
trade-type label so the recipient knows what to do into the close:

| Horizon | Pill | Action line |
|---|---|---|
| `day` (default) | **DAY TRADE** (amber) | "This is an intraday setup — the bot exits by 3:55 PM ET today (auto-close at 15:55 ET, 5 minutes before the bell). If you mirror it manually, be flat before the close." |
| `swing` | **SWING TRADE** (indigo) | "This is a swing setup — holding overnight (and over weekends) is expected. The bot manages the exit." |

Subjects get ` · Day Trade` / ` · Swing Trade` **appended** after the existing
whitelisted prefix (killswitch check is substring-anywhere, so this is safe).

## Where the label comes from

- **Saro daily stock pick** (`emit_theta_pick`): **hard-coded Day** — the
  platform force-closes stock picks at 15:55 ET unconditionally. No column can
  override it. Watch-only picks get **no** pill/action line (they are not
  trades) and no subject suffix.
- **Futures watcher signal emails** (`send_signal_email`): per-strategy —
  `strategies.trade_horizon` (`'day'`/`'swing'`, NULL = day), looked up in
  `runner._emit_signal` before the send.
- **Trade receipts** (`send_trade_receipt_email`): defaults to Day; the paper
  trader resolves the strategy's column by `strategy_id`.

Resolution helper: `app/services/trade_horizon.py::get_trade_horizon`
(stock_pick source → always `day`; otherwise only an explicit `'swing'`
returns swing).

## Flip a strategy to Swing (until the strategy editor grows a control)

The column is added lazily (`ADD COLUMN IF NOT EXISTS`) on first use; the
ALTER below is a harmless no-op if it already ran.

```sql
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS trade_horizon VARCHAR(8);
UPDATE strategies SET trade_horizon = 'swing' WHERE id = '<strategy-uuid>';
```

Back to Day:

```sql
UPDATE strategies SET trade_horizon = NULL WHERE id = '<strategy-uuid>';  -- NULL = day
```

Check what's set:

```sql
SELECT id, name, trade_horizon FROM strategies WHERE trade_horizon IS NOT NULL;
```

API alternative (no UI yet): `PUT /api/strategies/{id}` accepts an optional
`"trade_horizon": "swing"` field. Omitting the field leaves the stored value
untouched (the current frontend never sends it, so edits can't clobber SQL-set
values). Responses do **not** expose the column yet (it is a deferred ORM
column; read it via SQL).

## Out of scope / known follow-ups

- `send_consolidated_signals_email` and `send_pending_trade_confirm_email`
  also carry entry setups but have no horizon label yet — follow-up needed for
  Ryan's rule to cover every entry-like surface.
- Admin heartbeat test (`POST /api/admin/send-test-trade-email`) renders the
  SWING pill for the `futures` sample and DAY for `stock`/`options`, so both
  variants can be eyeballed end-to-end.

# Risk Calculator — Manual Verification Checklist

A hand-driven spec to spot-check the Live Trading "Risk Calculator · Trade
Sizing" card after deploy. Mirrors `backend/tests/test_allocation_sizing.py`
plus the bits that pure tests can't cover (live-typing UX, persistence,
staleness color, refresh button).

Pre-flight: at least one active broker account linked, hard-refresh
(Cmd-Shift-R) the Live Trading page after deploy so the new bundle loads.

---

## 1. Allocation math (the headline use case)

- [ ] **$1k → 6 shares of a $150 stock.** Set ticker `NVDA`, entry `150`,
      stop `147`, Allocation `1000`. Account card shows: "Buy 6 shares of
      NVDA for ~$900 (allocation: $1,000). Risk: $18 if stop hits at $147.00."
      `Risk model` chip reads `$1,000 allocation`.
- [ ] **$10k → 66 shares of the same stock.** Bump Allocation to `10000`.
      Card live-updates (within ~300ms) to "Buy 66 shares of NVDA for
      ~$9,900 (allocation: $10,000)." No need to click anything.
- [ ] **Live update on every digit.** Type `1`, `10`, `100`, `1000` — each
      keystroke kicks a fresh request and the share count updates. (Brief
      "refreshing..." note appears top-right of the card.)
- [ ] **Empty Allocation falls back to risk-based.** Clear the Allocation
      field. The chip swaps to `1% per trade (default)` (or whatever the
      account's risk_per_trade_* is) and shares re-compute from risk.

## 2. Cash vs margin constraints

- [ ] **Cash account, $25k allocation > $20k cash → red banner-equivalent
      cap message.** Pick a cash account (top strip shows `CASH`). Set
      entry `50`, stop `48`, Allocation `25000`. Card shows 400 shares
      (= 20000 // 50), capped by `cash`. The "Capped by:" amber line reads
      `Capped by: cash`. `Cash available` stat shows the $20k limit.
- [ ] **Margin account uses BP, not cash.** Switch the Account selector to
      a margin account (top strip `MARGIN`). Set entry `100`, stop `97`,
      Allocation `80000`. Card shows 600 shares, capped by `buying_power`.
      The bottom stats labeled `Buying power`, not `Cash available`.

## 3. Top strip + Refresh balance

- [ ] **Strip shows Account type / Cash / BP / Equity / Last synced** for
      the selected account. All five labels render; values render in
      tabular-nums.
- [ ] **Fresh sync → green.** Click `Refresh balance`. Spinner shows
      briefly; `Last synced` flips to "just now" in emerald-green.
- [ ] **5-30 min old → amber.** (Time-travel: wait 5+ min, or temporarily
      set `cached_balance_at` to `now() - interval '7 minutes'` via psql.)
      `Last synced` shows in amber.
- [ ] **>30 min old → red.** Same trick but `interval '35 minutes'`.
      `Last synced` shows in rose-red.

## 4. Validation banner (stale / missing)

- [ ] **Stale balance → red banner on every account card + Deploy disabled.**
      Force `cached_balance_at` to `now() - interval '10 minutes'`. Card
      shows a rose-bordered banner: "Account data is 10 minutes stale.
      Click 'Refresh balance' before placing trades." Deploy button is
      greyed out and tooltip shows the same text.
- [ ] **Missing balance → "Account info missing. Sync account first."**
      Force `cached_equity = NULL`. Banner appears; Deploy disabled.
- [ ] **Click Refresh balance → banner clears** after the sync finishes
      and the new query result loads.

## 5. Account selector

- [ ] **Defaults to the first account.** On first load, top strip
      populates from accounts[0]. Selector dropdown lists every active
      account with `broker — name (sandbox)?`.
- [ ] **Switching accounts updates the top strip, the per-card validation,
      and re-fetches sizing.** No stale data from the previous account
      bleeds through.

## 6. Saro allocation persistence

- [ ] **Set $1k + check "Save as default" → next-day pre-fill.** With the
      cash account selected, type `1000`, check the box. ~600ms later the
      PATCH fires (Network tab). Reload the page; Allocation pre-fills to
      `1000` for that account.
- [ ] **Change to $10k → default updates.** With box still checked, edit
      to `10000`. Reload — Allocation pre-fills to `10000`.
- [ ] **Uncheck "Save as default" → clears the saved default.** Uncheck
      the box (the input keeps showing $10k for the current session, but
      the PATCH sends `null`). Reload — Allocation input is empty again.
- [ ] **Per-account scoped.** Saving $1k on account A does NOT change
      account B's saved default. Switch selector to verify.

## 7. Edge cases (shouldn't crash)

- [ ] Allocation `0` or negative → input rejected (red ring + "Enter a
      ticker, entry, and stop…" notice).
- [ ] Ticker blank or entry ≤ 0 → notice replaces the per-account grid;
      no API spam.
- [ ] Entry == stop (e.g. both `150`) → per-account card shows "Entry and
      stop are equal — define a stop to size the trade." Allocation
      mode still computes shares correctly when entry/stop differ but
      doesn't need risk-math; only the informational `actual_dollar_risk`
      stays $0 in that degenerate case.

## 8. Smoke that nothing else broke

- [ ] Dark mode contrast is fine on every new element (strip, banner,
      Deploy button disabled state, allocation input border).
- [ ] Old risk-mode cards (PercentDollar) still work elsewhere on the
      page (Sizing modal etc.).
- [ ] No 500s from `GET /accounts/{id}/sizing` or the new POST
      `/sizing-preview` in the network tab.

---

If all boxes are green, the Risk Calculator is shipped. File any failure
in a follow-up issue and link the screenshot.

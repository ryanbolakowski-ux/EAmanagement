# Strategy-selector label cleanup — manual verification checklist

Verifies the strategy-selector dropdowns across Paper Trading and Live Trading
show **clean strategy names**, flag running strategies with **· Active 🟢**, and
render unavailable strategies as **disabled options with a reason** — instead of
the old cluttered `Name (paused)` / `Name (draft)` labels.

Branch: `feat/finalize-pipeline-2026-06-09`

## General rules (apply to every selector below)
- [ ] Main selectable option label = **clean strategy name only** (no `(paused)`,
      no `(draft)` suffix).
- [ ] A strategy with a **currently running session** shows `Name · Active 🟢`.
- [ ] A strategy that is **not deployable** for the current context is a
      **disabled `<option>`** with a short reason in the label *and* a `title=`
      tooltip (not silently dropped).
- [ ] Asset-class + status FILTERING is unchanged — only the display label changed.

---

## 1. Paper Trading — `frontend/src/pages/PaperTrading.tsx`

### 1a. Options paper panel selector (`OptionsPaperPanel`, "Start an options session")
- [ ] Open Paper Trading → Options tab.
- [ ] Each of "your strategies" shows the **clean name** (a paused/draft strategy
      no longer reads `... (paused)` / `... (draft)`).
- [ ] Start an options-paper session for a strategy, then reopen the dropdown:
      that strategy now reads `Name · Active 🟢`.
- [ ] Built-in `🎯 Saro (daily pick)` option still present and selectable.

### 1b. Futures paper modal selector ("Start Paper Session" → Strategy)
- [ ] Click **Start Session**.
- [ ] Each futures strategy shows the **clean name** (no status suffix), incl.
      draft/paused ones (paper trading still allows draft/paused — filter intact).
- [ ] Start a futures paper session, reopen the modal: that strategy reads
      `Name · Active 🟢`.
- [ ] When no futures strategies exist: disabled "No futures strategies — create
      one on the Strategies page" option still shown.

(There is no separate Stock paper selector on this page — only Futures + Options
tabs exist.)

---

## 2. Live Trading (classic) — `frontend/src/pages/LiveTrading.tsx`

### Deploy Strategy modal selector
- [ ] Click **Deploy Strategy**, pick a broker account.
- [ ] Active strategies appear under their asset-class optgroup with **clean names**.
- [ ] A strategy with a running live session reads `Name · Active 🟢`.
- [ ] A **draft/paused** strategy whose asset class the broker supports appears
      under a **"Not deployable (activate first)"** optgroup as a **disabled**
      option labelled `Name — draft, activate first` / `Name — paused, activate
      first`, with a `title=` tooltip explaining it must be set to Active.
- [ ] Built-in Saro still listed under Stocks for stock-capable brokers.
- [ ] Before a broker is chosen: "Select a broker account first…" message shown.

---

## 3. Live Trading V2 — `frontend/src/pages/LiveTradingV2.tsx`

### Deploy form strategy selector (`byClass` / `<optgroup>` block)
- [ ] Click **Deploy strategy**, pick a broker account, pick the asset-type tab
      (Futures / Options / Stocks).
- [ ] Active strategies for the selected tab show **clean names** in the
      `⚡/🎯/📈` optgroup.
- [ ] A strategy with a running live session reads `Name · Active 🟢`.
- [ ] A **draft/paused** strategy matching the selected tab's asset class appears
      under a **"Not deployable (activate first)"** optgroup as a **disabled**
      option with the reason in the label + `title=` tooltip.
- [ ] Saro still injected into the Stocks tab for stock-capable brokers.
- [ ] "No <class> support on <broker>" / "No <class> strategies yet" messages
      still shown in their respective empty states.

---

## Backend note
- Confirmed: no backend endpoint appends status to the strategy **name** field.
  `strategy_name` is always serialized as the clean `Strategy.name`
  (`backend/app/api/routes/{live_trading,paper_trading,options_paper}.py`).
  The `(paused)`/`(draft)` clutter was added purely in the frontend, so all fixes
  are frontend-only. No backend change required.

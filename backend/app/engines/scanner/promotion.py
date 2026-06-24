"""Forward-test stats + promotion gate for shadow templates (SCANNER-V1, P2).

Reads the resolved SHADOW rows in email_signals_history (written by shadow.py,
scored by scanner._resolve_email_signal_outcomes) and computes REAL, measured
per-template stats. A template is `gate_cleared` only when its own measured
numbers clear the house quality bar — no figure is assumed or fabricated. This
view is what an operator uses to decide a promotion; promotion itself stays a
deliberate manual flip (definitions.enabled -> True), never automatic.
"""
from __future__ import annotations

# House quality gate (same bar as backtests._assess_quality)
GATE = {"min_samples": 30, "min_win_rate": 0.40, "min_profit_factor": 1.30,
        "max_drawdown_pct": 25.0, "min_expectancy": 0.0}


def _stats_from_rows(rows) -> dict:
    """rows: list of (outcome, outcome_pct) ordered by resolved_at ASC."""
    wins = [p for o, p in rows if o == "win" and p is not None]
    losses = [p for o, p in rows if o == "loss" and p is not None]
    expired = [p for o, p in rows if o == "expired" and p is not None]
    decisive = len(wins) + len(losses)
    resolved = decisive + len(expired)
    all_pct = [p for _, p in rows if p is not None]

    win_rate = (len(wins) / decisive) if decisive else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    expectancy = (sum(all_pct) / len(all_pct)) if all_pct else 0.0

    # max drawdown of the cumulative %-return curve (resolved order)
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for p in all_pct:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    cleared = (resolved >= GATE["min_samples"]
               and win_rate >= GATE["min_win_rate"]
               and pf >= GATE["min_profit_factor"]
               and max_dd <= GATE["max_drawdown_pct"]
               and expectancy > GATE["min_expectancy"])
    return {
        "resolved": resolved, "wins": len(wins), "losses": len(losses),
        "expired": len(expired), "win_rate": round(win_rate, 3),
        "profit_factor": (round(pf, 2) if pf != float("inf") else None),
        "expectancy_pct": round(expectancy, 3), "max_drawdown_pct": round(max_dd, 2),
        "gate_cleared": bool(cleared),
    }


async def template_stats(db) -> dict:
    """Per-template forward-test stats over all SHADOW rows to date."""
    from sqlalchemy import text as _t
    from app.engines.scanner.definitions import approved_templates

    rows = (await db.execute(_t("""
        SELECT matched_strategy, outcome, outcome_pct
          FROM email_signals_history
         WHERE shadow = true AND matched_strategy IS NOT NULL
         ORDER BY resolved_at ASC NULLS LAST
    """))).all()

    by_tpl: dict = {}
    pending: dict = {}
    for ms, outcome, pct in rows:
        if outcome is None:
            pending[ms] = pending.get(ms, 0) + 1
            continue
        by_tpl.setdefault(ms, []).append((outcome, float(pct) if pct is not None else None))

    out = []
    for tpl in approved_templates():
        if tpl.options.eligible:
            continue  # options templates don't forward-test until a feed is live
        st = _stats_from_rows(by_tpl.get(tpl.key, []))
        st.update({
            "key": tpl.key, "display_name": tpl.display_name, "family": tpl.family,
            "validation_method": tpl.validation_method, "pending": pending.get(tpl.key, 0),
            "enabled_live": tpl.enabled, "watch_only": not tpl.enabled,
            "promotable": st["gate_cleared"] and not tpl.enabled,
        })
        out.append(st)
    out.sort(key=lambda s: (s["gate_cleared"], s["resolved"]), reverse=True)
    return {"gate": GATE, "templates": out,
            "note": "Watch-only until gate_cleared on REAL stats AND an operator promotion."}

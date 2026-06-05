"""Theta Scanner — STT-style single-pick premarket scanner."""
import os, math, json
from datetime import datetime, timezone
from typing import Optional
from loguru import logger
from sqlalchemy import text

_CATALYST_WEIGHTS = {
    "1.01": 2.0, "2.02": 1.3, "7.01": 1.4, "8.01": 1.5, "5.02": 1.2,
}


async def _get_8k_catalyst(db, ticker: str):
    try:
        r = (await db.execute(text("""
            SELECT item_codes, filed_at FROM edgar_filings
             WHERE ticker = :t AND filed_at > NOW() - INTERVAL '36 hours'
             ORDER BY filed_at DESC LIMIT 1
        """), {"t": ticker})).first()
        if not r: return 1.0, ""
        codes = r.item_codes or "[]"
        try: codes = json.loads(codes) if isinstance(codes, str) else codes
        except Exception: codes = []
        max_w, best = 1.0, ""
        for c in codes:
            w = _CATALYST_WEIGHTS.get(c, 1.0)
            if w > max_w: max_w, best = w, f"8-K item {c}"
        return max_w, best
    except Exception:
        return 1.0, ""


async def find_best_premarket_pick(db) -> Optional[dict]:
    from app.engines.options.momentum_scanner import _fetch_market_snapshot
    rows = await _fetch_market_snapshot()
    if not rows:
        logger.warning("[ThetaScanner] scanner returned 0 rows")
        return None
    candidates = []
    for r in rows:
        try:
            price = float(r.get("day", {}).get("c") or 0)
            today_vol = int(r.get("day", {}).get("v") or 0)
            prev_close = float(r.get("prevDay", {}).get("c") or 0)
            prev_vol = int(r.get("prevDay", {}).get("v") or 0)
            ticker = r.get("ticker", "")
            if not ticker or price <= 0 or prev_close <= 0: continue
            # Skip warrants (W/WS), units (U), rights (R), and preferreds (P)
            # These have thin liquidity, unusual mechanics, and rarely trade options
            t_upper = ticker.upper()
            if (t_upper.endswith("W") or t_upper.endswith("WS") or
                t_upper.endswith(".U") or t_upper.endswith("/U") or
                t_upper.endswith(".R") or
                "." in t_upper or "/" in t_upper):
                continue
            gap_pct = (price - prev_close) / prev_close * 100.0
            if not (5.0 <= gap_pct <= 25.0): continue
            if price < 10 or price > 200: continue  # raised floor to skip micro-caps (was 2)
            if today_vol * price < 5_000_000: continue
            if prev_vol > 0 and today_vol / prev_vol < 2.0: continue
            rel_vol = today_vol / max(prev_vol, 1)
            cat_w, cat_reason = await _get_8k_catalyst(db, ticker)
            score = gap_pct * math.log(max(today_vol, 1)) * cat_w * min(rel_vol, 10) / 100
            candidates.append({
                "ticker": ticker, "price": price, "gap_pct": gap_pct,
                "today_vol": today_vol, "rel_vol": round(rel_vol, 2),
                "catalyst_weight": cat_w,
                "catalyst_reason": cat_reason or "high rel-vol gap",
                "score": round(score, 2),
            })
        except Exception:
            continue
    if not candidates:
        logger.info("[ThetaScanner] no candidate passed quality filters")
        return None
    candidates.sort(key=lambda c: c["score"], reverse=True)
    # Per-candidate visibility (every candidate, not just the winner) so we can
    # audit why a setup did/didn't make the cut on any given day.
    for c in candidates[:8]:
        logger.info(
            f"[stock-scanner] candidate {c['ticker']} score={c['score']:.2f} "
            f"gap={c['gap_pct']:.1f}% rel_vol={c['rel_vol']} "
            f"catalyst={c.get('catalyst_reason') or 'none'}"
        )
    best = candidates[0]
    # MIN_SCORE floor (added 2026-06-05): only fire if the top candidate clears
    # the quality bar. After 4 losing micro-cap stop-outs in 5 days, the bar
    # is non-negotiable. Reject sub-floor days entirely.
    MIN_SCORE = 15.0
    if best["score"] < MIN_SCORE:
        logger.info(
            f"[stock-scanner] no pick \u2014 best candidate {best['ticker']} "
            f"score={best['score']:.2f} below MIN_SCORE={MIN_SCORE}"
        )
        for c in candidates[:5]:
            logger.info(
                f"[stock-scanner] rejected candidate: {c['ticker']} "
                f"score={c['score']:.2f} gap={c['gap_pct']:.1f}% rel_vol={c['rel_vol']}"
            )
        return None
    best["entry"] = best["price"]
    # Stop is now computed at order-placement time from the ICT Oracle 5-min
    # opening candle (or pre-market session low for pre-mkt confirmed entries).
    # The blanket -3% stop was triggering 7%+ losses on micro-caps. Leave a
    # placeholder so email + email_signals_history can still render something.
    best["stop"] = round(best["price"] * 0.97, 2)  # placeholder; runner overrides
    best["stop_is_placeholder"] = True
    best["target"] = round(best["price"] * 1.10, 2)
    best["projected_move_pct"] = 10.0
    best["alternatives"] = [{"ticker": c["ticker"], "score": c["score"],
                              "gap_pct": round(c["gap_pct"], 1)} for c in candidates[1:6]]
    logger.info(f"[ThetaScanner] PICK: {best['ticker']} @ ${best['price']:.2f} score={best['score']} gap={best['gap_pct']:.1f}% vol={best['today_vol']:,} catalyst={best['catalyst_reason']}")
    # Persist to Redis so the frontend "Today's Pick" widget can read it
    try:
        import redis.asyncio as _r
        import json as _j
        from datetime import date as _d
        _redis = _r.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"), decode_responses=True)
        payload = dict(best)
        payload["picked_at"] = datetime.now(timezone.utc).isoformat()
        await _redis.setex(f"theta:lastpick:{_d.today().isoformat()}", 36*3600, _j.dumps(payload))
        await _redis.setex("theta:lastpick:latest", 36*3600, _j.dumps(payload))
    except Exception as e:
        logger.warning(f"[ThetaScanner] could not persist pick to redis: {e}")
    return best


async def emit_theta_pick(db, user, pick: dict) -> bool:
    from app.services.email import _send
    qty = max(1, int(1000 / pick["price"]))
    subject = f"🎯 Theta Scanner: {pick['ticker']} +{pick['projected_move_pct']:.0f}% target ({pick['catalyst_reason']})"
    alt_html = ""
    if pick.get("alternatives"):
        alt_html = "<p style='font-size:11px;color:#94a3b8;'>Runners-up: " + ", ".join(
            f"{a['ticker']} (gap {a['gap_pct']}%)" for a in pick["alternatives"][:3]) + "</p>"
    html = f"""<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      <h1 style="margin:0 0 8px;color:#7c3aed;">🎯 Theta Scanner pick</h1>
      <p style="color:#64748b;font-size:12px;margin:0 0 20px;">STT-style single highest-conviction setup for {datetime.now(timezone.utc).date()}</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr><td style="padding:8px;color:#475569;">Ticker</td><td style="text-align:right;font-weight:700;font-size:18px;">{pick['ticker']}</td></tr>
        <tr><td style="padding:8px;color:#475569;">Entry</td><td style="text-align:right;font-weight:700;">${pick['entry']:.2f}</td></tr>
        <tr><td style="padding:8px;color:#475569;">Stop (-3%)</td><td style="text-align:right;font-weight:700;color:#dc2626;">${pick['stop']:.2f}</td></tr>
        <tr><td style="padding:8px;color:#475569;">Target (+10%)</td><td style="text-align:right;font-weight:700;color:#16a34a;">${pick['target']:.2f}</td></tr>
        <tr><td style="padding:8px;color:#475569;">Gap</td><td style="text-align:right;">+{pick['gap_pct']:.1f}%</td></tr>
        <tr><td style="padding:8px;color:#475569;">Volume vs prior</td><td style="text-align:right;">{pick['rel_vol']}×</td></tr>
        <tr><td style="padding:8px;color:#475569;">Catalyst</td><td style="text-align:right;">{pick['catalyst_reason']}</td></tr>
        <tr><td style="padding:8px;color:#475569;">Score</td><td style="text-align:right;">{pick['score']}</td></tr>
      </table>
      <p style="margin:16px 0;font-size:12px;color:#64748b;">Picked by <b>Theta Scanner</b> — your sole auto-pick source. All 22 ICT/options strategies remain in your Live Trading library for manual trades.</p>
      {alt_html}
      <p style="font-size:10px;color:#94a3b8;margin-top:24px;">Risk: 10% target is historical median for setups matching this profile. Not a guarantee. Confirm size + stop before adding.</p>
    </div>"""
    ok = _send(user.email, subject, html)

    # === Entry timing gate (2026-06-05) ===
    # The blind 15-min auto-execute caused SPRC to enter 2.5h after pick at
    # a price that was ALREADY below the stop. Per the user spec, the
    # broker-fire path is now ROUTED through _execute_stock_pick_with_timing_gate
    # which checks (a) pre-market VWAP + higher-highs between 08:30-09:25 ET,
    # or (b) places a market-on-open order at 09:30+. Stop is computed at
    # order-placement time from the ICT Oracle 5-min opening candle (or the
    # pre-market session low for pre-mkt-confirmed entries) instead of the
    # blanket -3% which was triggering 7%+ losses on micro-caps.
    #
    # Implementation: enqueue the pick to Redis. The scheduler tick
    # (_check_pending_stock_entries) picks it up each cycle and asks the
    # timing gate whether to fire now, wait, or defer.
    try:
        from app.engines.options.premarket_scheduler import _resolve_user_broker
        broker_account_id, trade_mode = await _resolve_user_broker(user.id)
        # Honor per-account trading_enabled toggle (TradeSyncer-style)
        if broker_account_id:
            from app.models.user import BrokerAccount
            from sqlalchemy import select as _sel
            _acct = (await db.execute(_sel(BrokerAccount).where(BrokerAccount.id == broker_account_id))).scalar_one_or_none()
            if _acct and not getattr(_acct, "trading_enabled", True):
                logger.info(f"[ThetaScanner] {user.email}: trading_enabled=False on broker account — skipping Tradier fire")
                broker_account_id = None
        if trade_mode == "live" and broker_account_id:
            from datetime import date as _date
            qty = max(1, int(1000 / pick["price"]))  # $1000 position
            entry_payload = {
                "user_id": str(user.id),
                "user_email": user.email,
                "broker_account_id": broker_account_id,
                "ticker": pick["ticker"],
                "direction": "long",
                "qty": qty,
                "pick_price": pick["price"],  # snapshot at scan time — not the entry
                "target": pick["target"],
                "pick_date": _date.today().isoformat(),
                "score": pick.get("score"),
                "gap_pct": pick.get("gap_pct"),
            }
            try:
                import redis.asyncio as _ra
                import json as _j2
                _redis = _ra.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"), decode_responses=True)
                entry_key = f"theta:pending_entry:{entry_payload['pick_date']}:{user.id}"
                # Only queue if not already queued (idempotent — the daily
                # fire-slot already ensures one pick per day, but in case
                # emit_theta_pick re-fires for any reason we do not want dupes).
                if await _redis.set(entry_key, _j2.dumps(entry_payload), ex=36*3600, nx=True):
                    logger.info(
                        f"[stock-entry] QUEUED ticker={pick['ticker']} user={user.email} "
                        f"qty={qty} pick_price=${pick['price']:.2f} — waiting for timing gate"
                    )
                else:
                    logger.info(f"[stock-entry] already queued for {user.email} {pick['ticker']} today — skip")
            except Exception as _qe:
                logger.error(f"[ThetaScanner] failed to enqueue pending entry: {_qe}")
    except Exception as e:
        logger.error(f"[ThetaScanner] entry-queue failed: {e}")

    # Persist to email_signals_history so the Email Signals page can show
    # today's pick + the running 30-day log.
    try:
        from sqlalchemy import text as _t
        await db.execute(_t("""
            INSERT INTO email_signals_history
              (picked_at, ticker, asset_type, direction, entry, stop, target,
               gap_pct, rel_vol, today_vol, score, catalyst_reason)
            VALUES (NOW(), :tk, :at, 'long', :en, :st, :tg, :gp, :rv, :tv, :sc, :cr)
        """), {
            "tk": pick["ticker"], "at": pick.get("asset_type", "options"),
            "en": pick["entry"], "st": pick["stop"], "tg": pick["target"],
            "gp": pick["gap_pct"], "rv": pick.get("rel_vol", 0),
            "tv": pick["today_vol"], "sc": pick["score"],
            "cr": pick["catalyst_reason"],
        })
        await db.commit()
        logger.info(f"[ThetaScanner] persisted to email_signals_history: {pick['ticker']}")
    except Exception as e:
        logger.error(f"[ThetaScanner] history persist failed: {e}")
    return ok

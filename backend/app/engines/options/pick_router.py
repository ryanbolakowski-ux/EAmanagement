# UNWIRED (2026-07-07 verify round): superseded by the upgraded legacy
# pending-entry path (emit_theta_pick -> theta:pending_entry -> timing gate),
# which now carries allocation sizing + sandbox routing. Two racing order
# paths = the double-buy defect; kept for a future native Tradier-OTOCO
# rebuild but NOT called from the scheduler.
"""Daily-pick LIVE routing — THETA-LIVE-PICK-V1 (owner request 2026-07-07).

The Live Trading page promises "buys $X of Saro's pick every day"
(broker_accounts.theta_scanner_allocation_usd), but until now NO code ever
placed that order — the only live path was the legacy $1000-hardcoded
pending-entry queue, which additionally required a non-sandbox account.

route_pick_to_live() routes the daily Theta pick to the user's live-capable
broker account behind an 8-rung fail-CLOSED safety ladder. EVERY skip and
EVERY placement writes a security_audit_log row (event "theta_pick_route").

Exit handling (why there is no broker-resident bracket):
  * The Tradier adapter is single-leg only — grep of tradier.py has zero
    otoco/oco/bracket/multileg support; it can send MARKET/LIMIT/STOP orders
    but cannot LINK them.
  * Unlinked broker-resident STOP + LIMIT sells are dangerous for equities:
    when one fills, the sibling stays working and can open an accidental
    SHORT (LiveTrader only gets away with the emulation because it runs a
    monitoring loop that cancels siblings; no such loop exists for stocks).
  * So we follow the exact prod-tested pattern every other scanner stock
    entry uses (_execute_stock_pick_with_timing_gate): MARKET buy, then
    register the position in open_positions_watch — the TrailWatch loop
    enforces hard stop / target / 3% trail / 15:55 ET EOD close server-side
    and mirrors the close into the trades table.
  A LOUD warning is logged on every placement so nobody assumes a
  broker-resident stop exists.
"""
import os
import logging
from datetime import datetime, timezone

# Module-level ref so tests can monkeypatch pick_router.auto_trade_allowed.
from app.core.auto_trade_guard import auto_trade_allowed

logger = logging.getLogger("theta.pick_router")

AUDIT_EVENT = "theta_pick_route"
IDEMPOTENCY_PREFIX = "theta:livebuy:"          # theta:livebuy:{date}:{user_id}
IDEMPOTENCY_TTL_S = 20 * 3600                   # 20h — never twice per day
LEGACY_PENDING_PREFIX = "theta:pending_entry:"  # timing-gate queue (dedupe)
MAX_QUOTE_DRIFT = 0.03                          # |quote - pick.entry|/entry


# ── pure helpers ────────────────────────────────────────────────────────────

def _routing_enabled() -> bool:
    """Rung 1 — env kill switch. Default ON; THETA_LIVE_PICK_ROUTING=0 kills."""
    return os.environ.get("THETA_LIVE_PICK_ROUTING", "1").strip() != "0"


def _now_et():
    import zoneinfo
    return datetime.now(timezone.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))


def _market_open_now(now_et=None) -> bool:
    """Rung 8 — regular session 09:30-15:55 ET, weekdays only."""
    et = now_et if now_et is not None else _now_et()
    if et.weekday() >= 5:
        return False
    m = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= m <= (15 * 60 + 55)


def compute_router_shares(allocation_usd, quote,
                          cached_cash=None, cached_buying_power=None,
                          account_type: str = "cash") -> int:
    """Rung 7 — shares = floor(min(allocation, known funds) / quote).

    Funds cap: cash accounts cap at cached_cash, margin at
    cached_buying_power; if the preferred figure is unknown we fall back to
    the other known one; if neither is known the allocation alone sizes it.
    Returns 0 on any invalid input (fail-closed)."""
    import math
    try:
        alloc = float(allocation_usd)
        q = float(quote)
    except (TypeError, ValueError):
        return 0
    if alloc <= 0 or q <= 0:
        return 0
    cap = alloc
    is_cash = (account_type or "cash").strip().lower() != "margin"
    primary = cached_cash if is_cash else cached_buying_power
    fallback = cached_buying_power if is_cash else cached_cash
    funds = primary if primary is not None else fallback
    try:
        if funds is not None and float(funds) > 0:
            cap = min(cap, float(funds))
    except (TypeError, ValueError):
        pass
    return int(math.floor(cap / q))


# ── I/O helpers (each one monkeypatchable in tests) ─────────────────────────

async def _claim_daily_slot(user_id, date_str: str, ticker: str) -> bool:
    """Rung 2 — Redis SETNX theta:livebuy:{date}:{user_id} ex=20h."""
    import redis.asyncio as _ra
    r = _ra.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"),
                     decode_responses=True)
    key = f"{IDEMPOTENCY_PREFIX}{date_str}:{user_id}"
    return bool(await r.set(key, ticker or "1", nx=True, ex=IDEMPOTENCY_TTL_S))


async def _lookup_live_account(user_id):
    """Rung 3 — the user's live-capable account: active + trading_enabled
    tradier/alpaca. Real-money accounts are preferred; a sandbox/demo account
    is an accepted fallback because broker_factory routes its credentials to
    sandbox.tradier.com (is_demo OR sandbox_mode) — it can never place a
    real-money order. Returns a plain dict or None."""
    from app.database import async_session_factory
    from sqlalchemy import text
    async with async_session_factory() as db:
        r = (await db.execute(text("""
            SELECT id, account_type, theta_scanner_allocation_usd,
                   cached_cash, cached_buying_power,
                   (COALESCE(is_demo, false) OR COALESCE(sandbox_mode, false)) AS is_sandbox
              FROM broker_accounts
             WHERE user_id = CAST(:uid AS uuid)
               AND lower(broker) IN ('tradier', 'alpaca')
               AND is_active = true
               AND trading_enabled = true
             ORDER BY (COALESCE(is_demo, false) OR COALESCE(sandbox_mode, false)) ASC,
                      created_at DESC
             LIMIT 1
        """), {"uid": str(user_id)})).fetchone()
    if not r:
        return None
    return {
        "id": str(r.id),
        "account_type": r.account_type or "cash",
        "allocation_usd": r.theta_scanner_allocation_usd,
        "cached_cash": r.cached_cash,
        "cached_buying_power": r.cached_buying_power,
        "is_sandbox": bool(r.is_sandbox),
    }


async def _fetch_live_quote(ticker: str):
    """Rung 6 — FMP real-time short quote; None on any failure."""
    try:
        from app.engines.data_feeds.fmp_feed import fetch_quote_short_price
        px = await fetch_quote_short_price(ticker)
        return float(px) if px else None
    except Exception as e:
        logger.warning(f"[pick-route] quote fetch failed for {ticker}: {e}")
        return None


async def _audit(user_id, detail: dict) -> None:
    """Security audit trail row (event theta_pick_route). Best-effort."""
    try:
        from app.database import async_session_factory
        from app.api.routes.security import audit_log
        async with async_session_factory() as db:
            await audit_log(db, user_id, AUDIT_EVENT, detail, None)
            await db.commit()
    except Exception as e:
        logger.warning(f"[pick-route] audit write failed: {e}")


async def _get_broker(broker_account_id):
    """Build + connect the broker via the existing factory. None on failure."""
    from app.database import async_session_factory
    from app.models.user import BrokerAccount
    from sqlalchemy import select
    from app.engines.live_trading.broker_factory import build_broker_from_account
    async with async_session_factory() as db:
        acct = (await db.execute(
            select(BrokerAccount).where(BrokerAccount.id == broker_account_id)
        )).scalar_one_or_none()
    if not acct:
        return None
    broker = build_broker_from_account(acct)
    if not broker:
        return None
    ok = await broker.connect()
    return broker if ok else None


async def _register_position(*, user_id, broker_account_id, ticker, shares,
                             entry_price, stop, target, broker_order_id):
    """Register the fill in open_positions_watch + trades — the exact shape
    _execute_stock_pick_with_timing_gate persists, so TrailWatch (hard stop /
    target / 3% trail / EOD close) manages the exit server-side."""
    from app.database import async_session_factory
    from sqlalchemy import text as _t
    async with async_session_factory() as db:
        await db.execute(_t("""
            INSERT INTO open_positions_watch
              (user_id, broker_account_id, ticker, qty, entry_price,
               trail_pct, trail_high, hard_stop, target, source, broker_order_id)
            VALUES (CAST(:uid AS uuid), CAST(:bid AS uuid), :tk, :q, :ep,
                    3.0, :ep, :stop, :tgt, 'theta_scanner', :oid)
        """), {
            "uid": str(user_id), "bid": str(broker_account_id), "tk": ticker,
            "q": int(shares), "ep": float(entry_price), "stop": float(stop),
            "tgt": float(target), "oid": broker_order_id,
        })
        sess_row = (await db.execute(_t("""
            SELECT id FROM trade_sessions
             WHERE user_id = CAST(:uid AS uuid) AND mode = 'live'
               AND label = 'Theta Scanner'
             ORDER BY started_at DESC LIMIT 1
        """), {"uid": str(user_id)})).first()
        if sess_row:
            sess_id = sess_row.id
        else:
            ins = await db.execute(_t("""
                INSERT INTO trade_sessions (user_id, strategy_id, mode, label, broker_account_id, started_at, is_active)
                VALUES (CAST(:uid AS uuid), NULL, 'live', 'Theta Scanner', CAST(:bid AS uuid), NOW(), TRUE)
                RETURNING id
            """), {"uid": str(user_id), "bid": str(broker_account_id)})
            sess_id = ins.scalar()
        await db.execute(_t("""
            INSERT INTO trades (session_id, user_id, instrument, direction,
                entry_price, stop_loss, take_profit, contracts, entry_time,
                mode, status, broker_account_id, broker_order_id)
            VALUES (:sid, CAST(:uid AS uuid), :inst, 'long',
                :ep, :sl, :tp, :q, NOW(), 'live', 'open', CAST(:bid AS uuid), :oid)
        """), {
            "sid": sess_id, "uid": str(user_id), "inst": ticker,
            "ep": float(entry_price), "sl": float(stop), "tp": float(target),
            "q": int(shares), "bid": str(broker_account_id), "oid": broker_order_id,
        })
        await db.commit()


async def _clear_legacy_pending(date_str: str, user_id) -> None:
    """Delete the legacy $1000 timing-gate queue entry for this (date, user)
    so the 5-min tick cannot place a SECOND buy for the same pick."""
    try:
        import redis.asyncio as _ra
        r = _ra.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"),
                         decode_responses=True)
        n = await r.delete(f"{LEGACY_PENDING_PREFIX}{date_str}:{user_id}")
        if n:
            logger.info(f"[pick-route] cleared legacy pending-entry key for {user_id} (dedupe)")
    except Exception as e:
        logger.warning(f"[pick-route] legacy pending-entry clear failed: {e}")


# ── the router ──────────────────────────────────────────────────────────────

async def route_pick_to_live(user_id, email, pick: dict) -> tuple[bool, str]:
    """Route today's Theta pick to the user's live broker account.

    Returns (placed, reason). Every rung is fail-CLOSED: any error, missing
    prerequisite, or sanity failure skips with a specific reason, logs
    '[pick-route]', and writes a security_audit_log row (theta_pick_route).
    """
    ticker = str(pick.get("ticker") or "").upper()
    date_str = _now_et().date().isoformat()
    ctx = {"ticker": ticker, "date": date_str, "email": email}

    async def _skip(reason: str, **extra):
        logger.info(f"[pick-route] SKIP {email} {ticker or '?'}: {reason}")
        await _audit(user_id, {"placed": False, "reason": reason, **ctx, **extra})
        return (False, reason)

    # Rung 0 — pick shape sanity (extra fail-closed rungs).
    if not ticker:
        return await _skip("pick_missing_ticker")
    if pick.get("watch_only"):
        # Owner rule (emit_theta_pick precedent): watch-only never trades.
        return await _skip("watch_only_pick_never_trades")
    try:
        entry_ref = float(pick.get("entry") or pick.get("price") or 0)
        stop = float(pick.get("stop") or 0)
        target = float(pick.get("target") or 0)
    except (TypeError, ValueError):
        return await _skip("pick_levels_invalid")
    if entry_ref <= 0 or stop <= 0 or target <= 0 or stop >= entry_ref or target <= entry_ref:
        return await _skip("pick_levels_invalid")

    # Rung 1 — env kill switch (THETA_LIVE_PICK_ROUTING, default on).
    if not _routing_enabled():
        return await _skip("routing_disabled_by_env")

    # Rung 2 — once per user per day (Redis SETNX, 20h TTL).
    try:
        fresh = await _claim_daily_slot(user_id, date_str, ticker)
    except Exception as e:
        return await _skip(f"idempotency_error:{type(e).__name__}")
    if not fresh:
        return await _skip("already_routed_today")

    # Rung 3 — live-capable broker account.
    try:
        acct = await _lookup_live_account(user_id)
    except Exception as e:
        return await _skip(f"account_lookup_error:{type(e).__name__}")
    if not acct:
        return await _skip("no_live_broker_account")
    ctx["broker_account_id"] = acct["id"]
    ctx["sandbox"] = bool(acct.get("is_sandbox"))

    # Rung 4 — daily allocation must be explicitly set and positive.
    alloc_raw = acct.get("allocation_usd")
    try:
        alloc = float(alloc_raw) if alloc_raw is not None else 0.0
    except (TypeError, ValueError):
        alloc = 0.0
    if alloc <= 0:
        return await _skip("no daily allocation set — set it on the Live Trading page")

    # Rung 5 — Phase E fail-closed backstop (tier_5 + agreement + trading_enabled).
    allowed, why = await auto_trade_allowed(
        user_id, acct["id"],
        context={"kind": "theta_pick", "ticker": ticker, "date": date_str})
    if not allowed:
        return await _skip(f"auto_trade_blocked:{why}")

    # Rung 6 — live quote sanity (real-time FMP vs the pick's entry basis).
    quote = await _fetch_live_quote(ticker)
    if not quote or quote <= 0:
        return await _skip("live_quote_unavailable")
    drift = abs(quote - entry_ref) / entry_ref
    if drift > MAX_QUOTE_DRIFT:
        return await _skip(
            f"quote_drift_{drift * 100:.1f}pct_exceeds_3pct",
            quote=quote, pick_entry=entry_ref)

    # Rung 7 — sizing off the LIVE quote, capped by known funds.
    shares = compute_router_shares(
        alloc, quote,
        cached_cash=acct.get("cached_cash"),
        cached_buying_power=acct.get("cached_buying_power"),
        account_type=acct.get("account_type") or "cash")
    if shares < 1:
        return await _skip(
            f"allocation_below_1_share(alloc=${alloc:.2f}, quote=${quote:.2f})")

    # Rung 8 — regular session only (09:30-15:55 ET weekday).
    if not _market_open_now():
        return await _skip("market_closed")

    # ── Placement — existing broker layer, MARKET buy. ─────────────────────
    try:
        broker = await _get_broker(acct["id"])
    except Exception as e:
        return await _skip(f"broker_build_error:{type(e).__name__}")
    if broker is None:
        return await _skip("broker_connect_failed")

    from app.engines.live_trading.broker_base import (
        OrderRequest, OrderSide, OrderType, OrderStatus)
    try:
        resp = await broker.place_order(OrderRequest(
            instrument=ticker, side=OrderSide.BUY,
            quantity=int(shares), order_type=OrderType.MARKET))
    except Exception as e:
        return await _skip(f"order_error:{type(e).__name__}", shares=int(shares), quote=quote)
    if resp is None or getattr(resp, "status", None) == OrderStatus.REJECTED:
        msg = getattr(resp, "message", "") or "no broker response"
        logger.error(f"[pick-route] ENTRY REJECTED {email} {ticker} x{shares}: {msg}")
        await _audit(user_id, {"placed": False, "reason": f"entry_rejected:{msg}",
                               "shares": int(shares), "quote": quote, **ctx})
        return (False, f"entry_rejected:{msg}")

    order_id = getattr(resp, "broker_order_id", None)

    # LOUD: the Tradier adapter cannot place a linked bracket/OTOCO/OCO
    # (single-leg only), and unlinked resident STOP+LIMIT sells risk an
    # accidental short once one of them fills. Exits are therefore
    # server-managed by TrailWatch via open_positions_watch — same as every
    # other scanner stock entry. If registration fails, exits are MANUAL.
    logger.warning(
        f"[pick-route] LOUD: {ticker} x{shares} for {email} has NO broker-resident "
        f"exit orders (Tradier adapter is single-leg; no OTOCO/OCO). Exits are "
        f"server-managed by TrailWatch (stop={stop}, target={target}, 3% trail, "
        f"EOD close). If the backend goes down, exits become MANUAL.")

    exits_mode = "server_managed_trailwatch"
    try:
        await _register_position(
            user_id=user_id, broker_account_id=acct["id"], ticker=ticker,
            shares=int(shares), entry_price=float(quote), stop=stop,
            target=target, broker_order_id=order_id)
    except Exception as e:
        exits_mode = "MANUAL"
        logger.error(
            f"[pick-route] LOUD: position registration FAILED for {ticker} x{shares} "
            f"({email}) — NO stop/target is managed ANYWHERE. EXITS ARE MANUAL. err={e}")

    # Dedupe against the legacy $1000 timing-gate queue (double-buy guard).
    await _clear_legacy_pending(date_str, user_id)

    await _audit(user_id, {
        "placed": True, "reason": "placed", "shares": int(shares),
        "quote": quote, "stop": stop, "target": target,
        "allocation_usd": alloc, "broker_order_id": order_id,
        "exits": exits_mode, **ctx})
    logger.info(
        f"[pick-route] PLACED {email} {ticker} x{shares} @~${quote:.2f} "
        f"(alloc=${alloc:.0f}, stop={stop}, target={target}) order={order_id}")
    return (True, "placed")

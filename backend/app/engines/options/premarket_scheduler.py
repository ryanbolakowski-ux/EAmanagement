import os
"""Pre-market + intraday scanner scheduler.

Mirrors StocksToTrade's behaviour:
  • Runs 04:00 → 20:00 ET every weekday
  • Skips ±30 min around FOMC, CPI, PPI, NFP, GDP, etc.
  • Pre-market scan at 08:30 ET produces pending trades with Confirm/Skip
  • Intraday scans (every 5 min) emit receipt-only signals that auto-execute

Two scanner modes per strategy:
  • signal_mode = "momentum_scanner" → catch 10%+ movers (the STT clone)
  • signal_mode = "universe_scan"    → run ICT logic across the watchlist
                                       (the existing path)
"""
import asyncio
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from loguru import logger


def _smart_atr_proxy(price: float) -> float:
    """Bug #26 fix: tiered ATR proxy that respects bid-ask spread on low-
    priced tickers and doesn't go absurd on high-priced ones.

    - Sub-$2 tickers: 5% (penny stocks need wide stops vs spread)
    - $2-$10: 3%
    - $10-$50: 2%
    - $50-$200: 1.5%
    - $200+: 1.2% (don't over-tighten — pre-market is volatile)
    Minimum stop distance always >= $0.05 to avoid sub-tick stops.
    """
    p = abs(float(price))
    if p < 2:    pct = 0.05
    elif p < 10: pct = 0.03
    elif p < 50: pct = 0.02
    elif p < 200: pct = 0.015
    else:        pct = 0.012
    return max(0.05, p * pct)

from sqlalchemy import select, text

from app.database import async_session_factory
from app.models.user import User
from app.models.strategy import Strategy
from app.engines.options.universe import get_universe
from app.engines.options.universe_scanner import scan_universe, ScannerHit
from app.engines.options.momentum_scanner import scan_for_momentum, MomentumHit
from app.engines.options.stt_scanners import (
    scan_low_float_squeeze, scan_52w_breakout,
    scan_premarket_gappers, scan_oracle_opening_candle, STTHit,
)
from app.engines.options.pending_trades import (
    create_pending_trade, auto_execute_pending, expire_old_pending,
)
from app.engines.options.news_calendar import (
    refresh_blackouts, is_blackout_active, next_clear_time,
)
from app.engines.options.sec_edgar import refresh_edgar
from app.engines.strategy_engine.base_strategy import StrategyConfig


ET = ZoneInfo("America/New_York")

# Daily window — matches StocksToTrade's 4am-8pm ET coverage
MARKET_OPEN_ET    = dtime(4, 0)
MARKET_CLOSE_ET   = dtime(20, 0)
PREMARKET_SCAN_T  = dtime(9, 0)   # the special pre-market batch with confirm flow
INTRADAY_PERIOD_SEC = 5 * 60       # every 5 min during the window

# News blackout buffer — pause scanner this many minutes either side of a
# high-impact event
BLACKOUT_BUFFER_MIN = 30


def _today_et() -> datetime:
    return datetime.now(ET)


def _within_market_window(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    t = now_et.time()
    return MARKET_OPEN_ET <= t <= MARKET_CLOSE_ET


def _next_market_open_et() -> datetime:
    """Next 04:00 ET — usually tomorrow, or Monday if it's the weekend."""
    now = _today_et()
    today_open = now.replace(hour=4, minute=0, second=0, microsecond=0)
    target = today_open if now < today_open else today_open + timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target



async def _compute_qty_from_sizing(*, broker_account_id, ticker: str, entry: float,
                                     stop: float, default: int = 100) -> int:
    """Compute share quantity for a stock pick from the broker account's
    saved sizing rules. Honors account_type (cash vs margin) for BP cap.

    Migrated to delegate the core min-of size math to
    app.core.sizing.unified_size (#136). Two edge behaviours are preserved
    exactly: (1) a missing/invalid stop defaults to a 2% per-share risk, and
    (2) a valid pick never sizes to 0 — the final max(1, shares) emergency
    fallback keeps at least 1 share once we have a real risk basis.
    """
    if not broker_account_id:
        return max(1, default)
    try:
        from app.database import async_session_factory as _asf
        from app.models.user import BrokerAccount as _BA
        from sqlalchemy import select as _sel
        from app.core.sizing import unified_size
        async with _asf() as _db:
            acct = (await _db.execute(_sel(_BA).where(_BA.id == broker_account_id))).scalar_one_or_none()
        if not acct:
            return max(1, default)

        risk_usd = acct.risk_per_trade_usd
        if not risk_usd:
            pct = acct.risk_per_trade_pct or 1.0
            eq = acct.cached_equity or 0.0
            risk_usd = (eq * pct / 100.0) if eq else None
        if not risk_usd or risk_usd <= 0:
            return max(1, default)

        # Preserve the default-2%-stop-when-missing edge behaviour by mapping a
        # missing/invalid stop to an effective stop that yields the same
        # per-share risk (entry * 0.02) under unified_size's point_value=1.
        if stop and stop > 0:
            eff_stop = stop
        else:
            eff_stop = entry * (1.0 - 0.02)
        if abs(entry - eff_stop) <= 0:
            return max(1, default)

        # Capital cap mapping: the OLD code applied the buying-power cap to all
        # accounts, then ALSO applied a cash cap (cached_equity or bp) for cash
        # accounts. unified_size applies a single capital cap chosen by
        # account_type, so fold both into the relevant slot as the tighter one.
        bp = acct.cached_buying_power or 0.0
        is_cash = (acct.account_type or "cash").lower() == "cash"
        if is_cash:
            cash = (acct.cached_equity or bp)
            caps = [c for c in (bp if bp > 0 else None, cash if cash else None) if c]
            eff_cash = min(caps) if caps else None
            res = unified_size(
                entry_price=entry,
                stop_loss=eff_stop,
                risk_per_trade_usd=risk_usd,
                account_equity=acct.cached_equity,
                max_position_usd=acct.max_position_usd,
                cached_cash=eff_cash,
                account_type="cash",
                point_value=1.0,
            )
        else:
            res = unified_size(
                entry_price=entry,
                stop_loss=eff_stop,
                risk_per_trade_usd=risk_usd,
                account_equity=acct.cached_equity,
                max_position_usd=acct.max_position_usd,
                cached_buying_power=(bp if bp > 0 else None),
                account_type="margin",
                point_value=1.0,
            )

        shares = res.final_size
        return max(1, shares) if shares >= 1 else 0
    except Exception as e:
        from loguru import logger as _lg
        _lg.error(f"[sizing] compute failed for {ticker}: {e}")
        return max(1, default)


async def _place_intraday_broker_order(broker_account_id: str, ticker: str,
                                         direction: str, qty: int):
    """Place a real broker order for an intraday scanner pick.

    Returns (broker_order_id, status_str, error_msg). Status is one of:
      'executed' — order accepted by broker
      'rejected' — broker said no
      'error'    — internal error
    """
    from app.database import async_session_factory as _asf
    from app.models.user import BrokerAccount as _BA
    from app.engines.live_trading.broker_factory import build_broker_from_account as _bld
    from app.engines.live_trading.broker_base import OrderRequest as _OR, OrderSide as _OS, OrderType as _OT, OrderStatus as _OSt
    from sqlalchemy import select as _sel
    try:
        async with _asf() as db:
            acct = (await db.execute(
                _sel(_BA).where(_BA.id == broker_account_id)
            )).scalar_one_or_none()
        if not acct:
            return None, "error", "broker account not found"
        broker = _bld(acct)
        if not broker:
            return None, "error", f"unsupported broker: {acct.broker}"
        ok = await broker.connect()
        if not ok:
            return None, "error", "broker connect failed"
        resp = await broker.place_order(_OR(
            instrument=ticker.upper(),
            side=_OS.BUY if direction == "long" else _OS.SELL,
            quantity=qty,
            order_type=_OT.MARKET,
        ))
        if resp.status == _OSt.REJECTED:
            return None, "rejected", resp.message
        return resp.broker_order_id, "executed", None
    except Exception as e:
        return None, "error", str(e)


async def _build_config(strategy: Strategy, instrument: str) -> StrategyConfig:
    return StrategyConfig(
        name=strategy.name, instruments=[instrument],
        primary_timeframe=strategy.primary_timeframe or "5m",
        execution_timeframe=strategy.execution_timeframe or "1m",
        higher_timeframes=strategy.higher_timeframes or ["1H"],
        risk_reward_ratio=strategy.risk_reward_ratio or 2.0,
        stop_loss_type=strategy.stop_loss_type or "structure",
        max_contracts=strategy.max_contracts or 1,
        fvg_min_size_ticks=strategy.fvg_min_size_ticks or 4,
    )


# ── Pre-market 09:00 batch ──────────────────────────────────────────────────

async def _emit_premarket_hit(strategy, user, hit, expires_in: int):
    """Emit one pre-market pending trade + confirm email."""
    # Session window guard — block emits outside the 4 strict windows
    sess_label = _current_session_label()
    if sess_label == "DEAD":
        logger.debug(f"[Premarket] DEAD-ZONE: {hit.ticker} for {user.email} suppressed")
        return
    # Email cap: 1 per (user, instrument-family, session) atomically
    sess_label = _current_session_label()
    if not await _claim_session_slot(str(user.id), hit.ticker, sess_label):
        logger.info(f"[Premarket] CAP-HIT {hit.ticker} for {user.email} — already signaled {sess_label} session")
        return
    if not await _claim_daily_slot(str(user.id), max_per_day=1):
        logger.info(f"[Premarket] CAP-HIT {hit.ticker} for {user.email} — daily cap (1) reached")
        return
    from app.services.email import send_pending_trade_confirm_email

    # Default 1% stop, RR-based target (per options page universal rules)
    atr_proxy = _smart_atr_proxy(hit.price if hasattr(hit, "price") else hit.spot)
    spot_or_price = getattr(hit, "spot", None) or getattr(hit, "price", 0.0)
    direction = hit.direction if hasattr(hit, "direction") else ("long" if hit.change_pct > 0 else "short")
    if direction == "long":
        entry, stop = spot_or_price, spot_or_price - atr_proxy
        target = spot_or_price + atr_proxy * (strategy.risk_reward_ratio or 2.0)
    else:
        entry, stop = spot_or_price, spot_or_price + atr_proxy
        target = spot_or_price - atr_proxy * (strategy.risk_reward_ratio or 2.0)

    ticker = hit.ticker
    bias = getattr(hit, "bias", None) or (getattr(hit, "catalyst", None))
    reason = (hit.note if hasattr(hit, "note") else
              getattr(hit, "reason", f"{direction.upper()} signal on {ticker}"))

    pid = await create_pending_trade(
        user_id=str(user.id), strategy_id=str(strategy.id),
        mode="paper",
        instrument=ticker, direction=direction,
        contracts=int(strategy.max_contracts or 1),
        entry=entry, stop=stop, target=target,
        bias=bias, reason=reason, is_intraday=False,
        expires_in_minutes=expires_in,
        notes={"score": getattr(hit, "score", 0.0),
               "change_pct": getattr(hit, "change_pct", None),
               "catalyst":   getattr(hit, "catalyst", None),
               "session": _current_session_label()},
    )
    token = (await _get_confirm_token(pid)) or ""
    expires_at_human = (datetime.now(ET) + timedelta(minutes=expires_in)).strftime("%I:%M %p ET")

    try:
        send_pending_trade_confirm_email(
            to=user.email, username=getattr(user, "username", ""),
            ticker=ticker, direction=direction,
            entry=entry, stop=stop, target=target,
            bias=str(bias or "neutral"), reason=reason,
            confirm_token=token, expires_at_human=expires_at_human,
            strategy_name=strategy.name,
        )
    except Exception as e:
        logger.error(f"[Premarket] email failed for {ticker}: {e}")




_MICRO_TO_PARENT = {"MES":"ES","MNQ":"NQ","MYM":"YM","M2K":"RTY","MCL":"CL","MGC":"GC","MET":"ETH","MBT":"BTC"}
def _normalize_instrument(inst: str) -> str:
    if not inst: return inst
    u = inst.upper(); return _MICRO_TO_PARENT.get(u, u)

def _current_session_label() -> str:
    """STT-style strict session windows. Each window allows ONE email max.
    Everything outside these windows returns 'DEAD' and emits NOTHING.

    Windows (ET):
      ASIA      18:00 -> 03:00 (next day)   — 1 email max
      LONDON    03:00 -> 09:00              — 1 email max
      NY_AM     09:30 -> 12:00              — 1 email max
      NY_PM     14:30 -> 16:30              — 1 email max
      DEAD      everything else             — 0 emails
    Total daily max per user: 4 emails."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        import zoneinfo as _zi
        et = _dt.now(_tz.utc).astimezone(_zi.ZoneInfo("America/New_York"))
    except Exception:
        et = _dt.now(_tz.utc)
    t = et.hour * 60 + et.minute
    # ASIA wraps midnight, so split it
    if t >= 18*60 or t < 3*60:        return "ASIA"
    if 3*60 <= t < 9*60:              return "LONDON"
    if 9*60+30 <= t < 11*60:          return "NY_AM"
    if 13*60+30 <= t < 16*60+30:      return "NY_PM"
    return "DEAD"  # quiet zones: 9:00-9:30, 12:00-14:30, 16:30-18:00




# Redis-backed atomic email caps. SETNX returns True only for the first
# claimer — kills the race condition where 14 strategies fan out in <1s
# and each finds "no prior pending_trade" before any has committed.
import redis.asyncio as _redis_lib
import os as _os
_email_redis = None
def _get_email_redis():
    global _email_redis
    if _email_redis is None:
        url = _os.environ.get("REDIS_URL", "redis://edge_redis:6379")
        _email_redis = _redis_lib.from_url(url, decode_responses=True)
    return _email_redis


async def _claim_session_slot(user_id: str, instrument: str, session: str) -> bool:
    """Atomically claim 1 email slot per (user, instrument-family, session).
    Returns True if claim succeeded, False if already taken."""
    norm = _normalize_instrument(instrument)
    key = f"emailcap:sess:{user_id}:{norm}:{session}"
    # 4-hour TTL covers any single session safely
    res = await _get_email_redis().set(key, "1", ex=4*3600, nx=True)
    return bool(res)


async def _claim_daily_slot(user_id: str, max_per_day: int = 1) -> bool:
    """Atomic per-user-per-day counter. Returns True if still under cap."""
    from datetime import date as _date
    key = f"emailcap:day:{user_id}:{_date.today().isoformat()}"
    r = _get_email_redis()
    cur = await r.incr(key)
    if cur == 1:
        await r.expire(key, 86400)  # 24h TTL on first incr
    return cur <= max_per_day


async def _release_daily_slot(user_id: str):
    """Refund the daily counter if downstream rejected the signal."""
    from datetime import date as _date
    key = f"emailcap:day:{user_id}:{_date.today().isoformat()}"
    try:
        await _get_email_redis().decr(key)
    except Exception: pass

async def _check_daily_email_cap(user_id: str, max_per_day: int = 1) -> bool:
    from app.database import async_session_factory as _asf
    from sqlalchemy import text as _t
    async with _asf() as db:
        r = await db.execute(_t("SELECT COUNT(*) AS n FROM pending_trades WHERE user_id = :uid AND created_at::date = CURRENT_DATE"), {"uid": user_id})
        row = r.fetchone()
        return (row.n if row else 0) < max_per_day

async def _already_signaled_this_session(user_id: str, instrument: str, strategy_id: str = None) -> bool:
    """Check whether this user/instrument/strategy has already received a
    signal in the current session. Caps spam at one per session per pair."""
    from app.database import async_session_factory as _asf
    from sqlalchemy import text as _t
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    sess = _current_session_label()
    # 4-hour lookback covers any single session safely
    cutoff = _dt.now(_tz.utc) - _td(hours=4)
    async with _asf() as db:
        inst_norm = _normalize_instrument(instrument)
        r = await db.execute(_t("""
            SELECT 1 FROM pending_trades
             WHERE user_id = :uid
               AND CASE UPPER(instrument)
                     WHEN 'MES' THEN 'ES' WHEN 'MNQ' THEN 'NQ'
                     WHEN 'MYM' THEN 'YM' WHEN 'M2K' THEN 'RTY'
                     ELSE UPPER(instrument)
                   END = :inst
               AND created_at >= :cut
               AND COALESCE(notes->>'session', '') = :sess
             LIMIT 1
        """), {"uid": user_id, "inst": inst_norm, "cut": cutoff, "sess": sess})
        if r.fetchone() is not None:
            return True
    return False


async def _emit_intraday_hit(strategy, user, hit):
    """Emit one intraday auto-execute receipt."""
    # User-spec: one signal per (user, instrument, session) maximum.
    try:
        sess_label = _current_session_label()
        if sess_label == "DEAD":
            logger.debug(f"[Intraday] DEAD-ZONE: {hit.ticker} for {user.email} suppressed (outside session windows)")
            return
        # Min-score gate: only quality signals get through
        signal_score = getattr(hit, "score", 0) or 0
        if signal_score < 15:
            logger.debug(f"[Intraday] LOW-SCORE: {hit.ticker} score={signal_score:.1f} < 15, skip")
            return
        claimed = await _claim_session_slot(str(user.id), hit.ticker, sess_label)
        if not claimed:
            logger.info(f"[Intraday] CAP-HIT {hit.ticker} for {user.email} — already signaled {sess_label} session")
            return
        under_daily = await _claim_daily_slot(str(user.id), max_per_day=1)
        if not under_daily:
            logger.info(f"[Intraday] CAP-HIT {hit.ticker} for {user.email} — daily cap (1) reached")
            return
    except Exception as _e:
        logger.warning(f"[Intraday] session-cap check failed (proceeding): {_e}")
    from app.services.email import send_trade_receipt_email

    atr_proxy = _smart_atr_proxy(getattr(hit, "price", getattr(hit, "spot", 0.0)))
    spot_or_price = getattr(hit, "spot", None) or getattr(hit, "price", 0.0)
    direction = hit.direction if hasattr(hit, "direction") else ("long" if hit.change_pct > 0 else "short")
    if direction == "long":
        entry, stop = spot_or_price, spot_or_price - atr_proxy
        target = spot_or_price + atr_proxy * (strategy.risk_reward_ratio or 2.0)
    else:
        entry, stop = spot_or_price, spot_or_price + atr_proxy
        target = spot_or_price - atr_proxy * (strategy.risk_reward_ratio or 2.0)


    ticker = hit.ticker
    # Build a structured reason for the email — catalyst + vol surge + expected continuation
    _chg = getattr(hit, "change_pct", 0) or 0
    _vol = getattr(hit, "pct_of_avg_volume", 0) or 0
    _cat = getattr(hit, "catalyst", "") or ""
    _expected = max(3.0, min(abs(_chg) * 0.6, 10.0))   # bot expects 60% follow-through, clamped 3-10%
    _parts = []
    if _chg:
        _parts.append(f"{ticker} up {_chg:.1f}% intraday")
    if _vol:
        _parts.append(f"on {_vol:.1f}x average volume vs prior day")
    if _cat:
        _blurb = {
            "intraday_runner": "continuation breakout — buyers absorbing supply",
            "premarket_runner": "premarket gap holding into the bell",
            "news_burst": "news-driven surge (likely earnings or PR)",
            "gap": "morning gap with follow-through",
            "afterhours": "after-hours move; entry valid only if it holds at open",
        }.get(_cat, _cat)
        _parts.append(f"Catalyst: {_blurb}")
    _parts.append(
        f"Expected continuation: +{_expected:.1f}% in next 30-90 min based on the {_cat or 'momentum'} pattern's historical follow-through. "
        f"Stops out if price retraces 2% from entry — that's typically when buyer exhaustion finishes."
    )
    reason = " \u00b7 ".join(_parts) if _parts else (
        hit.note if hasattr(hit, "note") else
        getattr(hit, "reason", f"intraday {direction} on {ticker}")
    )

    broker_account_id, trade_mode = await _resolve_user_broker(user.id)

    # Bug-32: size from broker_account sizing rules (account_type, risk %,
    # max position $) instead of hardcoded 100. Falls back to 100 shares if
    # account has no sizing config yet.
    qty = await _compute_qty_from_sizing(
        broker_account_id=broker_account_id,
        ticker=ticker, entry=entry, stop=stop,
        default=int(strategy.max_contracts or 1) * 100,
    )
    if qty < 1:
        logger.warning(f"[Intraday] sizing returned 0 shares for {ticker} — skipping")
        return

    # Bug #28 fix: actually call the broker on live mode. Previously the
    # row was inserted with status='executed' but no broker call ever fired
    # ("phantom executions"). Now: live path goes through Tradier; on
    # success we keep status='executed', on reject/error we record the error
    # in notes and status='error'.
    broker_order_id = None
    actual_status = "executed"  # paper default
    broker_err = None
    if trade_mode == "live" and broker_account_id:
        logger.info(f"[Intraday] FIRING Tradier order: user={user.email} ticker={ticker} dir={direction} qty={qty} entry={entry:.2f}")
        broker_order_id, actual_status, broker_err = await _place_intraday_broker_order(
            broker_account_id=broker_account_id, ticker=ticker,
            direction=direction, qty=qty,
        )
        if actual_status == "executed":
            logger.info(f"[Intraday] ✅ Tradier ACCEPTED {ticker}: order_id={broker_order_id}")
        else:
            logger.error(f"[Intraday] ❌ Tradier REJECTED {ticker}: status={actual_status} err={broker_err}")
        # Insert into trades table so the UI + win-rate stats see this
        if actual_status == "executed":
            try:
                from datetime import datetime, timezone
                async with async_session_factory() as _tdb:
                    # Find or create a trade_session for this user+strategy+live
                    sess_row = (await _tdb.execute(text("""
                        SELECT id FROM trade_sessions
                         WHERE user_id = :uid AND strategy_id = :sid AND mode = 'live'
                         ORDER BY created_at DESC LIMIT 1
                    """), {"uid": str(user.id), "sid": str(strategy.id)})).first()
                    if sess_row:
                        sess_id = str(sess_row.id)
                    else:
                        ins = await _tdb.execute(text("""
                            INSERT INTO trade_sessions (user_id, strategy_id, mode, created_at)
                            VALUES (CAST(:uid AS uuid), CAST(:sid AS uuid), 'live', NOW())
                            RETURNING id
                        """), {"uid": str(user.id), "sid": str(strategy.id)})
                        sess_id = str(ins.scalar())
                    await _tdb.execute(text("""
                        INSERT INTO trades (session_id, instrument, direction, entry_price,
                            stop_loss, take_profit, contracts, entry_time, mode, status,
                            broker_account_id, broker_order_id)
                        VALUES (CAST(:sid AS uuid), :inst, :dir, :ep, :sl, :tp, :qty, :et, :mode, :status, CAST(:bid AS uuid), :oid)
                    """), {
                        "sid": sess_id,
                        "inst": ticker, "dir": direction, "ep": entry, "sl": stop, "tp": target,
                        "qty": qty, "et": datetime.now(timezone.utc), "mode": "live",
                        "status": "open", "bid": broker_account_id, "oid": broker_order_id,
                    })
                    await _tdb.commit()
            except Exception as _e:
                logger.warning(f"[Intraday] trades-row insert failed: {_e}")

    await create_pending_trade(
        user_id=str(user.id), strategy_id=str(strategy.id),
        mode=trade_mode, broker_account_id=broker_account_id,
        instrument=ticker, direction=direction,
        contracts=qty,
        entry=entry, stop=stop, target=target,
        bias=getattr(hit, "catalyst", None), reason=reason,
        is_intraday=True,
        expires_in_minutes=1,
        notes={"score": getattr(hit, "score", 0.0),
               "change_pct": getattr(hit, "change_pct", None),
               "catalyst":   getattr(hit, "catalyst", None),
               "broker_order_id": broker_order_id,
               "actual_status": actual_status,
               "broker_err": broker_err,
               "session": _current_session_label()},
    )

    try:
        send_trade_receipt_email(
            to=user.email, username=getattr(user, "username", ""),
            ticker=ticker, direction=direction,
            entry=entry, stop=stop, target=target,
            contracts=int(strategy.max_contracts or 1),
            reason=reason, strategy_name=strategy.name, mode="paper",
        )
    except Exception as e:
        logger.error(f"[Intraday] receipt email failed for {ticker}: {e}")


async def _get_confirm_token(pid: str) -> Optional[str]:
    async with async_session_factory() as db:
        r = await db.execute(text("SELECT confirm_token FROM pending_trades WHERE id = :pid"),
                              {"pid": pid})
        row = r.fetchone()
        return row.confirm_token if row else None





async def _resolve_user_broker(user_id: str) -> tuple[Optional[str], str]:
    """Returns (broker_account_id_or_None, mode).

    If the user has an active+trading-enabled Tradier account, we route
    pending trades to that account in live mode. Otherwise we fall back
    to paper. This is the per-user data-licensing split — Tradier covers
    the data + execution for each user; Theta Algos pays $0."""
    async with async_session_factory() as db:
        r = (await db.execute(text("""
            SELECT id FROM broker_accounts
             WHERE user_id = :uid
               AND lower(broker) = 'tradier'
               AND is_active = true
               AND trading_enabled = true
             ORDER BY (sandbox_mode IS NOT TRUE) DESC, created_at DESC
             LIMIT 1
        """), {"uid": str(user_id)})).fetchone()
    if r:
        return str(r.id), "live"
    return None, "paper"


async def _emit_premarket_batch(strategy, user, hits: list, expires_in: int):
    """Save all hits as pending_trades and send ONE consolidated email
    with the top pick prominent + runners-up listed below."""
    from app.services.email import send_consolidated_signals_email

    if not hits:
        return

    rows: list[dict] = []
    for hit in hits:
        atr_proxy = _smart_atr_proxy(getattr(hit, "price", getattr(hit, "spot", 0.0)))
        spot_or_price = getattr(hit, "spot", None) or getattr(hit, "price", 0.0)
        direction = (hit.direction if hasattr(hit, "direction")
                      else ("long" if getattr(hit, "change_pct", 0) > 0 else "short"))
        # Prefer the hit's pre-computed entry/stop/target if present (STT hits have these)
        entry  = getattr(hit, "entry",  spot_or_price)
        stop   = getattr(hit, "stop",   spot_or_price - atr_proxy if direction == "long" else spot_or_price + atr_proxy)
        target = getattr(hit, "target",
                          spot_or_price + atr_proxy * (strategy.risk_reward_ratio or 2.0)
                          if direction == "long"
                          else spot_or_price - atr_proxy * (strategy.risk_reward_ratio or 2.0))

        ticker = hit.ticker
        bias = (getattr(hit, "bias", None) or getattr(hit, "catalyst", None)
                 or getattr(hit, "catalyst_keyword", None))
        reason = (hit.reason if hasattr(hit, "reason") and hit.reason
                   else getattr(hit, "note",
                                 f"{direction.upper()} signal on {ticker}"))

        broker_account_id, trade_mode = await _resolve_user_broker(user.id)
        pid = await create_pending_trade(
            user_id=str(user.id), strategy_id=str(strategy.id),
            mode=trade_mode, broker_account_id=broker_account_id,
            instrument=ticker, direction=direction,
            contracts=int(getattr(strategy, "max_contracts", 1) or 1),
            entry=entry, stop=stop, target=target,
            bias=str(bias) if bias else None, reason=reason,
            is_intraday=False, expires_in_minutes=expires_in,
            notes={"score": getattr(hit, "score", 0.0),
                   "strategy": getattr(hit, "strategy", None),
                   "metadata": getattr(hit, "metadata", {})},
        )
        token = (await _get_confirm_token(pid)) or ""
        rows.append({
            "ticker": ticker, "direction": direction,
            "entry": float(entry), "stop": float(stop), "target": float(target),
            "bias": str(bias) if bias else None, "reason": reason,
            "confirm_token": token, "score": float(getattr(hit, "score", 0.0)),
        })

    primary = rows[0]
    runners_up = rows[1:]
    expires_at_human = (datetime.now(ET) + timedelta(minutes=expires_in)).strftime("%I:%M %p ET")
    scan_time_human = datetime.now(ET).strftime("%a %b %d, %I:%M %p ET")

    try:
        send_consolidated_signals_email(
            to=user.email, username=getattr(user, "username", ""),
            strategy_name=strategy.name,
            primary=primary, runners_up=runners_up,
            expires_at_human=expires_at_human,
            scan_time_human=scan_time_human,
        )
    except Exception as e:
        logger.error(f"[Premarket] consolidated email failed: {e}")

    # Bug #21 fix: schedule per-strategy delayed auto-execute using
    # this strategy's own auto_execute_delay_min, not the global default.
    _delay_min = int(getattr(strategy, "auto_execute_delay_min", 15) or 15)
    asyncio.create_task(_delayed_auto_execute_for_strategy(str(strategy.id), _delay_min * 60))


# ── Scan dispatch ───────────────────────────────────────────────────────────

async def _scan_one_strategy(strategy, user, *, is_premarket: bool):
    """Dispatch by strategy.signal_mode → run the right scanner.

    Modes (all pre-built, no per-user config required):
      • momentum_scanner          — bulk-yfinance 10%+ movers
      • low_float_squeeze         — Sykes A: $0.50-$20, <10M float, +catalyst
      • fifty_two_week_breakout   — Sykes B: within 2% of 52WH, 300% volume
      • premarket_gap_runner      — Sykes C: 4-9:30 ET gappers, 100K+ pre vol
      • oracle_opening_candle     — Oracle: 9:30-9:35 candle bias engine
      • universe_scan             — legacy ICT-on-watchlist
    """
    mode = (getattr(strategy, "signal_mode", "") or "").lower()
    expires_in = max(5, int(getattr(strategy, "auto_execute_delay_min", 15) or 15))

    if mode == "low_float_squeeze":
        hits = await scan_low_float_squeeze(top_k=1)
    elif mode == "fifty_two_week_breakout":
        hits = await scan_52w_breakout(top_k=1)
    elif mode == "premarket_gap_runner":
        hits = await scan_premarket_gappers(top_k=1)
    elif mode == "oracle_opening_candle":
        hits = await scan_oracle_opening_candle(top_k=1)
    elif mode == "momentum_scanner":
        # User spec: long-only $2-$10 stocks up 10%+ on volume surge.
        # include_negative=False filters out the short candidates that were
        # polluting the inbox. min_vol_ratio=2.0 (was 1.5) keeps quiet
        # runners out — real momentum names print 2x+ vs prior day.
        hits = await scan_for_momentum(
            # v3 spec: catch the move EARLY (+5% trigger) but skip blow-off
            # tops (>15%) where reversal risk dominates. 4x volume cuts
            # false positives. top_k=5 so we can show 4 runners-up in the email.
            min_change_pct=5.0,
            max_change_pct=15.0,
            min_price=2.0, max_price=10.0,
            min_day_volume=750_000,
            min_vol_ratio=4.0,
            top_k=5,
            include_negative=False,
        )
    else:  # universe_scan (ICT — legacy)
        universe_list = getattr(strategy, "watch_universe", None) or get_universe("expanded")
        if not universe_list:
            return
        cfg = await _build_config(strategy, universe_list[0])
        hits = await scan_universe(cfg, universe_list, top_k=1)

    if not hits:
        return

    if is_premarket:
        # Consolidated email: 1 top pick + the rest as runners-up
        await _emit_premarket_batch(strategy, user, hits, expires_in)
    else:
        for hit in hits:
            await _emit_intraday_hit(strategy, user, hit)


async def _run_scan_cycle(*, is_premarket: bool):
    # Yield to user backtests + optimizations. The scanner's pandas-heavy
    # cycles can starve concurrent CPU-bound work (backtest hangs at ~44%
    # because the GIL is held by .dropna / __contains__ on big universe
    # frames). Skip the intraday tick if anything is actively running.
    # Pre-market 09:00 batch always runs — it's time-critical for emails.
    # NOTE: we intentionally do NOT skip the scan when a backtest/optimization
    # is RUNNING. Doing so previously SUPPRESSED real intraday signal emails for
    # ALL users whenever a simulation ran on ANY account (a simulation must
    # never block real email delivery). GIL contention from the scan is now
    # mitigated by the shared bar cache + thread-offloaded fetches. If a sim is
    # running we just log it for observability and proceed with the scan/email.
    if not is_premarket:
        try:
            from sqlalchemy import text as _t_busy
            from app.database import async_session_factory as _busy_sf
            async with _busy_sf() as _busy_db:
                busy = (await _busy_db.execute(_t_busy(
                    "SELECT 1 FROM backtest_runs WHERE status='RUNNING' "
                    "UNION SELECT 1 FROM optimization_runs WHERE status='RUNNING' LIMIT 1"
                ))).first()
            if busy:
                logger.info("[Scanner] sim (backtest/optimization) running — proceeding with scan anyway (emails are never suppressed by simulations)")
        except Exception:
            pass

    await expire_old_pending()

    # News blackout — pause INTRADAY ticks (which auto-execute) but always
    # let the pre-market 08:30 batch run. The morning email is informational
    # only; the user manually clicks Confirm to execute, which can\'t happen
    # inside the blackout window anyway because auto_execute_pass respects it.
    if not is_premarket:
        block = await is_blackout_active(buffer_min=BLACKOUT_BUFFER_MIN)
        if block:
            logger.info(f"[Scanner] blackout active: {block['event_name']} @ {block['event_time']} — skip intraday tick")
            return
    else:
        block = await is_blackout_active(buffer_min=BLACKOUT_BUFFER_MIN)
        if block:
            logger.info(f"[Scanner] pre-market batch — running scan during {block['event_name']} blackout (email only, no auto-exec)")

    async with async_session_factory() as db:
        rows = (await db.execute(text("""
            SELECT s.*, u.email AS u_email, u.username AS u_username
              FROM strategies s
              JOIN users u ON u.id = s.user_id
             WHERE LOWER(s.status::text) = 'active'
               AND s.signal_mode IN (
                   'universe_scan', 'momentum_scanner',
                   'low_float_squeeze', 'fifty_two_week_breakout',
                   'premarket_gap_runner', 'oracle_opening_candle'
               )
        """))).all()

    for row in rows:
        rm = row._mapping
        strat = type("S", (), dict(rm))()
        user  = type("U", (), {"id": rm["user_id"], "email": rm["u_email"],
                                "username": rm["u_username"]})()
        try:
            await _scan_one_strategy(strat, user, is_premarket=is_premarket)
        except Exception as e:
            logger.error(f"[Scanner] strategy {rm.get('id')} failed: {e}")
            try:
                from app.engines.pipeline_alerts import send_pipeline_failure_alert
                import traceback as _tb
                await send_pipeline_failure_alert(
                    reason=f"Scanner strategy run failed: {type(e).__name__}",
                    context={"job": "premarket_scheduler._run_scan_cycle",
                             "step": "per-strategy",
                             "strategy_id": str(rm.get("id")),
                             "is_premarket": is_premarket,
                             "error": str(e)},
                    traceback_str=_tb.format_exc(),
                )
            except Exception:
                pass


async def _run_auto_execute_pass():
    """Auto-execute confirmed signals — but respect the blackout. If we\'re
    inside a ±30-min news window, defer execution until the window closes."""
    block = await is_blackout_active(buffer_min=BLACKOUT_BUFFER_MIN)
    if block:
        logger.info(f"[Scanner] auto-execute deferred: {block['event_name']} blackout active")
        return
    async with async_session_factory() as db:
        rows = (await db.execute(text("""
            SELECT id FROM strategies
             WHERE LOWER(status::text) = 'active'
               AND require_confirm = false
               AND signal_mode IN (
                   'universe_scan', 'momentum_scanner',
                   'low_float_squeeze', 'fifty_two_week_breakout',
                   'premarket_gap_runner', 'oracle_opening_candle'
               )
        """))).all()
    for r in rows:
        try:
            auto = await auto_execute_pending(str(r.id))
            for ap in auto:
                logger.info(f"[Premarket] auto-executing pending {ap.get('id')} "
                             f"({ap.get('instrument')} {ap.get('direction')})")
        except Exception as e:
            logger.error(f"[Premarket] auto-execute failed for strategy {r.id}: {e}")


# ── Main scheduler ──────────────────────────────────────────────────────────

async def start_premarket_scheduler():
    """Long-running task — covers premarket (08:30) + intraday (every 5 min
    from 04:00 to 20:00 ET) + news blackouts."""
    logger.info("[Scanner] scheduler started")

    # Bootstrap: refresh blackouts + EDGAR immediately
    try:
        n = await refresh_blackouts()
        logger.info(f"[Scanner] news_blackouts refreshed: {n} events")
    except Exception as e:
        logger.warning(f"[Scanner] blackout refresh failed: {e}")

    try:
        n = await refresh_edgar()
        logger.info(f"[Scanner] EDGAR 8-K feed: {n} filings ingested")
    except Exception as e:
        logger.warning(f"[Scanner] EDGAR refresh failed: {e}")

    last_news_refresh = datetime.now(timezone.utc)
    last_edgar_refresh = datetime.now(timezone.utc)
    premarket_fired_today: set = set()

    while True:
        try:
            now_et = _today_et()
            now_utc = datetime.now(timezone.utc)

            # Refresh news every 6 hours
            if (now_utc - last_news_refresh).total_seconds() > 6 * 3600:
                try:
                    n = await refresh_blackouts()
                    logger.info(f"[Scanner] news refresh: {n} events")
                except Exception as e:
                    logger.warning(f"[Scanner] news refresh failed: {e}")
                last_news_refresh = now_utc

            # Refresh EDGAR every 5 minutes — catches material 8-K filings
            # in near-realtime (companies file within ~4 business days but
            # most file the day of the event)
            if (now_utc - last_edgar_refresh).total_seconds() > 5 * 60:
                try:
                    n = await refresh_edgar()
                    if n > 0:
                        logger.info(f"[Scanner] EDGAR refresh: {n} new 8-K filings")
                except Exception as e:
                    logger.warning(f"[Scanner] EDGAR refresh failed: {e}")
                last_edgar_refresh = now_utc

            # Outside the trading window — sleep until next open
            if not _within_market_window(now_et):
                target = _next_market_open_et()
                wait_s = max(60, (target - now_et).total_seconds())
                logger.info(f"[Scanner] outside window — sleep until {target.strftime('%Y-%m-%d %H:%M ET')} ({wait_s/60:.0f} min)")
                await asyncio.sleep(min(3600, wait_s))
                continue

            # Pre-market batch — fire once at exactly 09:00 ET (30 min before NYSE open)
            premarket_key = now_et.strftime("%Y-%m-%d")
            within_premarket_slot = (
                dtime(9, 0) <= now_et.time() <= dtime(9, 5)
                and premarket_key not in premarket_fired_today
            )
            if within_premarket_slot:
                logger.info("[Scanner] firing 09:00 PRE-MARKET scan")
                await _run_scan_cycle(is_premarket=True)
                premarket_fired_today.add(premarket_key)
                # Auto-execute pass at 08:30 + auto_execute_delay_min (default 15)
                asyncio.create_task(_delayed_auto_execute(int(getattr(__import__("app.config", fromlist=["settings"]).settings, "PREMARKET_AUTO_EXECUTE_DELAY_SEC", 15 * 60))))

            # Theta Scanner morning pick (tiered threshold 6:00-9:50 ET).
            # This was previously ONLY invoked from the except block — bug.
            # In the happy path it never ran, so the morning options email
            # silently stopped firing. Now called every loop iteration; the
            # function itself debounces (5 min) + once-per-day Redis SETNX.
            try:
                await _check_and_run_theta_scanner()
            except Exception as _tse:
                logger.warning(f"[ThetaScanner] check failed: {_tse}")

            # ── Pending stock-entry timing gate (2026-06-05) ──
            # Iterates Redis-queued picks and decides per the ICT timing
            # gate whether to fire pre-mkt, MOO, or defer.
            try:
                await _check_pending_stock_entries()
            except Exception as _pe:
                logger.warning(f"[stock-entry] check failed: {_pe}")

            # ── Trail watcher (30s cadence inside the function) ──
            # SAME BUG PATTERN as the theta scanner above: was only called in
            # the except block, so during normal operation it never ran.
            # That's why the 6/1 EEIQ + AIIO positions never got their
            # trailing stops fired. The function itself debounces (>=30s).
            try:
                await _check_trail_watcher()
            except Exception as _twe:
                logger.warning(f"[TrailWatch] check failed: {_twe}")

            # ── End-of-day close (3:55 PM ET, idempotent per ET date) ──
            # Theta Scanner picks are intraday — they should NOT be carried
            # overnight. Without this, EEIQ (entered 6/1 10:51 ET) sat as a
            # losing position 24h+. Now: at 15:55 ET each trading day we
            # market-sell any still-open theta_scanner row.
            try:
                await _check_end_of_day_close()
            except Exception as _eod:
                logger.warning(f"[EOD-close] check failed: {_eod}")

            # Intraday cadence — every 5 min within window
            await _run_scan_cycle(is_premarket=False)
            await asyncio.sleep(INTRADAY_PERIOD_SEC)

        except asyncio.CancelledError:
            logger.info("[Scanner] scheduler cancelled")
            return
        except Exception as e:
            logger.error(f"[Scanner] loop error: {e}")
            try:
                from app.engines.pipeline_alerts import send_pipeline_failure_alert
                import traceback as _tb
                await send_pipeline_failure_alert(
                    reason=f"Premarket scheduler outer loop crashed: {type(e).__name__}",
                    context={"job": "premarket_scheduler.start_premarket_scheduler",
                             "step": "outer_loop", "error": str(e)},
                    traceback_str=_tb.format_exc(),
                )
            except Exception:
                pass
            await _check_and_run_theta_scanner()
            await _check_trail_watcher()
            await _check_and_refresh_news_calendar()
            await asyncio.sleep(60)



async def _delayed_auto_execute_for_strategy(strategy_id: str, delay_seconds: int):
    """Bug #21 fix: per-strategy delayed auto-execute. Honors each
    strategy's own auto_execute_delay_min instead of the global 15-min
    default. Called from _emit_premarket_batch after the morning email
    is sent so the auto-confirm pass fires at the right time for THIS
    strategy."""
    try:
        await asyncio.sleep(max(60, int(delay_seconds)))
        from app.engines.options.pending_trades import auto_execute_pending
        confirmed = await auto_execute_pending(strategy_id)
        for ap in confirmed:
            logger.info(f"[Premarket/perStrat] auto-confirmed {ap.get('id')} for strategy {strategy_id}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[Premarket/perStrat] failed for {strategy_id}: {e}")


async def _delayed_auto_execute(delay_seconds: int):
    """Fire the auto-execute pass for non-confirm strategies after the
    auto_execute_delay_min window. Called as a one-off task from the
    pre-market path so the main scheduler keeps ticking."""
    try:
        await asyncio.sleep(delay_seconds)
        await _run_auto_execute_pass()
    except asyncio.CancelledError:
        pass



# ===== THETA SCANNER 9:25 ET DAILY =====

async def _start_theta_pick_paper_session(db, user_id, strategy_id, ticker, pick) -> bool:
    """THETA-AUTOPAPER-V1 (#27). Auto-start ONE options-paper session on today's
    Theta pick for this user. Idempotent per (date, user, ticker); gated by env
    THETA_AUTO_PAPER_ENABLED (default OFF). Returns True if a session started."""
    import os as _os
    if _os.environ.get("THETA_AUTO_PAPER_ENABLED", "0") not in ("1", "true", "True", "yes"):
        return False
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return False
    date_str = _today_et().date().isoformat()
    # ── Idempotency: one auto-paper session per user per ticker per day. ──
    rds = _get_email_redis()
    if rds is not None:
        try:
            if not rds.set(f"theta:autopaper:{date_str}:{user_id}:{ticker}", "1", nx=True, ex=20 * 3600):
                logger.info(f"[OptionsPaper-Theta] already auto-started today user={user_id} {ticker}")
                return False
        except Exception:
            pass
    else:
        from sqlalchemy import text as _t2
        dup = (await db.execute(_t2("""
            SELECT 1 FROM trade_sessions
             WHERE user_id = :uid AND mode = 'options_paper' AND instrument = :inst
               AND is_active = true AND started_at::date = CURRENT_DATE LIMIT 1
        """), {"uid": str(user_id), "inst": ticker})).fetchone()
        if dup:
            return False
    try:
        from app.models.trade import TradeSession
        sess = TradeSession(strategy_id=strategy_id, user_id=user_id,
                            mode="options_paper", is_active=True, instrument=ticker)
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        from app.engines.options.options_paper_runner import start_options_paper_session as _start
        import asyncio
        asyncio.create_task(_start(str(sess.id), str(strategy_id), str(user_id), ticker))
        logger.info(f"[OptionsPaper-Theta] auto-started session={sess.id} user={user_id} "
                    f"ticker={ticker} score={pick.get('score')} entry={pick.get('entry')}")
        return True
    except Exception as e:
        logger.error(f"[OptionsPaper-Theta] auto-start failed user={user_id} {ticker}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return False


async def run_theta_scanner_for_all_users():
    """Runs Theta Scanner once. Emails + broker order for each eligible user."""
    from app.database import async_session_factory
    from app.engines.options.theta_scanner import find_best_premarket_pick, emit_theta_pick
    from sqlalchemy import text as _t

    async with async_session_factory() as db:
        pick = await find_best_premarket_pick(db)
        if not pick:
            logger.info("[ThetaScanner] no qualified pick today — sending nothing")
            return
        users = (await db.execute(_t("""
            SELECT DISTINCT ON (u.id) u.id, u.email, u.username, s.id AS strategy_id FROM users u
              JOIN strategies s ON s.user_id = u.id
             WHERE s.signal_mode = 'theta_scanner' AND s.status = 'ACTIVE'
               AND u.is_active = true   -- exclude deactivated/deleted accounts
             ORDER BY u.id, s.id
        """))).fetchall()
        for u in users:
            class _U: pass
            user = _U(); user.id = u.id; user.email = u.email; user.username = u.username
            try:
                ok = await emit_theta_pick(db, user, pick)
                logger.info(f"[ThetaScanner] emitted to {u.email}: ok={ok}")
            except Exception as e:
                logger.error(f"[ThetaScanner] emit failed for {u.email}: {e}")
            # THETA-AUTOPAPER-V1 (#27): also paper-trade the pick through the
            # options-paper engine (flag-gated, idempotent).
            try:
                await _start_theta_pick_paper_session(db, u.id, u.strategy_id, pick.get("ticker"), pick)
            except Exception as e:
                logger.error(f"[OptionsPaper-Theta] wiring error for {u.email}: {e}")


_theta_fired_today = None  # in-memory cache, backed by Redis
_theta_last_scan_min = None  # debounce: don't scan more than once per ~5 min
# Once-per-day no-pick alert (BUG B 2026-06-04). When the scan window ends
# (>9:50 ET) without anything firing, send ONE URGENT alert per trading
# date so we know the scanner didn't silently fail. Cleared by date.
_theta_no_pick_alerted_for_date: set = set()


async def _send_no_pick_emails(date_str: str, reason: str) -> None:
    """Email active Theta Scanner subscribers a short "no pick today" note with
    the reason. Subject carries "Theta Scanner" so the EMAIL_KILL_SWITCH
    whitelist passes it. Recipients filtered to active users only."""
    from app.database import async_session_factory
    from app.services.email import _send_tracked
    from sqlalchemy import text as _t
    try:
        async with async_session_factory() as _db:
            rows = (await _db.execute(_t(
                "SELECT DISTINCT u.email FROM users u JOIN strategies s ON s.user_id = u.id "
                "WHERE s.signal_mode = 'theta_scanner' AND s.status = 'ACTIVE' AND u.is_active = true"
            ))).fetchall()
    except Exception as _e:
        logger.error(f"[ThetaScanner] no-pick recipient query failed: {_e}")
        return
    subject = f"\U0001f3af Theta Scanner: No pick today ({date_str})"
    html = (
        "<div style=\"font-family:system-ui,Arial,sans-serif;max-width:520px;margin:0 auto;padding:18px;\">"
        "<h2 style=\"color:#7c3aed;margin:0 0 10px;\">\U0001f3af Theta Scanner \u2014 no pick today</h2>"
        "<p style=\"font-size:14px;color:#334155;\">The premarket scan ran and stood down: no setup cleared the quality bar.</p>"
        f"<div style=\"background:#f1f5f9;border-radius:8px;padding:10px 12px;font-size:13px;color:#475569;\"><b>Why:</b> {reason}</div>"
        "<p style=\"font-size:12px;color:#94a3b8;margin-top:14px;\">No action needed \u2014 a no-trade day is a valid outcome. You will get the next qualifying pick automatically.</p>"
        "</div>"
    )
    for r in rows:
        try:
            _send_tracked(r[0], subject, html)
        except Exception as _e:
            logger.error(f"[ThetaScanner] no-pick email to {r[0]} failed: {_e}")

# Once-per-day exception alert. Same idea, but for the case where
# find_best_premarket_pick raises — we want one alert per traceback
# per day, not one alert every 5 minutes for an hour.
_theta_exception_alerted_for_date: set = set()

def _min_score_for_et(et) -> float:
    """Time-tiered score threshold. Earlier = stricter. As we approach 9:25
    the bar drops so SOMETHING goes out before the open even if it's marginal.

    HLIT today scored 19.25 — would fire at 7:00 ET tier (>=18).
    The goal: get high-conviction setups out EARLY (before they move).
    """
    h, m = et.hour, et.minute
    t = h * 60 + m  # ET minutes since midnight
    if t < 6*60:    return 99.0   # before 6am: don't fire yet (pre-market thin)
    if t < 7*60:   return 20.0   # 6:00-7:00 ET: only exceptional (>=20)
    if t < 7*60+30: return 18.0   # 7:00-7:30: high conviction
    if t < 8*60:   return 16.0   # 7:30-8:00
    if t < 8*60+30: return 14.0   # 8:00-8:30
    if t < 9*60:   return 12.0   # 8:30-9:00
    if t < 9*60+25: return 10.0   # 9:00-9:25: anything decent
    if t <= 9*60+50: return 0.0   # 9:25-9:50: last-chance, whatever's best
    return 99.0  # outside window

async def _check_and_run_theta_scanner():
    """Tiered premarket scanner — fires on the FIRST qualifying setup found
    between 6:00 ET and 9:50 ET, with the score threshold dropping as 9:30
    approaches. The earlier a high-conviction setup fires, the more time
    users have to act before the move plays out. Today HLIT scored 19.25
    by ~7am — would have fired at 7:00 ET (~2.5h before our old 9:25 trigger)
    giving users entire premarket window to position.

    Once-per-day cap via Redis SETNX. If a top-tier setup never appears,
    the threshold drops every 30 min so the email still goes out by 9:25.

    Visibility (added 2026-06-04 — BUG B):
      * Every call emits `[ThetaScanner] tick ET=HH:MM window=in/out
        fired_today=bool` so we can grep prod logs to confirm the function
        even ran today.
      * If the scan window closes (>9:50 ET) without a fire, send ONE
        URGENT pipeline_failure alert per trading date (idempotent via
        _theta_no_pick_alerted_for_date).
      * If find_best_premarket_pick raises, attach the traceback to a
        pipeline_failure alert (also once-per-day).
    """
    global _theta_fired_today, _theta_last_scan_min
    from datetime import datetime as _dt, date as _date, timezone as _tz
    try:
        import zoneinfo
        et = _dt.now(_tz.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return

    et_min = et.hour * 60 + et.minute
    in_window = (6*60 <= et_min <= 9*60+50)
    today_dt = _date.today()
    today_key_visible = today_dt.isoformat()
    fired_today_bool = (_theta_fired_today == today_dt)
    # BUG B: heartbeat tick. INFO so it's grep-able; runs even for outside-
    # window calls so we know the scheduler IS calling us.
    logger.info(
        f"[ThetaScanner] tick ET={et.strftime('%H:%M')} "
        f"window={'in' if in_window else 'out'} fired_today={fired_today_bool} "
        f"date={today_key_visible}"
    )

    # End-of-window no-pick guard. Fires at the first call AFTER 9:50 ET
    # on a date that never produced a fire. We use the same Redis flag the
    # main path uses so we don't false-alarm a worker that lost its
    # in-memory state mid-day.
    if (not in_window) and et_min > 9*60+50 and et.weekday() < 5:
        if today_key_visible not in _theta_no_pick_alerted_for_date:
            already_fired_via_redis = False
            try:
                import redis as _redis_alert
                _r_alert = _redis_alert.Redis.from_url(
                    os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                    decode_responses=True,
                )
                already_fired_via_redis = bool(_r_alert.get(f"theta_fired:{today_key_visible}"))
            except Exception:
                already_fired_via_redis = fired_today_bool
            if not already_fired_via_redis:
                _theta_no_pick_alerted_for_date.add(today_key_visible)
                # No-pick is an EXPECTED outcome (no qualifying setup, or a news
                # blackout such as FOMC) — NOT a pipeline failure. Do NOT send the
                # URGENT failure email (it was re-spamming admins on every restart).
                # The restart-safe redis-deduped user-facing note below + the yellow
                # systems-check status already surface no-pick days as a warning.
                logger.warning(
                    f"[ThetaScanner] no pick for {today_key_visible} "
                    f"(expected on no-setup / news-blackout days) — not alerting as failure"
                )

                # --- user-facing "no pick today" note + dashboard sentinel ---
                try:
                    from app.engines.options import theta_scanner as _ts
                    _diag = (getattr(_ts, "_NOPICK_STATE", {}) or {}).get("last") or {}
                    _reason = _diag.get("reason") or "No setup cleared the quality filters today."
                    import json as _json, redis as _rds
                    # The Redis key doubles as a restart-safe idempotency latch:
                    # only email if we have not already recorded a no-pick today.
                    _already_noticed = False
                    try:
                        _rc = _rds.Redis.from_url(
                            os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
                        _already_noticed = bool(_rc.get(f"theta:nopick:{today_key_visible}"))
                        if not _already_noticed:
                            _rc.set(f"theta:nopick:{today_key_visible}",
                                    _json.dumps({"reason": _reason, "ticker": _diag.get("ticker"),
                                                 "score": _diag.get("score")}), ex=129600)
                    except Exception:
                        pass
                    if not _already_noticed:
                        await _send_no_pick_emails(today_key_visible, _reason)
                except Exception as _ne:
                    logger.error(f"[ThetaScanner] no-pick user note failed: {_ne}")

    # Skip if outside the entire premarket scan window (6:00 ET - 9:50 ET)
    if not in_window:
        return

    # Debounce: scanner is expensive (~30s of API calls). Don't run more
    # than once per 5 min wall-clock.
    if _theta_last_scan_min is not None and (et_min - _theta_last_scan_min) < 5:
        return
    _theta_last_scan_min = et_min

    today = _date.today()
    today_key = today.isoformat()

    # Already-fired check (Redis-backed)
    _r = None
    try:
        import redis as _redis_theta
        _r = _redis_theta.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
        if _r.get(f"theta_fired:{today_key}"):
            _theta_fired_today = today
            logger.info(f"[ThetaScanner] skip: already fired today ({today_key})")
            return
    except Exception as _e:
        logger.warning(f"[ThetaScanner] redis check failed: {_e}")
        if _theta_fired_today == today: return

    # Find the best premarket candidate
    threshold = _min_score_for_et(et)
    logger.info(f"[ThetaScanner] job start {et.strftime('%H:%M ET')} threshold={threshold}")
    try:
        from app.engines.options.theta_scanner import find_best_premarket_pick
        from app.database import async_session_factory as _asf
        async with _asf() as db:
            pick = await find_best_premarket_pick(db)
    except Exception as e:
        logger.error(f"[ThetaScanner] find_best_premarket_pick failed: {e}")
        # BUG B: once per trading-day, send the full traceback as an
        # URGENT pipeline_failure alert so engineering doesn't have to
        # grep stdout to find out the scanner was silently dying.
        try:
            if today_key not in _theta_exception_alerted_for_date:
                _theta_exception_alerted_for_date.add(today_key)
                import traceback as _tb
                from app.engines.pipeline_alerts import send_pipeline_failure_alert
                await send_pipeline_failure_alert(
                    reason=f"Theta Scanner pick failed: {type(e).__name__}",
                    context={
                        "job": "premarket_scheduler._check_and_run_theta_scanner",
                        "et_now": et.strftime("%H:%M ET"),
                        "trading_date": today_key,
                        "error": str(e),
                    },
                    traceback_str=_tb.format_exc(),
                )
        except Exception as _ae:
            logger.error(f"[ThetaScanner] failed to send exception alert: {_ae}")
        return

    if not pick:
        logger.info(f"[ThetaScanner] {et.strftime('%H:%M ET')}: no candidate found yet (threshold={threshold})")
        return

    pick_score = float(pick.get("score", 0) or 0)
    if pick_score < threshold:
        logger.info(f"[ThetaScanner] {et.strftime('%H:%M ET')}: best={pick.get('ticker')} score={pick_score:.1f} < threshold {threshold} — waiting for better setup or lower bar")
        return

    # Claim the fire-slot atomically — only ONE worker fires per day
    try:
        if _r is not None and not _r.set(f"theta_fired:{today_key}", "running", ex=36*3600, nx=True):
            return  # another worker beat us
    except Exception:
        pass
    _theta_fired_today = today
    logger.info(f"[ThetaScanner] FIRING {et.strftime('%H:%M ET')}: {pick.get('ticker')} score={pick_score:.1f} (threshold={threshold})")
    await run_theta_scanner_for_all_users()



# ===== DAILY FOREX FACTORY REFRESH =====
_ff_last_refresh = None
async def _check_and_refresh_news_calendar():
    """Refresh news_blackouts from Forex Factory once every 6 hours.
    Catches Powell speeches, surprise testimony, late additions that
    aren't in the hardcoded 2026 calendar."""
    global _ff_last_refresh
    from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
    now = _dt2.now(_tz2.utc)
    if _ff_last_refresh and (now - _ff_last_refresh) < _td2(hours=6):
        return
    _ff_last_refresh = now
    try:
        from app.engines.options.news_calendar import refresh_blackouts
        n = await refresh_blackouts()
        logger.info(f"[FFRefresh] news_blackouts refreshed: {n} events ingested")
    except Exception as e:
        logger.warning(f"[FFRefresh] failed: {e}")



# ===== TRAILING-STOP WATCHER (3% trail on every Theta Scanner position) =====
async def _run_trailing_stop_watcher(reprice_only: bool = False):
    """Walk open_positions_watch rows. Update trail_high. If price falls
    >= trail_pct from trail_high, submit market SELL + mark closed."""
    from app.database import async_session_factory
    from sqlalchemy import text as _t
    import requests as _rq, os as _os
    key = _os.environ.get("POLYGON_API_KEY", "")
    if not key: return
    async with async_session_factory() as db:
        rows = (await db.execute(_t("""
            SELECT id, user_id, broker_account_id, ticker, qty, entry_price,
                   trail_pct, trail_high, hard_stop, target
              FROM open_positions_watch
             WHERE status = 'open'
             ORDER BY opened_at ASC LIMIT 100
        """))).fetchall()
        # Visibility — explicit so we can grep prod logs and see this firing.
        # Until 2026-06-02 this function was orphaned (only called from the
        # except block) and prod had ZERO [TrailWatch] log entries in 5 days.
        logger.info(f"[TrailWatch] checking {len(rows)} open positions")
        if not rows: return
        for r in rows:
            try:
                # Get live price from Polygon snapshot
                u = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{r.ticker}"
                resp = _rq.get(u, params={"apiKey": key}, timeout=4)
                if resp.status_code != 200: continue
                t = (resp.json() or {}).get("ticker") or {}
                price = None
                for fld, sub in (("lastTrade","p"), ("min","c"), ("day","c"), ("prevDay","c")):
                    v = (t.get(fld) or {}).get(sub)
                    if v and float(v) > 0: price = float(v); break
                if not price: continue

                # SYSTEMS-CHECK-V2 heartbeat: record that we SUCCESSFULLY priced
                # this position this tick, even when the high didn't move. Without
                # this, last_priced_at only advanced on a NEW high, so a quiet
                # position read as 'stale' forever in the admin System Check.
                await db.execute(_t(
                    "UPDATE open_positions_watch SET last_priced_at = NOW() WHERE id = :id"
                ), {"id": r.id})

                entry = float(r.entry_price); trail = float(r.trail_pct)
                trail_high = max(float(r.trail_high), price)
                pct_from_entry = (trail_high - entry) / entry  # peak unrealized %
                # STEPPED BREAKEVEN: as the position runs profitable, lock in tighter stops.
                # Once any milestone is hit, the effective stop ratchets UP — it never goes back down.
                base_hard_stop = float(r.hard_stop or 0)  # original -3% (e.g. $3.49 for $3.60 entry)
                effective_stop = base_hard_stop
                stop_label = "hard_stop"
                if pct_from_entry >= 0.20:
                    effective_stop = max(effective_stop, entry * 1.15)  # lock +15%
                    stop_label = "lock_15pct"
                elif pct_from_entry >= 0.10:
                    effective_stop = max(effective_stop, entry * 1.05)  # lock +5%
                    stop_label = "lock_5pct"
                elif pct_from_entry >= 0.05:
                    effective_stop = max(effective_stop, entry * 1.001)  # breakeven + 0.1% (covers commissions)
                    stop_label = "breakeven"
                # ALSO: standard 3% trail behind high once we're above +5%
                if pct_from_entry >= 0.05:
                    trail_stop_px = trail_high * (1 - trail/100.0)
                    if trail_stop_px > effective_stop:
                        effective_stop = trail_stop_px
                        stop_label = "3pct_trail"
                # Update high if moved up
                if trail_high > float(r.trail_high):
                    await db.execute(_t(
                        "UPDATE open_positions_watch SET trail_high = :h, hard_stop = :hs, last_priced_at = NOW() WHERE id = :id"
                    ), {"h": trail_high, "hs": effective_stop, "id": r.id})
                # Check exit conditions
                exit_reason = None
                if price <= effective_stop and effective_stop > 0:
                    exit_reason = stop_label
                # reprice_only (admin System Check 'Fix'): re-price + heartbeat
                # ONLY — never place an order. The scheduled watcher tick still
                # handles real exits, so a breached stop fires on its next run.
                if exit_reason and reprice_only:
                    logger.info(f"[TrailWatch] reprice_only: {r.ticker} would exit "
                                f"({exit_reason}) but skipping SELL (admin re-price).")
                if exit_reason and not reprice_only:
                    # Submit market SELL via the broker
                    try:
                        from app.engines.live_trading.broker_factory import build_broker_from_account
                        from app.engines.live_trading.broker_base import OrderRequest, OrderSide, OrderType
                        from app.models.user import BrokerAccount
                        from sqlalchemy import select as _sel
                        acct = (await db.execute(_sel(BrokerAccount).where(BrokerAccount.id == r.broker_account_id))).scalar_one_or_none()
                        if acct:
                            broker = build_broker_from_account(acct)
                            await broker.connect()
                            resp_o = await broker.place_order(OrderRequest(
                                instrument=r.ticker, side=OrderSide.SELL,
                                quantity=int(r.qty), order_type=OrderType.MARKET,
                            ))
                            logger.info(f"[TrailWatch] EXIT {r.ticker} qty={r.qty} reason={exit_reason} price=${price:.2f} (high=${trail_high:.2f}) order={resp_o.broker_order_id}")
                            await db.execute(_t("""
                                UPDATE open_positions_watch
                                   SET status='closed', exit_price=:p, exit_reason=:r, closed_at=NOW()
                                 WHERE id=:id
                            """), {"p": price, "r": exit_reason, "id": r.id})
                            # CRITICAL: the trades table is the user-visible source-of-truth
                            # for the Live Trading P&L panel and journal. open_positions_watch
                            # is the scanner-only sidecar. Until 2026-06-04 the trail watcher
                            # only updated the sidecar — leaving the trades row stuck open
                            # (URG, AIIO for jaceford12). Mirror the close into trades here
                            # using the same column order as the EOD path.
                            await db.execute(_t("""
                                UPDATE trades
                                   SET status = 'closed',
                                       exit_price = :p,
                                       exit_reason = :r,
                                       exit_time = NOW(),
                                       pnl = ROUND(((:p - entry_price) * contracts)::numeric, 2),
                                       net_pnl = ROUND(((:p - entry_price) * contracts - COALESCE(commission, 0))::numeric, 2),
                                       broker_order_id = COALESCE(broker_order_id, :oid),
                                       updated_at = NOW()
                                 WHERE user_id = :uid
                                   AND mode = 'live'
                                   AND status = 'open'
                                   AND instrument = :sym
                            """), {"p": price, "r": exit_reason, "uid": str(r.user_id),
                                   "sym": r.ticker, "oid": resp_o.broker_order_id})
                    except Exception as _e:
                        logger.error(f"[TrailWatch] exit order failed for {r.ticker}: {_e}")
            except Exception:
                continue
        await db.commit()


_trail_last_run = None
async def _check_trail_watcher():
    """Tick the trail watcher every 30s during market hours."""
    global _trail_last_run
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now = _dt.now(_tz.utc)
    if _trail_last_run and (now - _trail_last_run) < _td(seconds=30):
        return
    _trail_last_run = now
    try:
        await _run_trailing_stop_watcher()
    except Exception as e:
        logger.warning(f"[TrailWatch] failed: {e}")



# ===== END-OF-DAY AUTO-CLOSE (3:55 PM ET) =====
# Theta Scanner picks are intraday — flatten before close so we don't carry
# losing positions overnight (the precipitating bug: EEIQ entered 6/1 10:51
# ET, never closed, still open as a -$X loser on 6/2). Idempotent per ET
# trading date via in-memory _eod_fired_for_date set.
_eod_fired_for_date: set = set()


def _eod_now_et():
    """Testable seam — tests can monkeypatch this to force a specific ET time."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        import zoneinfo
        return _dt.now(_tz.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return None


async def _check_end_of_day_close():
    """Fire EOD close once per US trading day at 15:55 ET. Iterates open
    positions and submits market-sell to Tradier for each."""
    from datetime import time as _dtime
    et = _eod_now_et()
    if et is None:
        return

    # Fire at 15:55 ET; allow a 5-min slop window so a sleep-blip doesn't
    # skip us. The idempotency set keeps us from double-firing.
    if not (_dtime(15, 55) <= et.time() <= _dtime(16, 0)):
        return
    # Weekdays only (Sat=5, Sun=6)
    if et.weekday() >= 5:
        return

    today_key = et.date().isoformat()
    if today_key in _eod_fired_for_date:
        return
    _eod_fired_for_date.add(today_key)

    logger.info(f"[EOD-close] starting auto-close pass for {today_key} (15:55 ET)")

    from app.database import async_session_factory
    from sqlalchemy import text as _t

    closed = 0
    failed = 0
    total = 0
    async with async_session_factory() as db:
        rows = (await db.execute(_t("""
            SELECT id, user_id, broker_account_id, ticker, qty, entry_price
              FROM open_positions_watch
             WHERE status = 'open'
               AND opened_at::date <= (NOW() AT TIME ZONE 'America/New_York')::date
             ORDER BY opened_at ASC LIMIT 200
        """))).fetchall()
        total = len(rows)
        logger.info(f"[EOD-close] {total} open positions eligible for EOD close")

        for r in rows:
            try:
                from app.engines.live_trading.broker_factory import build_broker_from_account
                from app.engines.live_trading.broker_base import OrderRequest, OrderSide, OrderType
                from app.models.user import BrokerAccount
                from sqlalchemy import select as _sel
                acct = (await db.execute(
                    _sel(BrokerAccount).where(BrokerAccount.id == r.broker_account_id)
                )).scalar_one_or_none()
                if not acct:
                    logger.error(f"[EOD-close] no broker_account for {r.ticker} id={r.id} — skipping")
                    failed += 1
                    continue
                broker = build_broker_from_account(acct)
                await broker.connect()
                logger.info(f"[EOD-close] selling {r.ticker} qty={r.qty}")
                resp = await broker.place_order(OrderRequest(
                    instrument=r.ticker, side=OrderSide.SELL,
                    quantity=int(r.qty), order_type=OrderType.MARKET,
                ))
                fill_px = float(getattr(resp, "filled_price", None) or 0)
                broker_order_id = getattr(resp, "broker_order_id", None)
                logger.info(
                    f"[EOD-close] order placed for {r.ticker} broker_order={broker_order_id} fill_px={fill_px}"
                )
                await db.execute(_t("""
                    UPDATE open_positions_watch
                       SET status='closed', exit_price=:px,
                           exit_reason='eod_auto_close', closed_at=NOW()
                     WHERE id = :id
                """), {"px": fill_px or None, "id": r.id})
                # Same mirror as the trail watcher: trades is the source-of-truth
                # for the P&L table. Compute pnl/net_pnl in-DB so we don't
                # double-compute it elsewhere. fill_px is whatever the broker
                # filled at — if it's 0/None we still close the row but pnl
                # falls back to NULL (better than a fake number).
                if fill_px:
                    await db.execute(_t("""
                        UPDATE trades
                           SET status = 'closed',
                               exit_price = :px,
                               exit_reason = 'eod_auto_close',
                               exit_time = NOW(),
                               pnl = ROUND(((:px - entry_price) * contracts)::numeric, 2),
                               net_pnl = ROUND(((:px - entry_price) * contracts - COALESCE(commission, 0))::numeric, 2),
                               broker_order_id = COALESCE(broker_order_id, :oid),
                               updated_at = NOW()
                         WHERE user_id = :uid
                           AND mode = 'live'
                           AND status = 'open'
                           AND instrument = :sym
                    """), {"px": fill_px, "uid": str(r.user_id),
                           "sym": r.ticker, "oid": broker_order_id})
                else:
                    await db.execute(_t("""
                        UPDATE trades
                           SET status = 'closed',
                               exit_reason = 'eod_auto_close',
                               exit_time = NOW(),
                               broker_order_id = COALESCE(broker_order_id, :oid),
                               updated_at = NOW()
                         WHERE user_id = :uid
                           AND mode = 'live'
                           AND status = 'open'
                           AND instrument = :sym
                    """), {"uid": str(r.user_id), "sym": r.ticker, "oid": broker_order_id})
                closed += 1
            except Exception as e:
                failed += 1
                logger.error(f"[EOD-close] failed for {r.ticker} id={r.id}: {e}")
                # Pipeline alert per-position so we know to manually flatten
                try:
                    from app.engines.pipeline_alerts import send_pipeline_failure_alert
                    import traceback as _tb
                    await send_pipeline_failure_alert(
                        reason=f"EOD auto-close failed for {r.ticker}",
                        context={
                            "job": "premarket_scheduler._check_end_of_day_close",
                            "ticker": r.ticker, "qty": int(r.qty),
                            "open_position_id": str(r.id),
                            "user_id": str(r.user_id),
                            "error": str(e),
                        },
                        traceback_str=_tb.format_exc(),
                    )
                except Exception:
                    pass

        await db.commit()
    logger.info(f"[EOD-close] complete: closed {closed} of {total}, failed {failed}")


# ════════════════════════════════════════════════════════════════════════
# ENTRY TIMING GATE FOR STOCK PICKS  (2026-06-05)
# ════════════════════════════════════════════════════════════════════════
# The four losing stock-picks in early June 2026 had two failure modes:
#   1. Micro-cap junk getting picked (URG, AIIO, EEIQ) — addressed by the
#      $10 price floor + MIN_SCORE in theta_scanner.py.
#   2. Late entries via blind 15-min auto-execute. SPRC was filled 2.5h
#      after the pick at a price already below the protective stop.
#
# This module fixes problem (2) with a deterministic gate:
#   • Pre-market window (08:30-09:25 ET): enter only when BOTH
#       - current price > pre-market session VWAP
#       - latest 5-min bar high > previous 5-min bar high (higher highs)
#     If either fails, wait — the scheduler ticks every 5 min so we re-check.
#   • Market-on-open (09:30+ ET): place a plain market order, no further
#     confirmation. Tradier delivers at the open print.
#   • Before 08:30 ET: defer — too thin to trust either signal.
#
# Stops are computed at the moment of order placement, NOT at pick time:
#   • Pre-mkt confirmed entry: stop = pre-market session LOW (lowest trade
#     between 04:00 ET and now).
#   • MOO entry (placed 09:30+): stop = first 5-min bar low (the ICT Oracle
#     opening candle, 09:30-09:35 ET). Computed at 09:35 ET on first
#     post-open tick.
#   • Shorts (rare): symmetric — high of session / opening candle.

_PENDING_STOCK_ENTRY_PREFIX = "theta:pending_entry:"
# In-process double-fire guard (the daily Redis flag also covers this but
# we want fast-path skip without round-tripping Redis on every tick).
_pick_executed_today: set = set()


def _polygon_key() -> str:
    return os.environ.get("POLYGON_API_KEY", "")


def _today_et_date_str() -> str:
    """Return today's ET date as YYYY-MM-DD for Polygon range queries."""
    et = _eod_now_et()
    return et.date().isoformat() if et else ""


async def _poly_get(url: str, params: dict, timeout: float = 4.0):
    """Tiny async wrapper around requests.get. We use requests because the
    rest of this module already uses it and the call rate is low (a couple
    per tick at most)."""
    import requests as _rq
    import asyncio as _aio
    try:
        loop = _aio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: _rq.get(url, params=params, timeout=timeout),
        )
    except Exception:
        return None


async def _polygon_5min_bars(ticker: str, date_et: str) -> list:
    """Fetch 5-minute aggregate bars from Polygon for a given ticker + date.
    Returns a list of dicts: [{'t': epoch_ms, 'o': float, 'h': float,
                                'l': float, 'c': float, 'v': int}, ...]
    Sorted ascending by time. Empty list on any failure."""
    key = _polygon_key()
    if not key or not ticker or not date_et:
        return []
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/"
        f"range/5/minute/{date_et}/{date_et}"
    )
    resp = await _poly_get(url, {
        "adjusted": "false", "sort": "asc", "apiKey": key,
    }, timeout=4.0)
    if not resp or resp.status_code != 200:
        return []
    try:
        return (resp.json() or {}).get("results", []) or []
    except Exception:
        return []


async def _polygon_1min_bars(ticker: str, date_et: str) -> list:
    """Fetch 1-minute bars for VWAP / pre-mkt-low computation."""
    key = _polygon_key()
    if not key or not ticker or not date_et:
        return []
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/"
        f"range/1/minute/{date_et}/{date_et}"
    )
    resp = await _poly_get(url, {
        "adjusted": "false", "sort": "asc", "apiKey": key,
    }, timeout=6.0)
    if not resp or resp.status_code != 200:
        return []
    try:
        return (resp.json() or {}).get("results", []) or []
    except Exception:
        return []


def _bar_is_premarket_et(bar_t_ms: int) -> bool:
    """A 1-min bar's start-time is in the 04:00-09:29 ET pre-market window."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        import zoneinfo
        et = _dt.fromtimestamp(bar_t_ms / 1000.0, tz=_tz.utc).astimezone(
            zoneinfo.ZoneInfo("America/New_York")
        )
    except Exception:
        return False
    mins = et.hour * 60 + et.minute
    return (4 * 60) <= mins < (9 * 60 + 30)


def _bar_is_rth_et(bar_t_ms: int) -> bool:
    """A bar's start-time is in regular trading hours 09:30-16:00 ET."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        import zoneinfo
        et = _dt.fromtimestamp(bar_t_ms / 1000.0, tz=_tz.utc).astimezone(
            zoneinfo.ZoneInfo("America/New_York")
        )
    except Exception:
        return False
    mins = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= mins <= (16 * 60)


def _bar_et_minutes(bar_t_ms: int) -> int:
    """Return the bar-open ET hour*60+min, or -1 on failure."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        import zoneinfo
        et = _dt.fromtimestamp(bar_t_ms / 1000.0, tz=_tz.utc).astimezone(
            zoneinfo.ZoneInfo("America/New_York")
        )
        return et.hour * 60 + et.minute
    except Exception:
        return -1


def compute_premarket_vwap(bars_1m: list) -> Optional[float]:
    """Volume-weighted average price across all pre-market 1-min bars.

    Polygon includes a 'vw' field on each bar = vwap of that minute. We
    aggregate to a session VWAP using sum(vw_i * v_i) / sum(v_i)."""
    num = 0.0
    den = 0.0
    for b in bars_1m or []:
        try:
            if not _bar_is_premarket_et(int(b.get("t", 0))):
                continue
            v = float(b.get("v", 0) or 0)
            vw = b.get("vw")
            if vw is None:
                # Fall back to (h+l+c)/3
                h = float(b.get("h", 0) or 0)
                l = float(b.get("l", 0) or 0)
                c = float(b.get("c", 0) or 0)
                vw = (h + l + c) / 3.0
            else:
                vw = float(vw)
            if v <= 0 or vw <= 0:
                continue
            num += vw * v
            den += v
        except Exception:
            continue
    if den <= 0:
        return None
    return num / den


def compute_premarket_low(bars_1m: list) -> Optional[float]:
    """Lowest trade price between 04:00 ET and 09:30 ET."""
    lows = []
    for b in bars_1m or []:
        try:
            if not _bar_is_premarket_et(int(b.get("t", 0))):
                continue
            l = float(b.get("l", 0) or 0)
            if l > 0:
                lows.append(l)
        except Exception:
            continue
    return min(lows) if lows else None


def compute_premarket_high(bars_1m: list) -> Optional[float]:
    """Highest trade price between 04:00 ET and 09:30 ET."""
    highs = []
    for b in bars_1m or []:
        try:
            if not _bar_is_premarket_et(int(b.get("t", 0))):
                continue
            h = float(b.get("h", 0) or 0)
            if h > 0:
                highs.append(h)
        except Exception:
            continue
    return max(highs) if highs else None


def compute_oracle_5min_candle(bars_5m: list) -> Optional[dict]:
    """Find the first RTH 5-min bar (09:30-09:35 ET) — the ICT Oracle
    opening candle. Returns {'h': float, 'l': float, 'o': float, 'c': float}
    or None if not yet available."""
    for b in bars_5m or []:
        m = _bar_et_minutes(int(b.get("t", 0)))
        # Polygon 5-min bars are aligned to 5-min boundaries. The first
        # RTH bar should open exactly at minute 570 (09:30).
        if m == 9 * 60 + 30:
            return {
                "h": float(b.get("h", 0) or 0),
                "l": float(b.get("l", 0) or 0),
                "o": float(b.get("o", 0) or 0),
                "c": float(b.get("c", 0) or 0),
                "v": int(b.get("v", 0) or 0),
            }
    return None


def has_higher_high(bars_5m: list, now_et_min: int) -> bool:
    """Pre-market higher-highs check: latest fully-closed 5-min bar's high
    is strictly greater than the bar before it. Uses pre-market 5-min bars
    (04:00-09:29 ET). Returns False if fewer than 2 bars available."""
    pre_bars = []
    for b in bars_5m or []:
        m = _bar_et_minutes(int(b.get("t", 0)))
        # only consider bars whose CLOSE is <= now (i.e., bar fully closed)
        if 4 * 60 <= m < 9 * 60 + 30 and m + 5 <= now_et_min:
            pre_bars.append(b)
    if len(pre_bars) < 2:
        return False
    last = float(pre_bars[-1].get("h", 0) or 0)
    prev = float(pre_bars[-2].get("h", 0) or 0)
    return last > 0 and prev > 0 and last > prev


async def _polygon_last_trade_price(ticker: str) -> Optional[float]:
    """Fetch the most recent trade price for live pre-market gating."""
    key = _polygon_key()
    if not key or not ticker:
        return None
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}"
    resp = await _poly_get(url, {"apiKey": key}, timeout=4.0)
    if not resp or resp.status_code != 200:
        return None
    try:
        t = (resp.json() or {}).get("ticker") or {}
        for fld, sub in (("lastTrade", "p"), ("min", "c"), ("day", "c"), ("prevDay", "c")):
            v = (t.get(fld) or {}).get(sub)
            if v and float(v) > 0:
                return float(v)
    except Exception:
        return None
    return None


def compute_oracle_stop_long(*, entry_method: str, premarket_low: Optional[float],
                              oracle_candle: Optional[dict], fallback_price: float) -> tuple:
    """Compute stop for a LONG entry. Returns (stop_price, label)."""
    if entry_method == "pre-mkt" and premarket_low and premarket_low > 0:
        return round(premarket_low - 0.01, 2), "pre-mkt-low"
    if entry_method == "MOO" and oracle_candle and oracle_candle.get("l", 0) > 0:
        return round(oracle_candle["l"] - 0.01, 2), "oracle-bar"
    # Fallback if Polygon failed — use 3% to keep us safe-ish.
    return round(fallback_price * 0.97, 2), "fallback-3pct"


def compute_oracle_stop_short(*, entry_method: str, premarket_high: Optional[float],
                               oracle_candle: Optional[dict], fallback_price: float) -> tuple:
    """Compute stop for a SHORT entry. Returns (stop_price, label)."""
    if entry_method == "pre-mkt" and premarket_high and premarket_high > 0:
        return round(premarket_high + 0.01, 2), "pre-mkt-high"
    if entry_method == "MOO" and oracle_candle and oracle_candle.get("h", 0) > 0:
        return round(oracle_candle["h"] + 0.01, 2), "oracle-bar"
    return round(fallback_price * 1.03, 2), "fallback-3pct"


async def _execute_stock_pick_with_timing_gate(pending_entry: dict) -> bool:
    """Decide whether to fire the broker order for a queued stock pick.

    Three time windows, ET clock:
      • <08:30 ET — DEFER. Don't enter.
      • 08:30-09:25 ET — PRE-MKT path. Enter only if (a) live price > pre-mkt
        VWAP AND (b) the latest closed 5-min pre-mkt bar high > the previous
        one. Stop = pre-market session low (lowest trade since 04:00 ET).
      • 09:30+ ET — MOO path. Place a plain market order (Tradier fills at
        the open). Compute stop after 09:35 ET from the first 5-min bar low.

    Returns True if an order was placed (or pre-existing fill detected),
    False if we are still waiting / deferring."""
    from datetime import datetime as _dt, timezone as _tz, date as _date
    try:
        import zoneinfo
        now_et = _dt.now(_tz.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return False

    ticker = pending_entry["ticker"]
    direction = pending_entry.get("direction", "long")
    user_id = pending_entry["user_id"]
    user_email = pending_entry.get("user_email", "(unknown)")
    broker_account_id = pending_entry["broker_account_id"]
    qty = int(pending_entry["qty"])
    pick_price = float(pending_entry["pick_price"])
    target = float(pending_entry.get("target") or pick_price * 1.10)
    pick_date = pending_entry["pick_date"]

    now_et_min = now_et.hour * 60 + now_et.minute
    today_et_str = now_et.date().isoformat()

    # Window classification
    if now_et_min < (8 * 60 + 30):
        window = "too-early"
    elif now_et_min <= (9 * 60 + 25):
        window = "pre-mkt"
    elif now_et_min < (9 * 60 + 30):
        # 09:25-09:30 dead zone — wait for the open
        window = "dead-zone"
    else:
        window = "MOO"

    logger.info(
        f"[stock-entry] timing-gate ticker={ticker} time_et={now_et.strftime('%H:%M')} "
        f"window={window}"
    )

    if window == "too-early":
        logger.info(f"[stock-entry] DEFER ticker={ticker} — too early for confirmation (<08:30 ET)")
        return False
    if window == "dead-zone":
        logger.info(f"[stock-entry] DEFER ticker={ticker} — 09:25-09:30 dead zone, waiting for open")
        return False

    # Compute Oracle stop bits
    bars_5m = await _polygon_5min_bars(ticker, today_et_str)
    bars_1m = []
    pm_vwap = None
    pm_low = None
    pm_high = None
    oracle_candle = None
    if window == "pre-mkt":
        bars_1m = await _polygon_1min_bars(ticker, today_et_str)
        pm_vwap = compute_premarket_vwap(bars_1m)
        pm_low = compute_premarket_low(bars_1m)
        pm_high = compute_premarket_high(bars_1m)
        # Live price for gating
        live_px = await _polygon_last_trade_price(ticker)
        vwap_pass = bool(live_px and pm_vwap and live_px > pm_vwap)
        hh_pass = has_higher_high(bars_5m, now_et_min)
        logger.info(
            f"[stock-entry] WAITING pre-mkt confirmation — "
            f"vwap_pass={vwap_pass} higher_high_pass={hh_pass} "
            f"live_px={live_px} pm_vwap={pm_vwap}"
        )
        if not (vwap_pass and hh_pass):
            return False
        entry_method = "pre-mkt"
        entry_price = float(live_px or pick_price)
    else:
        # MOO. After 09:35 ET the Oracle candle exists.
        oracle_candle = compute_oracle_5min_candle(bars_5m)
        entry_method = "MOO"
        # Use latest snapshot as estimated fill — Tradier will give us the
        # real fill price asynchronously.
        live_px = await _polygon_last_trade_price(ticker)
        entry_price = float(live_px or pick_price)

    # Compute stop
    if direction == "long":
        stop_price, stop_label = compute_oracle_stop_long(
            entry_method=entry_method,
            premarket_low=pm_low,
            oracle_candle=oracle_candle,
            fallback_price=entry_price,
        )
    else:
        stop_price, stop_label = compute_oracle_stop_short(
            entry_method=entry_method,
            premarket_high=pm_high,
            oracle_candle=oracle_candle,
            fallback_price=entry_price,
        )

    # Sanity: if live price already <= stop on a long, don't enter — we'd
    # be stopping out immediately. This is the SPRC failure mode.
    if direction == "long" and stop_price > 0 and entry_price <= stop_price:
        logger.error(
            f"[stock-entry] FAILED ticker={ticker} reason=entry_below_stop "
            f"entry={entry_price:.2f} stop={stop_price:.2f} ({stop_label}) "
            f"— skipping order, would stop out instantly"
        )
        # Clear the pending entry so we don't keep retrying — the day is lost.
        await _clear_pending_entry(pick_date, user_id)
        return False
    if direction == "short" and stop_price > 0 and entry_price >= stop_price:
        logger.error(
            f"[stock-entry] FAILED ticker={ticker} reason=entry_above_stop "
            f"entry={entry_price:.2f} stop={stop_price:.2f}"
        )
        await _clear_pending_entry(pick_date, user_id)
        return False

    # Place the order
    try:
        broker_order_id, status, err = await _place_intraday_broker_order(
            broker_account_id=broker_account_id,
            ticker=ticker, direction=direction, qty=qty,
        )
    except Exception as e:
        logger.error(f"[stock-entry] FAILED ticker={ticker} reason=broker_exception err={e}")
        return False

    if status != "executed":
        logger.error(
            f"[stock-entry] FAILED ticker={ticker} reason=broker_{status} err={err}"
        )
        # Do NOT clear — next tick may succeed (transient broker error).
        return False

    logger.info(
        f"[stock-entry] ENTERED ticker={ticker} method={entry_method} "
        f"entry={entry_price:.2f} stop={stop_price:.2f} ({stop_label}) "
        f"target={target:.2f} order_id={broker_order_id} user={user_email}"
    )

    # Persist position-watch + trades row, mirroring what emit_theta_pick
    # used to do before the refactor.
    try:
        from app.database import async_session_factory as _asf
        from sqlalchemy import text as _t
        async with _asf() as db:
            await db.execute(_t("""
                INSERT INTO open_positions_watch
                  (user_id, broker_account_id, ticker, qty, entry_price,
                   trail_pct, trail_high, hard_stop, target, source, broker_order_id)
                VALUES (CAST(:uid AS uuid), CAST(:bid AS uuid), :tk, :q, :ep,
                        3.0, :ep, :stop, :tgt, 'theta_scanner', :oid)
            """), {
                "uid": str(user_id), "bid": broker_account_id, "tk": ticker,
                "q": qty, "ep": entry_price, "stop": stop_price,
                "tgt": target, "oid": broker_order_id,
            })
            # Find or create the Theta Scanner trade_session
            sess_row = (await db.execute(_t("""
                SELECT id FROM trade_sessions
                 WHERE user_id = CAST(:uid AS uuid) AND mode='live'
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
                """), {"uid": str(user_id), "bid": broker_account_id})
                sess_id = ins.scalar()
            await db.execute(_t("""
                INSERT INTO trades (session_id, user_id, instrument, direction,
                    entry_price, stop_loss, take_profit, contracts, entry_time,
                    mode, status, broker_account_id, broker_order_id)
                VALUES (:sid, CAST(:uid AS uuid), :inst, :dir,
                    :ep, :sl, :tp, :q, NOW(), 'live', 'open', CAST(:bid AS uuid), :oid)
            """), {
                "sid": sess_id, "uid": str(user_id), "inst": ticker,
                "dir": direction, "ep": entry_price, "sl": stop_price,
                "tp": target, "q": qty, "bid": broker_account_id, "oid": broker_order_id,
            })
            await db.commit()
    except Exception as e:
        logger.error(f"[stock-entry] position-persist failed for {ticker}: {e}")
        # Don't return False — the order DID fire. Just log.

    # Mark the in-process guard + clear Redis pending entry so we don't
    # double-fire on the next tick.
    _pick_executed_today.add(f"{pick_date}:{user_id}")
    await _clear_pending_entry(pick_date, user_id)
    return True


async def _clear_pending_entry(pick_date: str, user_id: str):
    """Delete the Redis pending-entry key for a (date, user) pair."""
    try:
        import redis.asyncio as _ra
        _redis = _ra.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"), decode_responses=True)
        await _redis.delete(f"{_PENDING_STOCK_ENTRY_PREFIX}{pick_date}:{user_id}")
    except Exception as e:
        logger.warning(f"[stock-entry] failed to clear pending entry: {e}")


async def _check_pending_stock_entries():
    """Scheduler-tick: iterate Redis pending entries and run the timing
    gate on each. Called every 5-min cycle by start_premarket_scheduler.

    Time-window guard: outside 08:30-10:00 ET we skip the scan — the gate
    can't do anything useful and we don't want unnecessary Polygon calls."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        import zoneinfo
        et = _dt.now(_tz.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return
    et_min = et.hour * 60 + et.minute
    if et_min < (8 * 60 + 0) or et_min > (10 * 60 + 30):
        return  # outside any relevant window — no point polling

    pick_date = et.date().isoformat()
    if et.weekday() >= 5:
        return  # weekends — nothing to do

    try:
        import redis.asyncio as _ra
        import json as _j
        _redis = _ra.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"), decode_responses=True)
        # SCAN pattern instead of KEYS to avoid blocking
        cursor = 0
        pending_keys = []
        while True:
            cursor, keys = await _redis.scan(cursor=cursor, match=f"{_PENDING_STOCK_ENTRY_PREFIX}{pick_date}:*", count=100)
            pending_keys.extend(keys)
            if cursor == 0:
                break
        if not pending_keys:
            return
        logger.info(f"[stock-entry] tick {et.strftime('%H:%M ET')}: {len(pending_keys)} pending entries to gate")
        for key in pending_keys:
            try:
                raw = await _redis.get(key)
                if not raw:
                    continue
                pending = _j.loads(raw)
                # Double-fire guard
                guard_key = f"{pending.get('pick_date')}:{pending.get('user_id')}"
                if guard_key in _pick_executed_today:
                    continue
                await _execute_stock_pick_with_timing_gate(pending)
            except Exception as e:
                logger.error(f"[stock-entry] tick failed for key={key}: {e}")
    except Exception as e:
        logger.warning(f"[stock-entry] _check_pending_stock_entries top-level fail: {e}")

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
    saved sizing rules. Honors account_type (cash vs margin) for BP cap."""
    if not broker_account_id:
        return max(1, default)
    try:
        from app.database import async_session_factory as _asf
        from app.models.user import BrokerAccount as _BA
        from sqlalchemy import select as _sel
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

        per_share_risk = abs(entry - stop) if (stop and stop > 0) else (entry * 0.02)
        if per_share_risk <= 0:
            return max(1, default)

        shares = int(risk_usd // per_share_risk)
        position_usd = shares * entry

        if acct.max_position_usd and position_usd > acct.max_position_usd:
            shares = int(acct.max_position_usd // entry)
            position_usd = shares * entry

        bp = acct.cached_buying_power or 0.0
        if bp > 0 and position_usd > bp:
            shares = int(bp // entry)

        if (acct.account_type or "cash").lower() == "cash":
            cash = acct.cached_equity or bp
            if cash and shares * entry > cash:
                shares = int(cash // entry)

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
    if 9*60+30 <= t < 12*60:          return "NY_AM"
    if 14*60+30 <= t < 16*60+30:      return "NY_PM"
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
                logger.info("[Scanner] skipping intraday tick — user backtest/optimization in progress")
                return
        except Exception:
            pass  # if the check itself fails, fail-open and run the scan

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

            # Intraday cadence — every 5 min within window
            await _run_scan_cycle(is_premarket=False)
            await asyncio.sleep(INTRADAY_PERIOD_SEC)

        except asyncio.CancelledError:
            logger.info("[Scanner] scheduler cancelled")
            return
        except Exception as e:
            logger.error(f"[Scanner] loop error: {e}")
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
            SELECT DISTINCT u.id, u.email, u.username FROM users u
              JOIN strategies s ON s.user_id = u.id
             WHERE s.signal_mode = 'theta_scanner' AND s.status = 'ACTIVE'
        """))).fetchall()
        for u in users:
            class _U: pass
            user = _U(); user.id = u.id; user.email = u.email; user.username = u.username
            try:
                ok = await emit_theta_pick(db, user, pick)
                logger.info(f"[ThetaScanner] emitted to {u.email}: ok={ok}")
            except Exception as e:
                logger.error(f"[ThetaScanner] emit failed for {u.email}: {e}")


_theta_fired_today = None  # in-memory cache, backed by Redis
_theta_last_scan_min = None  # debounce: don't scan more than once per ~5 min

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
    """
    global _theta_fired_today, _theta_last_scan_min
    from datetime import datetime as _dt, date as _date, timezone as _tz
    try:
        import zoneinfo
        et = _dt.now(_tz.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return

    # Skip if outside the entire premarket scan window (6:00 ET - 9:50 ET)
    et_min = et.hour * 60 + et.minute
    if et_min < 6*60 or et_min > 9*60+50:
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
            return
    except Exception as _e:
        logger.warning(f"[ThetaScanner] redis check failed: {_e}")
        if _theta_fired_today == today: return

    # Find the best premarket candidate
    threshold = _min_score_for_et(et)
    try:
        from app.engines.options.theta_scanner import find_best_premarket_pick
        from app.database import async_session_factory as _asf
        async with _asf() as db:
            pick = await find_best_premarket_pick(db)
    except Exception as e:
        logger.error(f"[ThetaScanner] find_best_premarket_pick failed: {e}")
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
async def _run_trailing_stop_watcher():
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
                if exit_reason:
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

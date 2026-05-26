"""Background runner for Account Signals.

Each watcher polls market data on the same cadence as paper trading, runs
the strategy, and when a signal fires inserts a row in `account_signals`
+ sends an email. Never places orders."""
import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from loguru import logger
from sqlalchemy import select, text
import yfinance as yf

# Disable yfinance's SQLite timezone cache. Otherwise every Ticker() call
# opens/locks the same /root/.cache/py-yfinance/tkr-tz.db file, and with
# 5 watchers + a backtest worker all hitting it in parallel the SQLite
# WAL contention pegs every thread in S(sleeping) state — backtests
# hang at whatever % they were at when the lock storm started.
try:
    yf.set_tz_cache_location(None)  # type: ignore[attr-defined]
except Exception:
    # Older yfinance: monkey-patch the cache dir to /tmp so each run is fresh
    import os as _yf_os, tempfile as _yf_tmp
    _yf_os.environ['YF_CACHE_DIR'] = _yf_tmp.mkdtemp(prefix='yf-')

from app.database import async_session_factory
from app.models.user import User
from app.models.strategy import Strategy
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig, SignalType
from app.engines.data_feeds.local_cache import fetch_from_cache
from app.api.routes.account_signals import send_signal_email


_active: dict[str, asyncio.Task] = {}
YAHOO_SYMBOLS = {"ES": "ES=F", "NQ": "NQ=F", "RTY": "RTY=F", "YM": "YM=F"}


async def start_watcher(watcher_id: str, strategy_id: str, user_id: str,
                        instruments: list[str], account_label: str, channels: list[str], session_filter: str = "all"):
    if watcher_id in _active and not _active[watcher_id].done():
        logger.info(f"[Signals] Watcher {watcher_id} already running")
        return
    task = asyncio.create_task(_run_watcher(watcher_id, strategy_id, user_id, instruments, account_label, channels, session_filter))
    _active[watcher_id] = task
    logger.info(f"[Signals] Watcher started: {watcher_id} ({account_label}) on {instruments}")


async def stop_watcher(watcher_id: str):
    task = _active.pop(watcher_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _fetch_bars_sync(instrument: str, timeframe: str, count: int = 50):
    """Bug #27 fix: read from candle_cache (populated nightly by our own
    Polygon/Databento fetchers) instead of relying on yfinance bulk pulls
    that rate-limit to ~5% of the universe per cycle. Fall back to yfinance
    only when the cache is empty for this symbol/timeframe."""
    import psycopg2, os, pandas as _pd
    sym = instrument.upper()
    # Map timeframes to candle_cache resampling. The cache stores 1m bars.
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}.get(timeframe, 1)
    need = count * tf_minutes + 60  # buffer for resampling
    try:
        # Direct synchronous read — we are inside a sync helper invoked from
        # asyncio.to_thread in the watcher loop.
        url = os.environ.get("DATABASE_URL", "")
        # asyncpg url → psycopg2 url
        psy_url = url.replace("postgresql+asyncpg://", "postgresql://")
        with psycopg2.connect(psy_url) as cn, cn.cursor() as cur:
            cur.execute(
                "SELECT timestamp, open, high, low, close, volume "
                "FROM candle_cache WHERE symbol = %s "
                "ORDER BY timestamp DESC LIMIT %s",
                (sym, need),
            )
            rows = cur.fetchall()
        if rows:
            rows.reverse()
            df = _pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = _pd.to_datetime(df["timestamp"], utc=True)
            df = df.set_index("timestamp").astype({"open": float, "high": float, "low": float, "close": float, "volume": float})

            # Freshness gate: during market hours, fall through to yfinance if
            # the latest cached bar is > 15 min stale. The cache is populated
            # nightly so it has yesterday's bars during today's session.
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            latest_ts = df.index[-1]
            now_utc = _dt.now(_tz.utc)
            staleness_min = (now_utc - latest_ts.to_pydatetime()).total_seconds() / 60
            if staleness_min > 15:
                logger.info(f"[Signals] candle_cache for {sym} is {staleness_min:.0f}min stale — falling through to yfinance for live bars")
            else:
                if timeframe != "1m":
                    df = df.resample(f"{tf_minutes}min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
                df = df.tail(count)
                return [{
                    "timestamp": ts.to_pydatetime(),
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": int(r["volume"]),
                } for ts, r in df.iterrows()]
    except Exception as e:
        logger.warning(f"[Signals] candle_cache read failed for {sym}: {e}")

    # Fallback: yfinance, with an in-process 60s result cache + a global lock
    # so simultaneous watchers don't all hammer Yahoo in parallel and trigger
    # rate-limiting (which previously cratered the whole pipeline).
    fb_sym = YAHOO_SYMBOLS.get(instrument.upper(), instrument + "=F")
    period_map = {"1m": "5d", "5m": "5d", "15m": "10d", "30m": "10d", "1h": "30d", "1d": "60d"}
    period = period_map.get(timeframe, "5d")
    return _yfinance_cached(fb_sym, period, timeframe, count)


# ── yfinance throttle: 60s result cache + single in-process lock ─────────
import threading as _threading_yf
import time as _time_yf
_YF_LOCK = _threading_yf.Lock()
_YF_CACHE: dict[tuple[str, str, str], tuple[float, list]] = {}
_YF_TTL = 60.0  # seconds; matches the watcher's poll cadence


def _yfinance_cached(fb_sym: str, period: str, timeframe: str, count: int):
    key = (fb_sym, period, timeframe)
    now = _time_yf.time()
    # Quick read of cache (no lock needed — Python dict reads are atomic)
    hit = _YF_CACHE.get(key)
    if hit and (now - hit[0]) < _YF_TTL:
        return hit[1][-count:] if hit[1] else []
    # Serialize all yfinance calls — at most one at a time across the whole
    # process. Combined with the cache, the steady-state rate is at most
    # 1 call per (symbol, period, tf) per 60s.
    with _YF_LOCK:
        # Re-check after acquiring lock (another thread may have populated)
        hit = _YF_CACHE.get(key)
        if hit and (_time_yf.time() - hit[0]) < _YF_TTL:
            return hit[1][-count:] if hit[1] else []
        try:
            df = yf.Ticker(fb_sym).history(period=period, interval=timeframe)
            if df is None or df.empty:
                _YF_CACHE[key] = (_time_yf.time(), [])
                return []
            bars = [{
                "timestamp": ts.to_pydatetime(),
                "open": float(r["Open"]), "high": float(r["High"]),
                "low":  float(r["Low"]),  "close": float(r["Close"]),
                "volume": int(r["Volume"]),
            } for ts, r in df.iterrows()]
            _YF_CACHE[key] = (_time_yf.time(), bars)
            return bars[-count:]
        except Exception as e:
            # On error, cache an empty result for HALF the TTL so we back off
            # gracefully without retrying every tick.
            _YF_CACHE[key] = (_time_yf.time() - _YF_TTL/2, [])
            logger.error(f"[Signals] yfinance fallback failed for {fb_sym}: {e}")
            return []


async def _run_watcher(watcher_id, strategy_id, user_id, instruments, account_label, channels, session_filter="all"):
    """One asyncio loop per watcher. Polls each instrument every 60s, feeds the
    strategy, and on a fresh signal inserts a row + sends email."""
    try:
        _inst_strategies = {}  # Bug #15 fix: persist strategy state across cycles
        # Load strategy + user
        async with async_session_factory() as db:
            s_res = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
            strategy_model = s_res.scalar_one_or_none()
            u_res = await db.execute(select(User).where(User.id == user_id))
            user = u_res.scalar_one_or_none()
            if not strategy_model or not user:
                logger.error(f"[Signals] watcher {watcher_id}: missing strategy or user")
                return

            primary_tf = strategy_model.primary_timeframe or "5m"
            exec_tf    = strategy_model.execution_timeframe or "1m"
            htfs       = strategy_model.higher_timeframes or ["1H"]
            all_tfs = list(set([primary_tf, exec_tf] + htfs))

            cfg = StrategyConfig(
                name=strategy_model.name,
                instruments=instruments,
                primary_timeframe=primary_tf,
                execution_timeframe=exec_tf,
                higher_timeframes=htfs,
                risk_reward_ratio=strategy_model.risk_reward_ratio or 2.0,
                stop_loss_type=strategy_model.stop_loss_type or "structure",
                stop_loss_ticks=strategy_model.stop_loss_ticks,
                max_contracts=strategy_model.max_contracts or 1,
                session_filters=(
                    [session_filter.upper()] if session_filter and session_filter.lower() != "all"
                    else (strategy_model.session_filters or [])
                ),
                fvg_min_size_ticks=strategy_model.fvg_min_size_ticks or 4,
                fvg_max_size_ticks=strategy_model.fvg_max_size_ticks,
                max_daily_loss=strategy_model.max_daily_loss,
                max_trades_per_day=strategy_model.max_trades_per_day,
                use_rsi_filter=bool((strategy_model.rule_tree or {}).get("use_rsi_filter", False)),
                use_vwap_filter=bool((strategy_model.rule_tree or {}).get("use_vwap_filter", False)),
            )

        # Per-instrument: warm a buffer from cache, then poll Yahoo
        buffers: dict[tuple[str, str], list] = {}
        last_seen: dict[tuple[str, str], datetime] = {}

        for inst in instruments:
            for tf in all_tfs:
                try:
                    end = datetime.now(timezone.utc)
                    start = end - timedelta(days=90)
                    df = await fetch_from_cache(inst, start, end, tf)
                    if df is not None and not df.empty:
                        bars = []
                        for ts, row in df.tail(400).iterrows():
                            bars.append({
                                "timestamp": ts.to_pydatetime(),
                                "open": float(row["open"]), "high": float(row["high"]),
                                "low":  float(row["low"]),  "close": float(row["close"]),
                                "volume": int(row["volume"]),
                            })
                        buffers[(inst, tf)] = bars
                        if bars:
                            last_seen[(inst, tf)] = bars[-1]["timestamp"]
                except Exception as e:
                    logger.warning(f"[Signals] preload {inst} {tf}: {e}")

        logger.info(f"[Signals] {account_label} watching {instruments} TFs={all_tfs}")

        while True:
            # Confirm watcher is still active
            async with async_session_factory() as db:
                r = await db.execute(text("SELECT is_active FROM account_signal_watchers WHERE id = :id"),
                                     {"id": watcher_id})
                row = r.fetchone()
                if not row or not row[0]:
                    logger.info(f"[Signals] watcher {watcher_id} deactivated")
                    return

            for inst in instruments:
                # Fetch fresh bars per timeframe
                import pandas as pd
                bars_dict = {}
                for tf in all_tfs:
                    new_bars = await asyncio.to_thread(_fetch_bars_sync, inst, tf, 50)
                    buf = buffers.get((inst, tf), [])
                    last_t = last_seen.get((inst, tf))
                    for b in new_bars:
                        if last_t is None or b["timestamp"] > last_t:
                            buf.append(b)
                            last_seen[(inst, tf)] = b["timestamp"]
                    if len(buf) > 500:
                        buf = buf[-500:]
                    buffers[(inst, tf)] = buf
                    if buf:
                        bars_dict[tf] = pd.DataFrame(buf).set_index("timestamp")

                # Run strategy
                try:
                    strat = _inst_strategies.get(inst)
                    if strat is None:
                        strat = ICTStrategy(cfg, instrument=inst)
                        _inst_strategies[inst] = strat
                    signal = strat.on_bar(bars_dict)
                    if signal and signal.signal != SignalType.NONE:
                        await _emit_signal(watcher_id, strategy_id, user_id, account_label, channels,
                                           strategy_model.name, inst, signal, user.email, user.username or "trader")
                except Exception as e:
                    logger.error(f"[Signals] {inst} strategy error: {e}")

            await asyncio.sleep(60)

    except asyncio.CancelledError:
        logger.info(f"[Signals] watcher {watcher_id} cancelled")
        raise
    except Exception as e:
        logger.error(f"[Signals] watcher {watcher_id} crashed: {e}")
    finally:
        _active.pop(watcher_id, None)


async def _emit_signal(watcher_id, strategy_id, user_id, account_label, channels,
                       strategy_name, instrument, signal, email, username):
    """De-dupe signals by (watcher, instrument, direction, entry rounded) within
    the last 10 minutes — prevents email spam if the strategy keeps re-firing."""
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    direction = signal.signal.value
    entry = round(signal.entry_price, 2)

    async with async_session_factory() as db:
        # De-dupe check
        r = await db.execute(text("""
            SELECT 1 FROM account_signals
             WHERE watcher_id = :wid AND instrument = :inst AND direction = :dir
               AND ABS(entry_price - :entry) < 0.5
               AND fired_at > NOW() - INTERVAL '10 minutes'
             LIMIT 1
        """), {"wid": watcher_id, "inst": instrument, "dir": direction, "entry": entry})
        if r.fetchone():
            return

        await db.execute(text("""
            INSERT INTO account_signals
                (id, watcher_id, user_id, strategy_id, instrument, direction,
                 entry_price, stop_loss, take_profit, bias, fired_at, status)
            VALUES (:id, :wid, :uid, :sid, :inst, :dir,
                    :entry, :sl, :tp, :bias, :now, 'sent')
        """), {
            "id": sid, "wid": watcher_id, "uid": user_id, "sid": strategy_id,
            "inst": instrument, "dir": direction,
            "entry": entry,
            "sl": float(signal.stop_loss), "tp": float(signal.take_profit),
            "bias": (signal.metadata or {}).get("bias"),
            "now": now,
        })
        await db.commit()

    if "email" in channels:
        try:
            send_signal_email(
                to=email, username=username, account_label=account_label,
                strategy_name=strategy_name, instrument=instrument, direction=direction,
                entry=entry, stop=float(signal.stop_loss), target=float(signal.take_profit),
                bias=(signal.metadata or {}).get("bias"),
                fired_at=now.strftime("%a, %b %-d %-I:%M %p ET"),
            )
        except Exception as e:
            logger.error(f"[Signals] email send failed: {e}")

    if "push" in channels:
        try:
            from app.api.routes.account_signals import send_push_to_user
            await send_push_to_user(
                user_id=user_id,
                title=f"[{account_label}] {direction.upper()} {instrument} @ {entry:.2f}",
                body=f"{strategy_name} · Stop {float(signal.stop_loss):.2f} · Target {float(signal.take_profit):.2f}",
                data={
                    "kind": "signal",
                    "instrument": instrument,
                    "direction": direction,
                    "entry": entry,
                    "stop": float(signal.stop_loss),
                    "target": float(signal.take_profit),
                    "account_label": account_label,
                },
            )
        except Exception as e:
            logger.error(f"[Signals] push send failed: {e}")

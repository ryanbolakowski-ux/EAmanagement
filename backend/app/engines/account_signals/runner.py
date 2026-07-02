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
# NOTE: tz cache is now configured in app.main (ensure dir exists). Don't
# call set_tz_cache_location() here — yfinance 1.3.0 has a regression where
# passing anything (including None) breaks subsequent Ticker() calls.

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


# ── Futures real-time via Polygon ETF proxy (added 2026-06-09) ──────────────
# yfinance "=F" bars are ~10-15 min delayed, which made the futures Account
# Signals emails fire ~10 min late. Polygon's real-time ETF proxy (SPY/QQQ/
# IWM/DIA) is seconds-fresh; we scale it to the futures price LEVEL with the
# existing dynamic get_proxy_scale(). Micros map to the SAME proxy + parent
# scale as their full-size contract.
_FUTURES_PROXY_ETF = {
    "ES": "SPY", "NQ": "QQQ", "RTY": "IWM", "YM": "DIA",
    "MES": "SPY", "MNQ": "QQQ", "M2K": "IWM", "MYM": "DIA",
}
# Micro -> full-size parent (for the proxy scale lookup, which is keyed on the
# full-size root: ES/NQ/RTY/YM).
_MICRO_PARENT = {"MES": "ES", "MNQ": "NQ", "M2K": "RTY", "MYM": "YM"}


def _fetch_futures_via_polygon(instrument: str, timeframe: str, count: int = 50):
    """Return a list of bar dicts for a futures `instrument`, sourced from the
    Polygon ETF proxy and scaled to the futures price level. Returns [] (so the
    caller falls back to candle_cache/yfinance) on any miss/error."""
    import os as _os
    import requests as _rq
    import pandas as _pd
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from app.engines.data_feeds.proxy_scale import get_proxy_scale

    inst = instrument.upper()
    etf = _FUTURES_PROXY_ETF.get(inst)
    if not etf:
        return []  # not a proxied futures instrument
    key = _os.environ.get("POLYGON_API_KEY", "")
    if not key:
        return []

    # Scale is keyed on the full-size root (micros borrow the parent's ratio).
    scale_root = _MICRO_PARENT.get(inst, inst)
    try:
        scale = float(get_proxy_scale(scale_root))
    except Exception as e:
        logger.warning(f"[Signals] futures {inst}: proxy scale lookup failed ({e}); skipping Polygon path")
        return []
    if not scale or scale <= 0:
        return []

    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}.get(timeframe, 1)
    # Pull a window of 1-min proxy bars wide enough to resample `count` bars of
    # the requested timeframe (+buffer). 2 calendar days covers an overnight gap
    # so the latest RTH/ETH bar is always present.
    end = _dt.now(_tz.utc)
    start = end - _td(days=2)
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{etf}"
        f"/range/1/minute/{start.date()}/{end.date()}"
        f"?adjusted=true&sort=asc&limit=50000&apiKey={key}"
    )
    try:
        r = _rq.get(url, timeout=8)
        if r.status_code != 200:
            logger.warning(f"[Signals] futures {inst} via Polygon {etf}: HTTP {r.status_code}; falling back")
            return []
        results = (r.json() or {}).get("results", []) or []
    except Exception as e:
        logger.warning(f"[Signals] futures {inst} via Polygon {etf}: fetch error {type(e).__name__}: {e}; falling back")
        return []
    if not results:
        logger.info(f"[Signals] futures {inst} via Polygon {etf}: no bars returned; falling back")
        return []

    df = _pd.DataFrame(results)
    df["timestamp"] = _pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume"})
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    # Scale ETF price -> futures price level (volume left as the ETF's).
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float) * scale
    df["volume"] = df["volume"].astype(float)

    # Resample to the requested timeframe the same way the candle_cache path does.
    if timeframe != "1m":
        df = df.resample(f"{tf_minutes}min").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna()
    df = df.tail(count)
    if df.empty:
        return []

    latest_ts = df.index[-1].to_pydatetime()
    age_sec = (_dt.now(_tz.utc) - latest_ts).total_seconds()
    logger.info(
        f"[Signals] futures {inst} via Polygon {etf} scale={scale:.2f} "
        f"latest_bar={latest_ts.isoformat()} age={age_sec:.0f}s"
    )
    # Freshness guard: the whole point is a fresher bar than yfinance. If the
    # proxy itself is stale (sparse pre-mkt / overnight SPY), don't regress —
    # return [] so the caller falls through to candle_cache/yfinance.
    _max_age = float(_os.environ.get("FUTURES_PROXY_MAX_AGE_SEC", "900"))
    if age_sec > _max_age:
        logger.info(
            f"[Signals] futures {inst} via Polygon {etf}: latest bar age "
            f"{age_sec:.0f}s > {_max_age:.0f}s (sparse proxy) — falling back to yfinance"
        )
        return []
    return [{
        "timestamp": ts.to_pydatetime(),
        "open": float(r2["open"]), "high": float(r2["high"]),
        "low": float(r2["low"]), "close": float(r2["close"]),
        "volume": int(r2["volume"]),
    } for ts, r2 in df.iterrows()]


def _fetch_futures_via_alpaca(instrument: str, timeframe: str, count: int = 50):
    """Return scaled futures bars sourced from Alpaca's real-time IEX ETF proxy.

    Alpaca's FREE tier serves real-time IEX bars for ultra-liquid ETFs
    (SPY/QQQ/IWM/DIA), accurate to the penny — the cheapest fix for the
    ~15-min-late futures emails (Polygon real-time is 403 "not entitled").
    We fetch the proxy ETF, scale it to the futures price level with the same
    dynamic get_proxy_scale() the Polygon path uses, and resample to the
    requested timeframe.

    Returns [] (so the caller falls through to the Polygon/yfinance paths) on
    any miss/error. Alpaca IEX is real-time, so — unlike the delayed Polygon
    proxy — we do NOT apply the 900s freshness-discard guard here: its bars are
    trusted as fresh."""
    import os as _os
    import pandas as _pd
    from datetime import datetime as _dt, timezone as _tz
    from app.engines.data_feeds.proxy_scale import get_proxy_scale
    from app.engines.data_feeds.alpaca_feed import fetch_alpaca_bars

    inst = instrument.upper()
    etf = _FUTURES_PROXY_ETF.get(inst)
    if not etf:
        return []  # not a proxied futures instrument
    if not _os.environ.get("ALPACA_API_KEY", ""):
        return []  # keys not configured -> stay on the existing fallback

    # Scale is keyed on the full-size root (micros borrow the parent's ratio).
    scale_root = _MICRO_PARENT.get(inst, inst)
    try:
        scale = float(get_proxy_scale(scale_root))
    except Exception as e:
        logger.warning(f"[Signals] futures {inst}: proxy scale lookup failed ({e}); skipping Alpaca path")
        return []
    if not scale or scale <= 0:
        return []

    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}.get(timeframe, 1)
    # Pull enough 1-min proxy bars to resample `count` bars of the requested
    # timeframe (+buffer). Alpaca caps at 10000/req; this stays well under.
    need = count * tf_minutes + 60
    try:
        df = fetch_alpaca_bars(etf, timeframe="1Min", limit=min(need, 10000))
    except Exception as e:
        logger.warning(f"[Signals] futures {inst} via Alpaca {etf}: fetch crashed {type(e).__name__}: {e}; falling back")
        return []
    if df is None or df.empty:
        return []

    df = df[["open", "high", "low", "close", "volume"]].copy()
    # Scale ETF price -> futures price level (volume left as the ETF's).
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float) * scale
    df["volume"] = df["volume"].astype(float)

    # Resample to the requested timeframe the same way the Polygon path does.
    if timeframe != "1m":
        df = df.resample(f"{tf_minutes}min").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna()
    df = df.tail(count)
    if df.empty:
        return []

    latest_ts = df.index[-1].to_pydatetime()
    age_sec = (_dt.now(_tz.utc) - latest_ts).total_seconds()
    # NOTE: no freshness-discard guard here — Alpaca IEX bars are real-time and
    # trusted as fresh (the 900s guard applies only to the delayed Polygon path).
    logger.info(
        f"[Signals] futures {inst} source=alpaca proxy={etf} scale={scale:.2f} "
        f"latest_bar={latest_ts.isoformat()} age={age_sec:.0f}s"
    )
    return [{
        "timestamp": ts.to_pydatetime(),
        "open": float(r2["open"]), "high": float(r2["high"]),
        "low": float(r2["low"]), "close": float(r2["close"]),
        "volume": int(r2["volume"]),
    } for ts, r2 in df.iterrows()]



def _latest_real_close(sym: str):
    """SIGNAL-PRICE-ALIGN-V1: latest REAL futures close from candle_cache (the
    source of truth that the chart + resolver use). Used to validate proxy
    price levels + the stale-entry guard. Returns float or None."""
    import psycopg2, os as _os2
    try:
        psy_url = _os2.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
        cn = psycopg2.connect(psy_url, connect_timeout=5)
        try:
            with cn.cursor() as cur:
                cur.execute("SELECT close FROM candle_cache WHERE symbol = %s "
                            "ORDER BY timestamp DESC LIMIT 1", ((sym or "").upper(),))
                r = cur.fetchone()
            return float(r[0]) if r and r[0] else None
        finally:
            cn.close()
    except Exception:
        return None


def _fetch_bars_uncached(instrument: str, timeframe: str, count: int = 50):
    """Bug #27 fix: read from candle_cache (populated nightly by our own
    Polygon/Databento fetchers) instead of relying on yfinance bulk pulls
    that rate-limit to ~5% of the universe per cycle. Fall back to yfinance
    only when the cache is empty for this symbol/timeframe.

    2026-06-09: for FUTURES instruments, try the real-time Polygon ETF proxy
    FIRST (seconds-fresh) before candle_cache/yfinance — this is what fixes the
    ~10-min-late futures emails. Any miss/error transparently falls through to
    the existing (slower but reliable) paths below."""
    import psycopg2, os, pandas as _pd
    sym = instrument.upper()
    # ── Futures real-time fast-path (scaled ETF proxy) ──
    # Source priority: (1) Alpaca IEX real-time (free tier, penny-accurate for
    # SPY/QQQ/IWM/DIA — the cheapest fix for ~15-min-late futures emails),
    # (2) Polygon proxy (existing fallback), (3) yfinance (final fallback below).
    if sym in _FUTURES_PROXY_ETF:
        # SIGNAL-PRICE-ALIGN-V1: validate any proxy's price LEVEL against the
        # real futures close (candle_cache = source of truth) and DISCARD the
        # proxy if it drifts too far (the stale-scale bug priced NQ ~1.4% low).
        _real_anchor = _latest_real_close(sym)
        _max_drift = float(os.environ.get("FUTURES_PROXY_MAX_DRIFT_PCT", "0.4")) / 100.0
        def _proxy_ok(bars):
            if not bars:
                return False
            if not _real_anchor or _real_anchor <= 0:
                return True  # no real anchor available — keep prior behavior
            px = float(bars[-1]["close"])
            drift = abs(px - _real_anchor) / _real_anchor
            if drift > _max_drift:
                logger.warning(
                    f"[Signals] futures {sym} PROXY DISCARDED: proxy close {px:.2f} vs "
                    f"real {_real_anchor:.2f} drift {drift*100:.2f}% > {_max_drift*100:.2f}% "
                    f"— using real candle_cache for correct price levels")
                return False
            return True
        # (1) Alpaca IEX — preferred when keys are configured. Real-time, so no
        # freshness-discard guard is applied to its bars (handled in the helper).
        try:
            alpaca_bars = _fetch_futures_via_alpaca(instrument, timeframe, count)
        except Exception as e:
            logger.warning(f"[Signals] futures {sym}: Alpaca proxy path crashed ({type(e).__name__}: {e}); falling back")
            alpaca_bars = None
        if _proxy_ok(alpaca_bars):
            return alpaca_bars
        # (2) Polygon proxy — fallback (delayed; keeps its own 900s freshness guard).
        try:
            proxy_bars = _fetch_futures_via_polygon(instrument, timeframe, count)
        except Exception as e:
            logger.warning(f"[Signals] futures {sym}: Polygon proxy path crashed ({type(e).__name__}: {e}); falling back")
            proxy_bars = None
        if _proxy_ok(proxy_bars):
            logger.info(f"[Signals] futures {sym} source=polygon proxy={_FUTURES_PROXY_ETF.get(sym)}")
            return proxy_bars
    # Map timeframes to candle_cache resampling. The cache stores 1m bars.
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}.get(timeframe, 1)
    need = count * tf_minutes + 60  # buffer for resampling
    try:
        # Direct synchronous read — we are inside a sync helper invoked from
        # asyncio.to_thread in the watcher loop.
        url = os.environ.get("DATABASE_URL", "")
        # asyncpg url → psycopg2 url
        psy_url = url.replace("postgresql+asyncpg://", "postgresql://")
        # CONNECTION-LEAK FIX: psycopg2 with-connect only manages the
        # TRANSACTION on __exit__ — it does NOT close the connection. Called from
        # every watcher poll (6 watchers x instruments x timeframes / 60s) this
        # leaked ~70 sockets and exhausted Postgres max_connections (100), which
        # in turn starved the optimization worker. Close explicitly in finally.
        cn = None
        try:
            cn = psycopg2.connect(psy_url, connect_timeout=5)
            with cn.cursor() as cur:
                cur.execute(
                    "SELECT timestamp, open, high, low, close, volume "
                    "FROM candle_cache WHERE symbol = %s "
                    "ORDER BY timestamp DESC LIMIT %s",
                    (sym, need),
                )
                rows = cur.fetchall()
        finally:
            if cn is not None:
                try:
                    cn.close()
                except Exception:
                    pass
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
from app.core.ttl_cache import TTLCache as _TTLCache
_YF_LOCK = _threading_yf.Lock()
# TTLCache (was a bare dict): keys accumulate per (symbol, period, timeframe)
# and were never pruned (TTL only checked on read). maxsize=512 bounds it;
# the manual _YF_TTL freshness checks below are unchanged. NOTE: the error
# path deliberately back-dates its timestamp by _YF_TTL/2 — that call-site
# semantics is preserved (the site check governs freshness; TTLCache only
# governs eviction).
_YF_CACHE: _TTLCache = _TTLCache(maxsize=512, ttl_seconds=60.0)
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


# ── Shared bar cache ────────────────────────────────────────────────────
# Dedupe identical (instrument, timeframe, count) fetches across ALL watchers
# within a short TTL. Without this, N watchers each re-read candle_cache +
# re-resample the same ES/NQ bars every poll cycle, bursting CPU. The bars are
# read-only downstream (the watcher appends them to its own buffer and builds a
# DataFrame; it never mutates the bar dicts), so handing out a shallow copy of
# the cached list is safe. Effect: ~N fetches/min -> ~2/min per (inst, tf).
# TTLCache (was a bare dict): bounded at 512 (instrument, timeframe, count)
# combos; expired entries are pruned on set instead of persisting forever.
# The manual _BAR_CACHE_TTL freshness checks below are unchanged.
_BAR_CACHE: _TTLCache = _TTLCache(maxsize=512, ttl_seconds=30.0)
_BAR_CACHE_LOCK = _threading_yf.Lock()
_BAR_CACHE_TTL = 30.0  # seconds; well under the 60s watcher poll cadence


def _fetch_bars_sync(instrument: str, timeframe: str, count: int = 50):
    key = (instrument.upper(), timeframe, count)
    now = _time_yf.time()
    hit = _BAR_CACHE.get(key)
    if hit and (now - hit[0]) < _BAR_CACHE_TTL:
        return list(hit[1])  # shallow copy — read-only dicts shared, list owned by caller
    with _BAR_CACHE_LOCK:
        hit = _BAR_CACHE.get(key)
        if hit and (_time_yf.time() - hit[0]) < _BAR_CACHE_TTL:
            return list(hit[1])
        bars = _fetch_bars_uncached(instrument, timeframe, count)
        _BAR_CACHE[key] = (_time_yf.time(), bars or [])
        return list(bars or [])


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
            cfg.rule_tree = strategy_model.rule_tree or {}  # carries engine_version (v1/v2)
            cfg.take_profit_mode = (strategy_model.rule_tree or {}).get("take_profit_mode", "auto")  # LIVE-PARITY-TPM-V1

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
                r = await db.execute(text("""
                    SELECT w.is_active, s.status, u.is_active AS user_active
                      FROM account_signal_watchers w
                      LEFT JOIN strategies s ON s.id = w.strategy_id
                      LEFT JOIN users u ON u.id = w.user_id
                     WHERE w.id = :id
                """), {"id": watcher_id})
                row = r.fetchone()
                if not row or not row[0]:
                    logger.info(f"[Signals] watcher {watcher_id} deactivated")
                    return
                # Also stop if the owning USER was deactivated/deleted. Only an
                # explicit False stops it (NULL/orphan joins are left running so
                # a missing user row can never silence a real customer).
                if row[2] is False:
                    logger.info(f"[Signals] watcher {watcher_id} owner deactivated — stopping")
                    return
                # Bug 2: never emit for a DRAFT strategy even if a watcher exists
                # (handles legacy rows created before the create-time guard).
                _sstat = row[1]
                _sstat = _sstat.value if hasattr(_sstat, "value") else str(_sstat or "")
                if _sstat.lower() == "draft":
                    logger.info(f"[Signals] watcher {watcher_id} paused — strategy is draft; skipping cycle")
                    await asyncio.sleep(60)
                    continue

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
        try:
            from app.engines.pipeline_alerts import send_pipeline_failure_alert
            import traceback as _tb
            await send_pipeline_failure_alert(
                reason=f"Account-signals watcher crashed: {type(e).__name__}",
                context={"job": "account_signals.runner._run_watcher",
                         "step": "watcher_outer",
                         "watcher_id": str(watcher_id),
                         "strategy_id": str(strategy_id),
                         "user_id": str(user_id),
                         "instruments": list(instruments) if instruments else [],
                         "error": str(e)},
                traceback_str=_tb.format_exc(),
            )
        except Exception:
            pass
    finally:
        _active.pop(watcher_id, None)


async def _emit_signal(watcher_id, strategy_id, user_id, account_label, channels,
                       strategy_name, instrument, signal, email, username):
    """Validate geometry, dedupe by a content idempotency key within a cooldown
    window, persist with delivery-tracking columns, then send. Replaces the old
    entry-rounded/10-min heuristic, which let genuine duplicates through and
    blocked nothing reliably."""
    import os as _os
    from app.engines.account_signals.signal_guard import validate_geometry, make_idempotency_key

    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    detected_at = now
    direction = signal.signal.value
    entry = round(signal.entry_price, 2)
    stop = round(float(signal.stop_loss), 2)
    tp = round(float(signal.take_profit), 2)
    bar_ts = getattr(signal, "timestamp", None) or now
    bias = (signal.metadata or {}).get("bias")

    # ── Bug 6: geometry validation BEFORE persisting or sending ──
    geo = validate_geometry(direction, entry, stop, tp, instrument)
    if not geo["valid"]:
        logger.warning(
            f"[Signals] REJECTED invalid geometry watcher={watcher_id} {instrument} "
            f"{direction} entry={entry} stop={stop} tp={tp}: {geo['error']}"
        )
        return
    for _w in geo["warnings"]:
        logger.warning(f"[Signals] geometry warning watcher={watcher_id} {instrument} {direction}: {_w}")

    # SIGNAL-PRICE-ALIGN-V1 stale-entry guard: never email/route a signal whose
    # entry has drifted too far from the CURRENT real market price (source of
    # truth = candle_cache). Catches proxy-drift / stale setups before send.
    # to_thread: _latest_real_close is a sync psycopg2 connect+query (up to 5s
    # connect timeout) — run it off the event loop so a slow DB can't stall
    # every other watcher/runner coroutine. Return value unchanged.
    _real_now = await asyncio.to_thread(_latest_real_close, instrument)
    _max_entry_drift = float(_os.environ.get("SIGNAL_MAX_ENTRY_DRIFT_PCT", "0.5")) / 100.0
    _sym_map = YAHOO_SYMBOLS.get(instrument.upper(), (instrument or "") + "=F")
    if _real_now and _real_now > 0:
        _drift = abs(entry - _real_now) / _real_now
        logger.info(
            f"[signal-source] {instrument}->{_sym_map} provider=candle_cache "
            f"entry={entry} real_now={_real_now:.2f} drift={_drift*100:.2f}%")
        if _drift > _max_entry_drift:
            async with async_session_factory() as _db:
                await _db.execute(text("""
                    INSERT INTO account_signals
                        (id, watcher_id, user_id, strategy_id, instrument, direction,
                         entry_price, stop_loss, take_profit, bias, fired_at, status,
                         detected_at, duplicate_suppressed_count, outcome_reason)
                    VALUES (:id, :wid, :uid, :sid, :inst, :dir, :entry, :sl, :tp, :bias,
                            :now, 'suppressed', :detected, 0, :reason)
                """), {"id": str(uuid.uuid4()), "wid": watcher_id, "uid": user_id,
                       "sid": strategy_id, "inst": instrument, "dir": direction,
                       "entry": entry, "sl": stop, "tp": tp, "bias": bias,
                       "now": now, "detected": detected_at,
                       "reason": f"stale_entry_drift {_drift*100:.2f}% vs real {_real_now:.2f}"})
                await _db.commit()
            logger.warning(
                f"[Signals] STALE-ENTRY suppressed {instrument} {direction} entry={entry} "
                f"vs real {_real_now:.2f} drift {_drift*100:.2f}% > {_max_entry_drift*100:.2f}% — NOT sent/routed")
            return

    # ── Bug 4: content idempotency key + cooldown-window suppression ──
    # NOTE: bar_ts is intentionally NOT part of the idem key — the key now
    # encodes only the SETUP SHAPE (watcher/strategy/instrument/direction +
    # tick-banded entry/stop/tp). Two consecutive bars on the same setup
    # produce the SAME key, so the cooldown query below catches the duplicate.
    # The bar_ts is still passed for logging compatibility.
    idem = make_idempotency_key(watcher_id, strategy_id, instrument, direction, bar_ts, entry, stop, tp)
    cooldown_min = int(_os.environ.get("SIGNAL_DUP_COOLDOWN_MIN", "15"))

    async with async_session_factory() as db:
        dup_row = (await db.execute(text("""
            SELECT id, fired_at FROM account_signals
             WHERE idempotency_key = :k
               AND fired_at > NOW() - make_interval(mins => :cool)
             ORDER BY fired_at DESC LIMIT 1
        """), {"k": idem, "cool": cooldown_min})).fetchone()
        # [signal-dedup] log line — makes the dedup decision visible in logs
        # so an operator can confirm the cooldown is firing.
        logger.info(
            f"[signal-dedup] idem={idem[:12]} cooldown_check={cooldown_min}m "
            f"within={dup_row is not None} watcher={watcher_id} {instrument} {direction} "
            f"entry={entry} stop={stop} tp={tp}"
        )
        if dup_row:
            await db.execute(text("""
                UPDATE account_signals
                   SET duplicate_suppressed_at = :now,
                       duplicate_suppressed_count = duplicate_suppressed_count + 1
                 WHERE id = :id
            """), {"now": now, "id": dup_row[0]})
            await db.commit()
            logger.info(
                f"[Signals] DUPLICATE suppressed idem={idem[:12]} watcher={watcher_id} "
                f"{instrument} {direction} @ {entry} (cooldown {cooldown_min}m, "
                f"original fired_at={dup_row[1]})"
            )
            return

        await db.execute(text("""
            INSERT INTO account_signals
                (id, watcher_id, user_id, strategy_id, instrument, direction,
                 entry_price, stop_loss, take_profit, bias, fired_at, status,
                 idempotency_key, detected_at, duplicate_suppressed_count)
            VALUES (:id, :wid, :uid, :sid, :inst, :dir,
                    :entry, :sl, :tp, :bias, :now, :status,
                    :idem, :detected, 0)
        """), {
            "id": sid, "wid": watcher_id, "uid": user_id, "sid": strategy_id,
            "inst": instrument, "dir": direction,
            "entry": entry, "sl": stop, "tp": tp, "bias": bias,
            "now": now, "status": "pending", "idem": idem, "detected": detected_at,
        })
        await db.commit()

    # ── Bug 5: email delivery tracking lifecycle ──
    if "email" in channels:
        queued_at = datetime.now(timezone.utc)
        async with async_session_factory() as db:
            await db.execute(text("UPDATE account_signals SET queued_at = :q WHERE id = :id"),
                             {"q": queued_at, "id": sid})
            await db.commit()
        try:
            # to_thread: send_signal_email is fully sync (sync redis gate,
            # _fetch_bars_sync bar pulls, matplotlib chart render, httpx POST
            # to Resend with retries) — several seconds of blocking that used
            # to freeze the whole event loop per email. Same args, same return
            # dict, same exceptions; it just runs on a worker thread now.
            result = await asyncio.to_thread(
                send_signal_email,
                to=email, username=username, account_label=account_label,
                strategy_name=strategy_name, instrument=instrument, direction=direction,
                entry=entry, stop=stop, target=tp, bias=bias,
                fired_at=now.strftime("%a, %b %-d %-I:%M %p ET"),
                signal_id=sid, entry_detected_at=detected_at,
            )
        except Exception as e:
            result = {"sent": False, "provider_status": "exception", "error": f"{type(e).__name__}: {e}",
                      "provider_message_id": None, "suppressed": False}
            logger.exception(
                f"[Signals] email send EXCEPTION signal_id={sid} symbol={instrument} "
                f"strategy={strategy_name} detected_at={detected_at.isoformat()} "
                f"attempted_at={datetime.now(timezone.utc).isoformat()} err={type(e).__name__}: {e}"
            )
        if not isinstance(result, dict):  # defensive: legacy bool
            result = {"sent": bool(result), "provider_status": "sent" if result else "failed",
                      "provider_message_id": None, "error": None, "suppressed": False}
        sent = bool(result.get("sent"))
        suppressed = bool(result.get("suppressed"))
        provider_sent_at = datetime.now(timezone.utc) if sent else None
        latency = (provider_sent_at - detected_at).total_seconds() if provider_sent_at else None
        final_status = "sent" if sent else ("suppressed" if suppressed else "failed")
        # chart_b64: annotated trade-chart PNG (base64) produced by
        # send_signal_email so the Email Signals page can render it inline.
        _chart_b64 = result.get("chart_b64")
        # Level reasons (e.g. "swing low", "London high") inferred in
        # send_signal_email so the Email Signals page can show WHY each
        # stop/target sits where it does. Never blank (falls back to
        # "strategy stop"/"strategy target").
        _stop_reason = result.get("stop_reason")
        _target_reason = result.get("target_reason")
        async with async_session_factory() as db:
            from app.api.routes.account_signals import _ensure_chart_columns
            await _ensure_chart_columns(db)
            await db.execute(text("""
                UPDATE account_signals
                   SET status = :st,
                       provider_sent_at = :psa,
                       delivered_at = :psa,
                       provider_message_id = :pid,
                       provider_status = :pstatus,
                       latency_seconds = :lat,
                       error_message = :err,
                       chart_b64 = :chart,
                       stop_reason = :stop_reason,
                       target_reason = :target_reason
                 WHERE id = :id
            """), {
                "st": final_status, "psa": provider_sent_at,
                "pid": result.get("provider_message_id"),
                "pstatus": result.get("provider_status"),
                "lat": latency, "err": result.get("error"),
                "chart": _chart_b64,
                "stop_reason": _stop_reason, "target_reason": _target_reason,
                "id": sid,
            })
            await db.commit()
        elapsed_ms = int((datetime.now(timezone.utc) - queued_at).total_seconds() * 1000)
        if elapsed_ms > 3000:
            logger.warning(f"[Signals] slow email send signal_id={sid} took {elapsed_ms}ms")

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

    # ROUTING (#156): fan the sent signal into eligible active paper/live sessions.
    if locals().get("final_status") == "sent":
        try:
            await route_emitted_signal(sid, user_id, instrument, signal, strategy_id)
        except Exception as _re:
            logger.warning(f"[signal-route] dispatch failed signal={sid}: {_re}")


async def route_emitted_signal(sid, user_id, instrument, signal, strategy_id=None):
    """ROUTING (#156): route a just-emitted email signal into the user's ACTIVE
    eligible paper/live sessions for `instrument`. Paper enters (safe); live is
    gated by Phase E auto_trade_allowed. Logs + audits every decision."""
    inst = (instrument or "").upper()
    routed = []
    try:
        from app.engines.paper_trading.runner import _active_traders as _pt
        for key, tr in list(_pt.items()):
            if str(getattr(tr, "user_id", "")) == str(user_id) and getattr(tr, "instrument", "").upper() == inst and getattr(tr, "_is_running", False):
                entered, reason = await tr.route_external_signal(signal, source_signal_id=str(sid))
                routed.append(("paper", key, entered, reason))
    except Exception as e:
        logger.warning(f"[signal-route] paper enumerate error: {e}")
    try:
        from app.engines.live_trading.runner import _active_live_traders as _lt
        from app.core.auto_trade_guard import auto_trade_allowed
        for key, tr in list(_lt.items()):
            if str(getattr(tr, "user_id", "")) == str(user_id) and getattr(tr, "instrument", "").upper() == inst and getattr(tr, "_is_running", False):
                ok, why = await auto_trade_allowed(getattr(tr, "user_id", None), getattr(tr, "broker_account_id", None),
                                                   context={"kind": "signal_route", "signal_id": str(sid), "instrument": inst})
                if not ok:
                    routed.append(("live", key, False, f"blocked:{why}")); continue
                entered, reason = await tr.route_external_signal(signal, source_signal_id=str(sid))
                routed.append(("live", key, entered, reason))
    except Exception as e:
        logger.warning(f"[signal-route] live enumerate error: {e}")
    summary = "; ".join(f"{m}[{k}]={'ENTERED' if e else 'skip:' + r}" for m, k, e, r in routed) or "no active eligible sessions"
    logger.info(f"[signal-route] signal={sid} inst={inst} -> {summary}")
    try:
        from app.api.routes.security import audit_log
        async with async_session_factory() as _db:
            for m, k, e, r in routed:
                await audit_log(_db, user_id, "signal_routed",
                                {"signal_id": str(sid), "mode": m, "session": k, "instrument": inst,
                                 "entered": bool(e), "reason": r}, None)
            await _db.commit()
    except Exception:
        pass
    return routed


async def run_signal_resolution_loop(interval_sec: int = 600):
    """Self-healing: periodically resolve pending SENT Email Signals so the
    backlog can't build up when nobody has the page open (frontend /stats
    polling is otherwise the only thing that drives resolution). Lazy imports
    keep this free of any import cycle with the routes module."""
    import asyncio as _aio
    await _aio.sleep(60)  # let the boot reconnect-storm settle before the first DB-heavy pass
    while True:
        try:
            from app.database import async_session_factory
            from app.api.routes.account_signals import _resolve_signal_outcomes
            async with async_session_factory() as _db:
                n = await _resolve_signal_outcomes(_db, limit=200)
                if n:
                    logger.info(f"[signals] scheduled resolution pass resolved {n}")
        except Exception as _e:
            logger.warning(f"[signals] scheduled resolution loop error: {_e}")
        await _aio.sleep(interval_sec)

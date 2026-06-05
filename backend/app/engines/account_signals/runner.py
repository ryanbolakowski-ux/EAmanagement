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


def _fetch_bars_uncached(instrument: str, timeframe: str, count: int = 50):
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


# ── Shared bar cache ────────────────────────────────────────────────────
# Dedupe identical (instrument, timeframe, count) fetches across ALL watchers
# within a short TTL. Without this, N watchers each re-read candle_cache +
# re-resample the same ES/NQ bars every poll cycle, bursting CPU. The bars are
# read-only downstream (the watcher appends them to its own buffer and builds a
# DataFrame; it never mutates the bar dicts), so handing out a shallow copy of
# the cached list is safe. Effect: ~N fetches/min -> ~2/min per (inst, tf).
_BAR_CACHE: dict = {}
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
                    SELECT w.is_active, s.status
                      FROM account_signal_watchers w
                      LEFT JOIN strategies s ON s.id = w.strategy_id
                     WHERE w.id = :id
                """), {"id": watcher_id})
                row = r.fetchone()
                if not row or not row[0]:
                    logger.info(f"[Signals] watcher {watcher_id} deactivated")
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
            result = send_signal_email(
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
        async with async_session_factory() as db:
            await db.execute(text("""
                UPDATE account_signals
                   SET status = :st,
                       provider_sent_at = :psa,
                       delivered_at = :psa,
                       provider_message_id = :pid,
                       provider_status = :pstatus,
                       latency_seconds = :lat,
                       error_message = :err
                 WHERE id = :id
            """), {
                "st": final_status, "psa": provider_sent_at,
                "pid": result.get("provider_message_id"),
                "pstatus": result.get("provider_status"),
                "lat": latency, "err": result.get("error"), "id": sid,
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

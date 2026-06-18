"""Shared per-session entry-guard for paper / live / options-paper runners.

Centralizes the four overtrade rules so every execution engine enforces them
identically:

  1. Per-session COOLDOWN — refuse new entries within N minutes of the
     previous entry on the same session.
  2. MAX_TRADES_PER_DAY — refuse entries once today's trade count hits
     the strategy's cap.
  3. MAX_OPEN_POSITIONS — refuse entries when the session already holds
     N open positions (across all instruments).
  4. PER-INSTRUMENT DUP — refuse entry if a position on this instrument
     is already open in the session (e.g. don't add another NQ while NQ
     is open).

All checks query the live `trades` and `strategies` tables; no in-memory
state needed, so the guard survives restarts and is shared across the
runner's per-instrument tasks.

Returns a `Decision(allowed, reason, debug)` so the caller can log a single
clear line per signal.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import text

from app.database import async_session_factory


# ── Default knobs (env-overridable, used when the strategy row has nothing) ──
# Futures default cooldown is 5 min, stocks/options 15 min — most ICT setups
# stay valid for several minutes, so re-entering on the next bar is almost
# always over-trading the same setup.
DEFAULT_COOLDOWN_MIN_FUTURES = int(os.environ.get("PAPER_COOLDOWN_MIN_FUTURES", "5"))
DEFAULT_COOLDOWN_MIN_STOCKS = int(os.environ.get("PAPER_COOLDOWN_MIN_STOCKS", "15"))
DEFAULT_MAX_TRADES_PER_DAY = int(os.environ.get("PAPER_MAX_TRADES_PER_DAY", "6"))
# ENTRY-GUARD-BARCLOCK-V1: refuse re-entering the SAME price level on the
# same (session, instrument, direction) within this many minutes.
SAME_PRICE_LOCKOUT_MIN = int(os.environ.get("PAPER_SAME_PRICE_LOCKOUT_MIN", "30"))
DEFAULT_MAX_OPEN_POSITIONS_FUTURES = int(os.environ.get("PAPER_MAX_OPEN_POSITIONS_FUTURES", "1"))
DEFAULT_MAX_OPEN_POSITIONS_STOCKS = int(os.environ.get("PAPER_MAX_OPEN_POSITIONS_STOCKS", "3"))

FUTURES_INSTRUMENTS = {"ES", "NQ", "RTY", "YM", "MES", "MNQ", "M2K", "MYM"}


@dataclass
class Decision:
    allowed: bool
    reason: str
    debug: dict


def _is_futures(instrument: str) -> bool:
    return (instrument or "").upper() in FUTURES_INSTRUMENTS


async def ensure_strategy_columns() -> None:
    """Idempotent ALTER TABLE so cooldown_min and max_open_positions exist.
    Run once per process on first guard call — cached so we don't re-issue
    DDL on every signal."""
    if getattr(ensure_strategy_columns, "_done", False):
        return
    try:
        async with async_session_factory() as db:
            await db.execute(text(
                "ALTER TABLE strategies "
                "ADD COLUMN IF NOT EXISTS cooldown_min INTEGER DEFAULT 5"
            ))
            await db.execute(text(
                "ALTER TABLE strategies "
                "ADD COLUMN IF NOT EXISTS max_open_positions INTEGER DEFAULT 1"
            ))
            await db.execute(text(
                "ALTER TABLE strategies "
                "ADD COLUMN IF NOT EXISTS breakeven_at_r DOUBLE PRECISION DEFAULT 0.0"
            ))
            await db.execute(text(
                "ALTER TABLE strategies "
                "ADD COLUMN IF NOT EXISTS breakeven_mode VARCHAR(16) DEFAULT 'off'"
            ))
            await db.commit()
        ensure_strategy_columns._done = True  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning(f"[entry-guard] ensure_strategy_columns failed: {e}")


async def _get_strategy_limits(strategy_id: str, instrument: str) -> dict:
    """Pull cooldown_min, max_trades_per_day, max_open_positions for this
    strategy, falling back to env defaults appropriate to the instrument family."""
    fut = _is_futures(instrument)
    default_cool = DEFAULT_COOLDOWN_MIN_FUTURES if fut else DEFAULT_COOLDOWN_MIN_STOCKS
    default_max_open = DEFAULT_MAX_OPEN_POSITIONS_FUTURES if fut else DEFAULT_MAX_OPEN_POSITIONS_STOCKS
    default_max_day = DEFAULT_MAX_TRADES_PER_DAY
    try:
        async with async_session_factory() as db:
            row = (await db.execute(text("""
                SELECT cooldown_min, max_trades_per_day, max_open_positions
                  FROM strategies WHERE id = :sid
            """), {"sid": strategy_id})).fetchone()
        if not row:
            return {"cooldown_min": default_cool, "max_trades_per_day": default_max_day,
                    "max_open_positions": default_max_open}
        cool = int(row[0]) if row[0] is not None else default_cool
        max_day = int(row[1]) if row[1] is not None else default_max_day
        max_open = int(row[2]) if row[2] is not None else default_max_open
        return {"cooldown_min": cool, "max_trades_per_day": max_day,
                "max_open_positions": max_open}
    except Exception as e:
        logger.warning(f"[entry-guard] strategy-limits read failed sid={strategy_id}: {e}")
        return {"cooldown_min": default_cool, "max_trades_per_day": default_max_day,
                "max_open_positions": default_max_open}


async def can_enter(*, session_id: str, strategy_id: str, instrument: str,
                    direction: str, mode: str = "paper",
                    open_positions_snapshot: Optional[list] = None,
                    bar_time: Optional[datetime] = None,
                    entry_price: Optional[float] = None) -> Decision:
    """Run all four checks. Returns Decision(allowed=False, reason=...) on
    first failure; Decision(allowed=True, reason='ok', debug={...}) when
    all checks pass. Logs an info line at every decision point so an operator
    can trace why a signal was/wasn't acted on.

    `open_positions_snapshot` (optional) is a list of dicts each with at
    least {'session_id', 'instrument'}. When provided, max_open_positions
    and per-instrument duplicate checks use it (in-memory, survives the
    paper runner's policy of only persisting closed trades). When omitted
    the guard falls back to DB-queried open trades — useful for engines
    that DO persist open rows (live, options-paper).
    """
    await ensure_strategy_columns()
    limits = await _get_strategy_limits(strategy_id, instrument)
    cooldown_min = limits["cooldown_min"]
    max_day = limits["max_trades_per_day"]
    max_open = limits["max_open_positions"]
    sid = session_id
    inst = (instrument or "").upper()
    direction = (direction or "").lower()
    now = datetime.now(timezone.utc)
    # ENTRY-GUARD-BARCLOCK-V1 reference clock. The paper runner replays
    # yfinance bars that lag wall-clock ~10-15min; comparing a stale
    # entry_time to now() made the cooldown a no-op. Use the candidate
    # bar's own clock when provided (live/options pass real-time bars).
    ref_now = now
    if bar_time is not None:
        try:
            _bt = bar_time
            if getattr(_bt, "tzinfo", None) is None:
                _bt = _bt.replace(tzinfo=timezone.utc)
            ref_now = _bt
        except Exception:
            ref_now = now

    logger.info(
        f"[paper-runner] sid={sid} considering entry inst={inst} dir={direction} "
        f"limits cooldown={cooldown_min}m max_day={max_day} max_open={max_open}"
    )

    try:
        async with async_session_factory() as db:
            # ── Open-position checks: prefer the in-memory snapshot when the
            # caller provided one (paper runner doesn't persist open trades),
            # otherwise fall back to DB-side open trades (live, options-paper).
            if open_positions_snapshot is not None:
                same_inst = [p for p in open_positions_snapshot
                             if (p.get("instrument") or "").upper() == inst]
                n_open = len(open_positions_snapshot)
                if same_inst:
                    logger.info(
                        f"[paper-runner] sid={sid} REJECTED reason=duplicate_instrument "
                        f"inst={inst} (in-memory snapshot)"
                    )
                    return Decision(False, "duplicate_instrument",
                                    {"open_trade_count": len(same_inst)})
                if n_open >= max_open:
                    logger.info(
                        f"[paper-runner] sid={sid} REJECTED reason=max_open_positions "
                        f"open={n_open} limit={max_open} (in-memory snapshot)"
                    )
                    return Decision(False, "max_open_positions",
                                    {"open": int(n_open), "limit": max_open})
            else:
                # ── Per-instrument duplicate-entry guard via DB ──────────
                dup_row = (await db.execute(text("""
                    SELECT id, entry_time FROM trades
                     WHERE session_id = :sid
                       AND instrument = :inst
                       AND status = 'open'
                     ORDER BY entry_time DESC LIMIT 1
                """), {"sid": sid, "inst": inst})).fetchone()
                if dup_row:
                    logger.info(
                        f"[paper-runner] sid={sid} REJECTED reason=duplicate_instrument "
                        f"inst={inst} already_open_trade={dup_row[0]} since={dup_row[1]}"
                    )
                    return Decision(False, "duplicate_instrument",
                                    {"open_trade_id": str(dup_row[0])})

                # ── Max open positions across all instruments via DB ─────
                n_open = (await db.execute(text("""
                    SELECT count(*) FROM trades
                     WHERE session_id = :sid AND status = 'open'
                """), {"sid": sid})).scalar() or 0
                if n_open >= max_open:
                    logger.info(
                        f"[paper-runner] sid={sid} REJECTED reason=max_open_positions "
                        f"open={n_open} limit={max_open}"
                    )
                    return Decision(False, "max_open_positions",
                                    {"open": int(n_open), "limit": max_open})

            # ── Max trades per day (entry_time within today UTC) ─────────
            today_start = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
            n_today = (await db.execute(text("""
                SELECT count(*) FROM trades
                 WHERE session_id = :sid AND entry_time >= :start
            """), {"sid": sid, "start": today_start})).scalar() or 0
            if n_today >= max_day:
                logger.info(
                    f"[paper-runner] sid={sid} REJECTED reason=max_trades_per_day "
                    f"at={n_today} limit={max_day}"
                )
                return Decision(False, "max_trades_per_day",
                                {"today": int(n_today), "limit": max_day})

            # ── Same-price re-entry lockout (ENTRY-GUARD-BARCLOCK-V1) ────
            # A setup that just closed must not immediately reopen at the
            # same price level. Consults recently-CLOSED trades too (the
            # paper engine persists only closed rows), keyed on the rounded
            # entry price within SAME_PRICE_LOCKOUT_MIN.
            if entry_price is not None:
                try:
                    _band = round(float(entry_price))
                    _lock_min = max(cooldown_min, SAME_PRICE_LOCKOUT_MIN)
                    _since = ref_now - timedelta(minutes=_lock_min)
                    _recent = (await db.execute(text("""
                        SELECT entry_price, entry_time FROM trades
                         WHERE session_id = :sid AND instrument = :inst
                           AND direction = :dir AND entry_time >= :since
                         ORDER BY entry_time DESC LIMIT 20
                    """), {"sid": sid, "inst": inst, "dir": direction,
                           "since": _since})).fetchall()
                    for _rp in _recent:
                        if _rp[0] is not None and round(float(_rp[0])) == _band:
                            logger.info(
                                f"[paper-runner] sid={sid} REJECTED reason=same_price_reentry "
                                f"inst={inst} dir={direction} price~{_band} "
                                f"prev_entry={_rp[1]} lockout={_lock_min}m"
                            )
                            return Decision(False, "same_price_reentry",
                                            {"price_band": _band, "lockout_min": _lock_min})
                except Exception as _spe:
                    logger.warning(f"[entry-guard] same-price check skipped sid={sid}: {_spe}")

            # ── Cooldown since last entry on this session ────────────────
            last_entry = (await db.execute(text("""
                SELECT entry_time FROM trades
                 WHERE session_id = :sid
                 ORDER BY entry_time DESC LIMIT 1
            """), {"sid": sid})).fetchone()
            if last_entry and last_entry[0]:
                last_t = last_entry[0]
                # Normalize to aware datetime in UTC
                if last_t.tzinfo is None:
                    last_t = last_t.replace(tzinfo=timezone.utc)
                elapsed = (ref_now - last_t).total_seconds()
                cooldown_s = cooldown_min * 60
                if elapsed < cooldown_s:
                    logger.info(
                        f"[paper-runner] sid={sid} REJECTED reason=cooldown "
                        f"last_entry={last_t.isoformat()} seconds_ago={elapsed:.0f} "
                        f"elapsed={elapsed:.0f}s < cooldown={cooldown_s}s"
                    )
                    return Decision(False, "cooldown",
                                    {"elapsed_s": int(elapsed),
                                     "cooldown_s": int(cooldown_s)})
    except Exception as e:
        # Fail OPEN on infra hiccup — we'd rather miss a guard than block
        # all paper trading. But log loudly.
        logger.error(f"[paper-runner] sid={sid} entry-guard CRASHED, failing open: {e}")
        return Decision(True, "guard_crashed_fail_open", {"error": str(e)})

    logger.info(
        f"[paper-runner] sid={sid} ALLOWED inst={inst} dir={direction} "
        f"open={n_open}/{max_open} today={n_today}/{max_day} cooldown={cooldown_min}m_clear"
    )
    return Decision(True, "ok", {
        "open": int(n_open), "max_open": max_open,
        "today": int(n_today), "max_day": max_day,
        "cooldown_min": cooldown_min,
    })

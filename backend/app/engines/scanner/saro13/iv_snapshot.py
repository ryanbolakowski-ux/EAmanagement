"""Saro 1.3 S3 — nightly ATM-IV snapshots into Postgres + the IV-Rank gate.

IV RANK REALITY (documented ramp): no 52-week IV history exists anywhere on
this stack, so Saro 1.3 BUILDS it: one ATM-IV row per screened candidate
per night (18:30-21:00 ET window, same policy as saro12's OI job) into the
durable Postgres table `saro_iv_history` (CREATE TABLE IF NOT EXISTS,
idempotent — NOT redis: the appendonly=off durability rule forbids redis
for accumulating history). The spec's "IV Rank > 50% vs 52-week history"
gate applies only once a ticker has IV_HISTORY_MIN_SESSIONS (20) rows;
below that it FAILS OPEN with a LOUD 'iv-history-warming' warning — that
warning line is the ONLY tell that the gate is not yet live, so it is
logged at WARNING, not debug. True 52-week rank arrives as history
accumulates (~252 sessions); the readout must treat early rows as
rank-ungated.

TENOR RULE (pinned so the series is comparable day over day — the raw
expirations list starts with 0DTE): snapshot the nearest MONTHLY
(third-Friday) expiration with >= IV_TENOR_MIN_DTE (7) DTE; when no monthly
is listed, the nearest expiration >= 7 DTE; last resort, the furthest
listed. ATM = strike nearest the underlying last; IV = mean of the
populated call/put IVs at that strike, reading greeks.mid_iv when > 0 and
falling back to greeks.smv_vol (ORATS smoothed vol) — probed live: illiquid
contracts return mid_iv/bid_iv/ask_iv/delta all 0.0 while smv_vol stays
populated. Dead strikes walk outward up to IV_STRIKE_WALK strikes.

READ-ONLY BROKER USE — SANDBOX ONLY (2026-08-06 incident rule): this module
selects a SANDBOX/DEMO tradier BrokerAccount only, calls ONLY market-data
reads (get_quotes / get_option_expirations / get_option_chain), and ALWAYS
disconnect()s in finally. Zero sandbox accounts -> skip with a log
(fail-open) — note that if the single sandbox account is ever deactivated,
history stops accumulating and the IV gate silently stays in warming
fail-open; the WARNING log is the tell.

SHADOW ONLY: no emails, no orders, no routing.
"""
from __future__ import annotations

import json
from loguru import logger  # stdlib INFO invisible in prod
import os
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.engines.scanner.saro13 import config as C

# (loguru imported above)


# ── pure tenor / ATM / rank math (unit-tested, no I/O) ─────────────────────
def third_friday(year: int, month: int) -> date:
    """The month's third Friday (standard monthly option expiration)."""
    d15 = date(year, month, 15)
    return d15 + timedelta(days=(4 - d15.weekday()) % 7)  # Fri between 15-21


def is_monthly_expiration(exp: str) -> bool:
    """True when `exp` (YYYY-MM-DD) is a third-Friday monthly."""
    try:
        d = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    return d == third_friday(d.year, d.month)


def pick_monthly_expiration(exps: list, today: date,
                            min_dte: int = C.IV_TENOR_MIN_DTE) -> Optional[str]:
    """PINNED tenor rule: nearest MONTHLY (third-Friday) expiration with
    DTE >= min_dte; else nearest expiration >= min_dte; else the furthest
    listed; None when nothing parses."""
    parsed: list[tuple[date, str]] = []
    for e in exps or []:
        try:
            parsed.append((datetime.strptime(str(e)[:10], "%Y-%m-%d").date(), str(e)[:10]))
        except Exception:
            continue
    if not parsed:
        return None
    parsed.sort()
    monthlies = [s for d, s in parsed
                 if (d - today).days >= min_dte and d == third_friday(d.year, d.month)]
    if monthlies:
        return monthlies[0]
    later = [s for d, s in parsed if (d - today).days >= min_dte]
    if later:
        return later[0]
    return parsed[-1][1]


def _pos(v: Any) -> float:
    try:
        f = float(v)
        return f if f > 0 else 0.0
    except Exception:
        return 0.0


def atm_iv_from_chain(chain: Optional[list], last: Optional[float]) -> Optional[dict]:
    """ATM IV from a greeks=true Tradier chain: strike nearest `last`, IV =
    mean of the populated call/put IVs there (mid_iv when > 0, else
    smv_vol). Dead strikes (all zeros) walk outward up to IV_STRIKE_WALK.
    Returns {strike, iv, source} or None."""
    if not chain or last is None or _pos(last) <= 0:
        return None
    by_strike: dict[float, list] = {}
    for opt in chain:
        try:
            k = float(opt.get("strike") or 0)
            if k > 0:
                by_strike.setdefault(k, []).append(opt)
        except Exception:
            continue
    if not by_strike:
        return None
    for strike in sorted(by_strike, key=lambda k: abs(k - float(last)))[:C.IV_STRIKE_WALK]:
        ivs: list[float] = []
        sources: set[str] = set()
        for opt in by_strike[strike]:
            g = opt.get("greeks") or {}
            mid = _pos(g.get("mid_iv"))
            smv = _pos(g.get("smv_vol"))
            if mid > 0:
                ivs.append(mid)
                sources.add("mid_iv")
            elif smv > 0:
                ivs.append(smv)
                sources.add("smv_vol")
        if ivs:
            return {"strike": strike, "iv": sum(ivs) / len(ivs),
                    "source": "+".join(sorted(sources))}
    return None


def compute_iv_rank(ivs: list) -> Optional[float]:
    """IV Rank of the LATEST value vs the whole accumulated series:
    (current - min) / (max - min) * 100. None on empty/degenerate input
    (max == min carries no rank information)."""
    try:
        vals = [float(v) for v in ivs if v is not None and float(v) > 0]
    except Exception:
        return None
    if len(vals) < 2:
        return None
    cur, lo, hi = vals[-1], min(vals), max(vals)
    if hi <= lo:
        return None
    return (cur - lo) / (hi - lo) * 100.0


# ── durable accumulation table (idempotent — no migration tooling here) ────
_CREATE_IV_TABLE = f"""
CREATE TABLE IF NOT EXISTS {C.IV_TABLE} (
    id bigserial PRIMARY KEY,
    snap_date date NOT NULL,
    ticker text NOT NULL,
    expiration date,
    dte integer,
    atm_strike numeric,
    iv numeric NOT NULL,
    iv_source text,
    underlying_price numeric,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT saro_iv_history_day_ticker UNIQUE (snap_date, ticker)
)"""


async def _ensure_iv_table(db) -> None:
    from sqlalchemy import text as _t
    await db.execute(_t(_CREATE_IV_TABLE))
    await db.commit()


async def iv_rank_gate(db, ticker: str) -> tuple[bool, str]:
    """S3 IV-Rank gate for the morning scan. (ok, note):
      >= IV_HISTORY_MIN_SESSIONS rows -> require rank >= IV_RANK_MIN
      fewer rows -> FAIL OPEN with the LOUD 'iv-history-warming' warning
    Never raises (gate errors fail open with a note — a broken gate must
    not zero the cohort)."""
    tk = str(ticker).upper()
    try:
        from sqlalchemy import text as _t
        await _ensure_iv_table(db)
        rows = (await db.execute(_t(
            f"SELECT iv FROM {C.IV_TABLE} WHERE ticker = :tk ORDER BY snap_date ASC"
        ), {"tk": tk})).all()
        ivs = [float(r0[0]) for r0 in rows]
        n = len(ivs)
        if n < C.IV_HISTORY_MIN_SESSIONS:
            logger.warning(
                f"[saro13-iv] {tk}: iv-history-warming ({n}/"
                f"{C.IV_HISTORY_MIN_SESSIONS} sessions) — IV-Rank gate FAIL-OPEN "
                f"(rank applies once history accumulates; ~252 sessions = true 52-week)")
            return True, (f"IV Rank UNGATED: iv-history-warming ({n}/"
                          f"{C.IV_HISTORY_MIN_SESSIONS} sessions) — fail-open")
        rank = compute_iv_rank(ivs)
        if rank is None:
            logger.warning(f"[saro13-iv] {tk}: degenerate IV history ({n} rows) — fail-open")
            return True, f"IV Rank incomputable over {n} sessions — fail-open"
        if rank > C.IV_RANK_MIN:  # spec: ABOVE 50%, strict
            return True, (f"IV Rank {rank:.1f} >= {C.IV_RANK_MIN:g} "
                          f"(vs {n}-session accumulated history)")
        return False, f"IV Rank {rank:.1f} < {C.IV_RANK_MIN:g} ({n}-session history)"
    except Exception as e:
        logger.warning(f"[saro13-iv] rank gate {tk} failed ({e}) — fail-open")
        return True, f"IV Rank gate error ({e}) — fail-open"


# ── nightly snapshot job ───────────────────────────────────────────────────
async def _find_tradier_account(db):
    """SANDBOX/DEMO tradier BrokerAccount ONLY. We never open a session with a
    customer's LIVE brokerage credentials for background data reads — if no
    sandbox account exists the nightly snapshot skips with a log (owner rule,
    2026-08-06 verify finding)."""
    from sqlalchemy import select
    from app.models.user import BrokerAccount
    rows = (await db.execute(
        select(BrokerAccount).where(BrokerAccount.broker == "tradier",
                                    BrokerAccount.is_active == True)  # noqa: E712
    )).scalars().all()
    rows = [a for a in rows if bool(a.is_demo or a.sandbox_mode)]
    if not rows:
        return None
    return rows[0]


async def snapshot_ticker_iv(broker, ticker: str, today: date) -> Optional[dict]:
    """One ATM-IV observation for `ticker` (READ-ONLY chain fetch). Returns
    {ticker, expiration, dte, atm_strike, iv, iv_source, underlying_price}
    or None when the chain/IV is unavailable."""
    try:
        tk = str(ticker).upper()
        quotes = await broker.get_quotes([tk])
        q = (quotes or {}).get(tk) or {}
        last = q.get("last") or q.get("close") or q.get("prevclose")
        last = float(last) if last else None
        if not last or last <= 0:
            return None
        exps = await broker.get_option_expirations(tk)
        exp = pick_monthly_expiration(exps, today)
        if not exp:
            return None
        chain = await broker.get_option_chain(tk, exp, include_greeks=True)
        atm = atm_iv_from_chain(chain, last)
        if not atm:
            return None
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        return {"ticker": tk, "expiration": exp, "dte": dte,
                "atm_strike": atm["strike"], "iv": atm["iv"],
                "iv_source": atm["source"], "underlying_price": last}
    except Exception as e:
        logger.warning(f"[saro13-iv] snapshot {ticker} failed: {e}")
        return None


async def run_iv_snapshot_once(*, redis_client=None) -> dict:
    """Nightly job (18:30-21:00 ET window via the scheduler hook): redis
    latch theta:saro13:iv_done:{date}; load today's screened candidates;
    write ONE saro_iv_history row per ticker/day (ON CONFLICT DO NOTHING —
    idempotent). SHADOW-SIDE DATA ONLY. Never raises."""
    r = redis_client
    own_r = r is None
    broker = None
    try:
        if os.environ.get(C.ENV_GATE, "1") != "1":
            return {"status": "disabled"}
        from app.engines.options.ignition_shadow import _now_et
        et = _now_et()
        if et.weekday() >= 5:
            return {"status": "weekend"}
        date_key = et.strftime("%Y-%m-%d")
        if r is None:
            import redis.asyncio as _redis
            r = _redis.from_url(os.environ.get(C.REDIS_URL_ENV, C.REDIS_URL_DEFAULT),
                                decode_responses=True)
        try:
            got = await r.set(C.LATCH_IV_FMT.format(date=date_key), "running",
                              ex=C.LATCH_TTL_S, nx=True)
        except Exception as e:
            logger.warning(f"[saro13-iv] redis latch failed ({e}) — skip (no dup risk)")
            return {"status": "no-redis"}
        if not got:
            return {"status": "latched"}

        try:
            raw = await r.get(C.CANDIDATES_KEY_FMT.format(date=date_key))
            cands = json.loads(raw) if raw else []
        except Exception:
            cands = []
        if not isinstance(cands, list) or not cands:
            logger.info(f"[saro13-iv] no candidates for {date_key} — exit")
            return {"status": "no-candidates"}

        from app.database import async_session_factory
        from app.engines.live_trading.broker_factory import build_broker_from_account
        async with async_session_factory() as db:
            await _ensure_iv_table(db)
            acct = await _find_tradier_account(db)
        if acct is None:
            logger.warning("[saro13-iv] no active SANDBOX tradier BrokerAccount — "
                           "skip (fail-open; IV history is NOT accumulating)")
            return {"status": "no-tradier-account"}
        broker = build_broker_from_account(acct)
        if broker is None or not await broker.connect():
            logger.warning("[saro13-iv] tradier connect failed — skip")
            return {"status": "connect-failed"}

        today = et.date()
        snaps: list[dict] = []
        for tk in [str(t).upper() for t in cands][:C.IV_SNAPSHOT_CAP]:
            snap = await snapshot_ticker_iv(broker, tk, today)
            if snap:
                snaps.append(snap)

        wrote = 0
        if snaps:
            from sqlalchemy import text as _t
            async with async_session_factory() as db:
                await _ensure_iv_table(db)
                for s in snaps:
                    try:
                        await db.execute(_t(f"""
                            INSERT INTO {C.IV_TABLE}
                              (snap_date, ticker, expiration, dte, atm_strike,
                               iv, iv_source, underlying_price)
                            VALUES (:d, :tk, :exp, :dte, :k, :iv, :src, :u)
                            ON CONFLICT ON CONSTRAINT saro_iv_history_day_ticker
                            DO NOTHING
                        """), {"d": date_key, "tk": s["ticker"], "exp": s["expiration"],
                               "dte": s["dte"], "k": s["atm_strike"], "iv": s["iv"],
                               "src": s["iv_source"], "u": s["underlying_price"]})
                        wrote += 1
                    except Exception as e:
                        logger.warning(f"[saro13-iv] insert {s.get('ticker')} failed: {e}")
                await db.commit()
        logger.info(f"[saro13-iv] {date_key}: snapped {len(snaps)}/{len(cands)} "
                    f"candidates, wrote {wrote} rows into {C.IV_TABLE}")
        return {"status": "done", "snapped": len(snaps), "wrote": wrote,
                "candidates": len(cands)}
    except Exception as e:  # fail-open — a shadow-side job must never break prod
        logger.error(f"[saro13-iv] run failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if broker is not None:
            try:
                await broker.disconnect()
            except Exception:
                pass
        if own_r and r is not None:
            try:
                await r.aclose()
            except Exception:
                pass

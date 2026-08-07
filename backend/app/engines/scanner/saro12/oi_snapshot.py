"""Saro 1.2 Section-3 (free part) — nightly Tradier OI snapshots + surge flag.

Per screened candidate, snapshot open interest for OTM CALLS on the nearest
OI_EXPIRATIONS_PER_TICKER expirations into redis (saro12:oi:{date}:{ticker}
-> {occ_symbol: open_interest}); flag a > OI_SURGE_PCT (50%) 24h surge in
total OTM-call OI. Zero FMP budget — Tradier market-data reads only.

READ-ONLY BROKER USE: this module calls ONLY Tradier market-data endpoints
(quotes, expirations, chains) through the existing TradierBroker client.
It never imports or touches any order/entry method. Auth: there is NO
global Tradier env token — credentials are per-account encrypted
BrokerAccount rows, so we select an ACTIVE tradier account (prefer
sandbox/demo for data reads), connect(), read, and ALWAYS disconnect() in
finally. Zero tradier accounts -> skip with a log (fail-open).

# ── SWEEPS / BLOCKS HOOK (NOT BUILT — paid feed required) ─────────────────
# Ryan is evaluating a paid options-flow feed (sweeps, block prints,
# aggressor side). When one lands, plug it in here:
#   async def fetch_sweeps(ticker: str, date_key: str) -> list[dict]: ...
# and merge its output into build_oi_flags() so quality_reasons can carry
# e.g. "call sweep $250k @ ask". Until then OI deltas from the free
# Tradier chain are the only Section-3 signal. DO NOT approximate sweeps
# from OI/volume — the owner explicitly deferred this.
# ──────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Optional

from app.engines.scanner.saro12 import config as C

logger = logging.getLogger(__name__)


# ── pure surge math (unit-tested, no I/O) ──────────────────────────────────
def compute_oi_surge(prior: Optional[dict], today: Optional[dict]) -> Optional[float]:
    """Percent change in TOTAL OTM-call OI between two snapshots
    ({occ_symbol: open_interest}). None when no prior baseline exists
    (can't call a surge without yesterday's number)."""
    if not prior or not today:
        return None
    try:
        p = sum(float(v or 0) for v in prior.values())
        t = sum(float(v or 0) for v in today.values())
    except Exception:
        return None
    if p <= 0:
        return None
    return (t - p) / p * 100.0


def is_oi_surge(pct: Optional[float]) -> bool:
    return pct is not None and pct > C.OI_SURGE_PCT


# ── redis access ───────────────────────────────────────────────────────────
def _redis():
    import redis.asyncio as _redis
    return _redis.from_url(os.environ.get(C.REDIS_URL_ENV, C.REDIS_URL_DEFAULT),
                           decode_responses=True)


async def _get_json(r, key: str) -> Optional[dict]:
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def otm_call_oi_surge(ticker: str, *, r=None) -> Optional[float]:
    """24h OTM-call OI surge %% for `ticker` from the latest two snapshots
    within OI_SURGE_MAX_LOOKBACK_DAYS. None when < 2 snapshots exist."""
    own_r = r is None
    if r is None:
        r = _redis()
    try:
        from app.engines.options.ignition_shadow import _now_et
        now = _now_et()
        snaps: list[dict] = []
        for d in range(C.OI_SURGE_MAX_LOOKBACK_DAYS + 1):
            day = (now - timedelta(days=d)).strftime("%Y-%m-%d")
            snap = await _get_json(r, C.OI_KEY_FMT.format(date=day, ticker=ticker.upper()))
            if snap:
                snaps.append(snap)
            if len(snaps) == 2:
                break
        if len(snaps) < 2:
            return None
        return compute_oi_surge(prior=snaps[1], today=snaps[0])
    except Exception as e:
        logger.warning(f"[saro12-oi] surge check {ticker} failed: {e}")
        return None
    finally:
        if own_r:
            try:
                await r.aclose()
            except Exception:
                pass


# ── nightly snapshot ───────────────────────────────────────────────────────
async def _find_tradier_account(db):
    """Any active tradier BrokerAccount, preferring sandbox/demo for data
    reads (chains/OI are identical either way)."""
    from sqlalchemy import select
    from app.models.user import BrokerAccount
    rows = (await db.execute(
        select(BrokerAccount).where(BrokerAccount.broker == "tradier",
                                    BrokerAccount.is_active == True)  # noqa: E712
    )).scalars().all()
    if not rows:
        return None
    rows.sort(key=lambda a: (not bool(a.is_demo or a.sandbox_mode)))
    return rows[0]


async def snapshot_ticker_oi(broker, ticker: str) -> Optional[dict]:
    """{occ_symbol: open_interest} for OTM calls on the nearest expirations.
    READ-ONLY. None when the chain is unavailable."""
    try:
        quotes = await broker.get_quotes([ticker])
        q = (quotes or {}).get(ticker) or {}
        last = q.get("last") or q.get("close") or q.get("prevclose")
        last = float(last) if last else None
        exps = await broker.get_option_expirations(ticker)
        if not exps:
            return None
        oi_map: dict[str, float] = {}
        for exp in exps[:C.OI_EXPIRATIONS_PER_TICKER]:
            chain = await broker.get_option_chain(ticker, exp, include_greeks=False)
            for opt in chain or []:
                try:
                    if str(opt.get("option_type", "")).lower() != "call":
                        continue
                    strike = float(opt.get("strike") or 0)
                    if last is not None and strike <= last:
                        continue  # OTM calls only (unknown last -> keep all calls)
                    occ = str(opt.get("symbol") or f"{ticker}:{exp}:{strike}")
                    oi_map[occ] = float(opt.get("open_interest") or 0)
                except Exception:
                    continue
        return oi_map or None
    except Exception as e:
        logger.warning(f"[saro12-oi] chain snapshot {ticker} failed: {e}")
        return None


async def run_oi_snapshot_once(*, redis_client=None) -> dict:
    """Nightly job: redis latch theta:saro12:oi_done:{date}; load today's
    screened candidates; snapshot OTM-call OI per candidate. SHADOW-SIDE
    DATA ONLY — no emails, no orders, no routing. Never raises."""
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
            r = _redis()
        try:
            got = await r.set(C.LATCH_OI_FMT.format(date=date_key), "running",
                              ex=C.LATCH_TTL_S, nx=True)
        except Exception as e:
            logger.warning(f"[saro12-oi] redis latch failed ({e}) — skip (no dup risk)")
            return {"status": "no-redis"}
        if not got:
            return {"status": "latched"}

        cands = await _get_json(r, C.CANDIDATES_KEY_FMT.format(date=date_key)) or []
        if not isinstance(cands, list) or not cands:
            logger.info(f"[saro12-oi] no candidates for {date_key} — exit")
            return {"status": "no-candidates"}

        from app.database import async_session_factory
        from app.engines.live_trading.broker_factory import build_broker_from_account
        async with async_session_factory() as db:
            acct = await _find_tradier_account(db)
        if acct is None:
            logger.info("[saro12-oi] no active tradier BrokerAccount — skip (fail-open)")
            return {"status": "no-tradier-account"}
        broker = build_broker_from_account(acct)
        if broker is None or not await broker.connect():
            logger.warning("[saro12-oi] tradier connect failed — skip")
            return {"status": "connect-failed"}

        done = 0
        for tk in [str(t).upper() for t in cands][:C.ENRICH_TOP_N]:
            snap = await snapshot_ticker_oi(broker, tk)
            if snap is None:
                continue
            try:
                await r.set(C.OI_KEY_FMT.format(date=date_key, ticker=tk),
                            json.dumps(snap), ex=C.OI_SNAPSHOT_TTL_S)
                done += 1
            except Exception as e:
                logger.warning(f"[saro12-oi] store {tk} failed: {e}")
        logger.info(f"[saro12-oi] {date_key}: snapped {done}/{len(cands)} candidates")
        return {"status": "done", "snapped": done, "candidates": len(cands)}
    except Exception as e:  # fail-open — a shadow-side job must never break prod
        logger.error(f"[saro12-oi] run failed: {e}")
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

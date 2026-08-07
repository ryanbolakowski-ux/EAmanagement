"""Saro 1.2 Section-1 screen — bulk FMP screener -> anti-junk -> capped list.

FMP BUDGET (worst case per trading day, all /stable, all redis-cached
per-ET-day so a same-day rerun costs ~0):
    1   company-screener (one page, client-side exchange filter)
  + 0   yfinance pre-rank (float + 3-month avg volume + options-chain check
        for up to PRE_ENRICH_CAP names — zero FMP budget)
  + 60  enrichment: ENRICH_TOP_N(15) x FMP_CALLS_PER_ENRICH(4)
        (shares-float, profile, insider-trading/statistics,
         acquisition-of-beneficial-ownership)
  + 15  historical-chart/1min for the momentum gate (TTL-cached in fmp_feed)
  = 76  calls/day max  (morning funnel spends ~79 — combined ~155)

Fail-open policy (owner instruction): the ownership>10% filter is a PROXY
(13D/G beneficial owners OR insider activity present). When the proxy data
is unavailable the filter PASSES with a logged note — it must never zero
the candidate list. Same for "shortable OR options chain" when both legs
are unverifiable on this plan. Hard screens (price band, volume, RVOL,
float, market cap, exchange) fail CLOSED as usual.

SHADOW ONLY: this module reads market data and writes redis caches. It
never emails, never orders, never routes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from app.engines.scanner.saro12 import config as C

logger = logging.getLogger(__name__)


# ── pure, unit-testable filters ────────────────────────────────────────────
def normalize_exchange(row: dict) -> str:
    """Map FMP exchange spellings to 'NASDAQ' / 'NYSE' / '' (other)."""
    raw = str(row.get("exchangeShortName") or row.get("exchange") or "").upper()
    if "NASDAQ" in raw:
        return "NASDAQ"
    if raw == "NYSE" or "NEW YORK STOCK EXCHANGE" in raw:
        return "NYSE"
    return ""


def passes_base_screen(row: dict) -> tuple[bool, str]:
    """First-pass filter on one bulk-screener row. Hard screens only:
    price $1-$15, daily volume > 500k, market cap >= $30M, NASDAQ/NYSE,
    no ETFs/funds. Returns (ok, reject_reason)."""
    try:
        price = float(row.get("price") or 0)
        volume = float(row.get("volume") or 0)
        mcap = float(row.get("marketCap") or 0)
    except Exception:
        return False, "unparseable row"
    if not (C.PRICE_MIN <= price <= C.PRICE_MAX):
        return False, f"price {price} outside ${C.PRICE_MIN}-${C.PRICE_MAX}"
    if volume <= C.DAILY_VOLUME_MIN:
        return False, f"volume {volume:.0f} <= {C.DAILY_VOLUME_MIN}"
    if mcap < C.MARKET_CAP_MIN:
        return False, f"marketCap {mcap:.0f} < {C.MARKET_CAP_MIN}"
    if normalize_exchange(row) not in C.ALLOWED_EXCHANGES:
        return False, f"exchange '{row.get('exchangeShortName') or row.get('exchange')}' not NASDAQ/NYSE"
    if row.get("isEtf") is True or row.get("isFund") is True:
        return False, "ETF/fund"
    if row.get("isActivelyTrading") is False:
        return False, "not actively trading"
    return True, ""


def passes_anti_junk(cand: dict) -> tuple[bool, list[str]]:
    """Anti-junk on an ENRICHED candidate dict. Keys used:
      float_shares (float|None)      -> hard: < FLOAT_MAX_SHARES; None = reject
      market_cap (float|None)        -> hard: >= MARKET_CAP_MIN;  None = reject
      exchange (str)                 -> hard: in ALLOWED_EXCHANGES
      ownership_evidence (bool|None) -> proxy: True pass, False/None FAIL-OPEN w/ note
      has_options_chain (bool|None)  -> pair with `shortable`:
      shortable (bool|None)             either True -> pass;
                                        both False -> reject;
                                        otherwise unverifiable -> FAIL-OPEN w/ note
    Returns (ok, reasons) — reasons carry the fail-open notes into
    quality_reasons so the 2-week review can see them."""
    reasons: list[str] = []
    fs = cand.get("float_shares")
    if fs is None or float(fs) <= 0:
        return False, [f"float unknown — hard screen (< {C.FLOAT_MAX_SHARES:,}) cannot pass"]
    if float(fs) >= C.FLOAT_MAX_SHARES:
        return False, [f"float {float(fs):,.0f} >= {C.FLOAT_MAX_SHARES:,}"]
    reasons.append(f"float {float(fs):,.0f} < {C.FLOAT_MAX_SHARES:,}")

    mc = cand.get("market_cap")
    if mc is None or float(mc) < C.MARKET_CAP_MIN:
        return False, [f"marketCap {mc} < ${C.MARKET_CAP_MIN:,}"]
    reasons.append(f"marketCap {float(mc):,.0f} >= ${C.MARKET_CAP_MIN:,}")

    if cand.get("exchange") not in C.ALLOWED_EXCHANGES:
        return False, [f"exchange '{cand.get('exchange')}' not in {C.ALLOWED_EXCHANGES}"]

    ev = cand.get("ownership_evidence")
    if ev is True:
        reasons.append("ownership proxy: 13D/G or insider activity present")
    else:
        # FAIL-OPEN (owner instruction): institutional-ownership/* is
        # restricted on this plan; absence of proxy data is not junk-proof.
        reasons.append("ownership >10% UNVERIFIED (plan limits) — fail-open")

    opt, short = cand.get("has_options_chain"), cand.get("shortable")
    if opt is True or short is True:
        reasons.append("active options chain" if opt is True else "shortable")
    elif opt is False and short is False:
        return False, ["not shortable AND no options chain"]
    else:
        reasons.append("shortable/options UNVERIFIED — fail-open")
    return True, reasons


# ── redis helpers (per-ET-day caches) ──────────────────────────────────────
def _redis():
    import redis.asyncio as _redis
    return _redis.from_url(os.environ.get(C.REDIS_URL_ENV, C.REDIS_URL_DEFAULT),
                           decode_responses=True)


async def _cache_get_json(r, key: str):
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _cache_set_json(r, key: str, value, ttl_s: int = C.DAY_CACHE_TTL_S):
    try:
        await r.set(key, json.dumps(value), ex=ttl_s)
    except Exception as e:
        logger.warning(f"[saro12-screen] cache set {key} failed: {e}")


# ── data fetchers (lazy imports; every failure fails open per-candidate) ───
async def _bulk_screener(r, date_key: str) -> list[dict]:
    """ONE company-screener page (redis-cached per ET day)."""
    cached = await _cache_get_json(r, C.SCREEN_CACHE_FMT.format(date=date_key))
    if isinstance(cached, list) and cached:
        logger.info(f"[saro12-screen] screener cache hit ({len(cached)} rows, 0 FMP calls)")
        return cached
    from app.engines.data_feeds.fmp_universe import _fmp_get_json
    try:
        rows = await _fmp_get_json(C.SCREENER_URL, {
            "priceMoreThan": C.PRICE_MIN,
            "priceLowerThan": C.PRICE_MAX,
            "volumeMoreThan": C.DAILY_VOLUME_MIN,
            "marketCapMoreThan": C.MARKET_CAP_MIN,
            "limit": C.SCREENER_LIMIT,
        })
    except Exception as e:
        logger.warning(f"[saro12-screen] bulk screener failed: {e}")
        return []
    rows = rows if isinstance(rows, list) else []
    if rows:
        await _cache_set_json(r, C.SCREEN_CACHE_FMT.format(date=date_key), rows)
    return rows


async def _yf_prescreen(sym: str) -> dict:
    """Zero-FMP-budget float + 3-month avg volume via the existing yfinance
    fundamentals helper (1h TTL cache). Never raises."""
    out = {"float_shares": None, "avg_volume_3m": None, "market_cap": None}
    try:
        from app.engines.options.fundamentals import get_fundamentals
        f = await get_fundamentals(sym)
        if f:
            out["float_shares"] = f.float_shares
            out["avg_volume_3m"] = f.avg_volume_3m
            out["market_cap"] = f.market_cap
    except Exception as e:
        logger.debug(f"[saro12-screen] yf prescreen {sym} failed: {e}")
    return out


async def _yf_has_options_chain(sym: str) -> Optional[bool]:
    """Best-effort options-chain presence via yfinance expirations (zero FMP
    budget). None = unverifiable (fail-open downstream)."""
    try:
        import yfinance as yf
        exps = await asyncio.wait_for(
            asyncio.to_thread(lambda: tuple(yf.Ticker(sym).options or ())), timeout=8.0)
        return len(exps) > 0
    except Exception:
        return None


async def _fmp_enrich(r, date_key: str, sym: str) -> dict:
    """The 4 capped per-candidate FMP calls, redis-cached per ET day:
    shares-float, profile, insider-trading/statistics, 13D/G beneficial
    ownership. Cache hit = 0 FMP calls. Never raises."""
    key = C.ENRICH_CACHE_FMT.format(date=date_key, ticker=sym)
    cached = await _cache_get_json(r, key)
    if isinstance(cached, dict):
        cached["fmp_calls"] = 0
        return cached
    out: dict = {"float_shares": None, "market_cap": None, "exchange": "",
                 "avg_volume_fmp": None, "ownership_evidence": None, "fmp_calls": 0}
    from app.engines.data_feeds.fmp_universe import _fmp_get_json

    async def _safe(url, site):
        out["fmp_calls"] += 1
        try:
            return await _fmp_get_json(url, {"symbol": sym})
        except Exception as e:
            logger.debug(f"[saro12-screen] {site} {sym} failed: {e}")
            return None

    flt = await _safe(C.SHARES_FLOAT_URL, "shares-float")
    if isinstance(flt, list) and flt:
        out["float_shares"] = flt[0].get("floatShares")
    prof = await _safe(C.PROFILE_URL, "profile")
    if isinstance(prof, list) and prof:
        p = prof[0]
        out["market_cap"] = p.get("marketCap") or p.get("mktCap")
        out["exchange"] = normalize_exchange(p)
        out["avg_volume_fmp"] = p.get("volAvg") or p.get("averageVolume") or p.get("avgVolume")
    ins = await _safe(C.INSIDER_STATS_URL, "insider-stats")
    ben = await _safe(C.BENEFICIAL_URL, "beneficial-ownership")
    ins_ok = isinstance(ins, list) and len(ins) > 0
    ben_ok = isinstance(ben, list) and len(ben) > 0
    out["ownership_evidence"] = True if (ins_ok or ben_ok) else None  # None -> fail-open
    await _cache_set_json(r, key, out)
    return out


# ── the funnel ─────────────────────────────────────────────────────────────
async def run_screen(*, r=None, date_key: Optional[str] = None,
                     top_n: int = C.ENRICH_TOP_N) -> list[dict]:
    """Section-1 funnel. Returns capped candidate dicts sorted by RVOL desc:
      {ticker, price, today_vol, avg_volume_3m, rvol, float_shares,
       market_cap, exchange, ownership_evidence, has_options_chain,
       shortable, momentum_ok, momentum_detail, bars, screen_reasons,
       fmp_calls}
    Also writes the candidate ticker list to redis (CANDIDATES_KEY_FMT) for
    the nightly OI snapshot. Never raises; failures -> shorter list."""
    own_r = r is None
    if r is None:
        r = _redis()
    fmp_calls = 0
    try:
        if date_key is None:
            from app.engines.options.ignition_shadow import _now_et
            date_key = _now_et().strftime("%Y-%m-%d")

        rows = await _bulk_screener(r, date_key)
        fmp_calls += 1 if rows else 0
        base = []
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym or not sym.isalnum():
                continue
            ok, _why = passes_base_screen(row)
            if ok:
                base.append(row)
        logger.info(f"[saro12-screen] base screen: {len(base)}/{len(rows)} rows pass")

        # zero-budget pre-rank: cap by today's volume, get float + 3m avg vol
        base.sort(key=lambda x: float(x.get("volume") or 0), reverse=True)
        pre = base[:C.PRE_ENRICH_CAP]
        prescr = await asyncio.gather(*(_yf_prescreen(str(x["symbol"]).upper()) for x in pre))

        from app.engines.scanner.saro12.indicators import rvol as _rvol
        # Time-adjusted RVOL: pro-rate the 3-month-avg denominator by session
        # progress (floored at 5%) — a raw cumulative/3m-avg ratio at 09:33
        # would reject everything 3 minutes into RTH. Still the spec's
        # "current volume >= 4.0x the 3-month average", measured pro-rata.
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _zi
        _now_et = _dt.now(_zi("America/New_York"))
        _sess_frac = max(0.05, min(1.0, ((_now_et.hour * 60 + _now_et.minute) - 570) / 390.0))
        scored = []
        for row, yfd in zip(pre, prescr):
            sym = str(row["symbol"]).upper()
            fs = yfd.get("float_shares")
            if fs is not None and float(fs) >= C.FLOAT_MAX_SHARES:
                continue  # hard float reject (authoritative recheck after FMP enrich)
            rv = _rvol(row.get("volume"), yfd.get("avg_volume_3m"))
            if rv is not None:
                rv = rv / _sess_frac  # pro-rated intraday RVOL (see above)
            if rv is None:
                logger.info(f"[saro12-screen] {sym}: no 3m avg volume — RVOL unknown, drop (hard screen)")
                continue
            if rv < C.RVOL_MIN:
                continue
            scored.append({"ticker": sym, "price": float(row.get("price") or 0),
                           "today_vol": int(float(row.get("volume") or 0)),
                           "avg_volume_3m": yfd.get("avg_volume_3m"),
                           "rvol": round(rv, 2), "float_shares": fs})
        scored.sort(key=lambda x: x["rvol"], reverse=True)
        capped = scored[:top_n]
        logger.info(f"[saro12-screen] RVOL>= {C.RVOL_MIN}: {len(scored)} pass, "
                    f"enriching top {len(capped)}")

        # capped FMP enrichment + anti-junk + momentum
        out: list[dict] = []
        from app.engines.data_feeds.fmp_feed import fetch_intraday_bars
        from app.engines.scanner.saro12.indicators import bars_to_df, momentum_screen_ok
        for cand in capped:
            sym = cand["ticker"]
            enr = await _fmp_enrich(r, date_key, sym)
            fmp_calls += enr.get("fmp_calls", 0)
            cand["float_shares"] = enr.get("float_shares") or cand.get("float_shares")
            # Only overwrite screener-verified fields when enrichment actually
            # returned data — a failed profile call must not hard-reject a
            # candidate the bulk screener already validated.
            if enr.get("market_cap") is not None:
                cand["market_cap"] = enr.get("market_cap")
            if enr.get("exchange"):
                cand["exchange"] = enr.get("exchange")
            cand["ownership_evidence"] = enr.get("ownership_evidence")
            cand["has_options_chain"] = await _yf_has_options_chain(sym)
            cand["shortable"] = None  # no shortability source on this plan
            if cand["avg_volume_3m"] is None and enr.get("avg_volume_fmp"):
                cand["avg_volume_3m"] = enr["avg_volume_fmp"]
                rv = None
                try:
                    rv = cand["today_vol"] / float(enr["avg_volume_fmp"])
                except Exception:
                    pass
                if rv is not None:
                    cand["rvol"] = round(rv, 2)
            ok, reasons = passes_anti_junk(cand)
            if not ok:
                logger.info(f"[saro12-screen] {sym} anti-junk reject: {reasons}")
                continue
            bars = await fetch_intraday_bars(sym)  # 1 FMP call, TTL-cached
            fmp_calls += 1
            df = bars_to_df(bars)
            mom_ok, mom_detail = momentum_screen_ok(df["close"]) if df is not None \
                else (False, {"note": "no intraday bars"})
            cand["momentum_ok"] = bool(mom_ok)
            cand["momentum_detail"] = mom_detail
            cand["bars"] = bars
            cand["screen_reasons"] = reasons + [
                f"RVOL {cand['rvol']}x >= {C.RVOL_MIN}x (vol {cand['today_vol']:,} / 3m avg)",
                f"momentum RSI>{C.RSI_CROSS_LEVEL:g} cross + MACD hist>0: "
                f"{'PASS' if mom_ok else 'no'} {mom_detail}",
            ]
            out.append(cand)

        # persist the candidate list for the nightly Tradier OI snapshot
        try:
            await _cache_set_json(r, C.CANDIDATES_KEY_FMT.format(date=date_key),
                                  [c["ticker"] for c in out])
        except Exception:
            pass
        logger.info(f"[saro12-screen] done: {len(out)} candidates, "
                    f"~{fmp_calls} FMP calls this run (budget max "
                    f"{1 + top_n * (C.FMP_CALLS_PER_ENRICH + 1)})")
        for c in out:
            c["fmp_calls"] = fmp_calls
        return out
    except Exception as e:
        logger.error(f"[saro12-screen] run_screen failed: {e}")
        return []
    finally:
        if own_r:
            try:
                await r.aclose()
            except Exception:
                pass

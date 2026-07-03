"""Scanner V2 ("Bellwether") historical replay — measured win rate vs V1 actuals.

Replays the V2 pipeline (funnel._coarse -> score_v2 -> decide_fire -> compute_levels)
for each trading day in a window, using ONLY data knowable at the 09:40 ET decision
point wherever the data source allows, then walks 1-minute bars forward to score the
pick with the SAME win/loss/expired semantics as the prod resolver
(app.api.routes.scanner._resolve_email_signal_outcomes).

Pipeline per day D (see docs/v2/scanner-v2-replay.md LIMITATIONS for honesty notes):
  1. Universe: Polygon grouped-daily rows for D (price/vol) + prev trading day
     (prev close/vol), filtered like momentum_scanner._fetch_market_snapshot
     ($1M day dollar-vol floor). NOTE: D's grouped row is the day's FULL session
     (close + full-day volume) -> residual lookahead in the coarse stage only.
  2. funnel._coarse per equity template; provisional score_v2 ranks; top 3 per
     template, capped to the best 12 unique tickers (mirrors shadow.py).
  3. Enrich those <=12 from 1m bars TRUNCATED to bars fully complete by 09:40 ET
     (start <= 09:39): price@0940, gap vs prev close, time-of-day-matched rel-vol
     (cum vol 04:00-09:39 D / same window D-1), premarket $-vol, session VWAP,
     confirmation proxy (above VWAP + last-3 higher highs — same as shadow.py),
     8-K catalyst from an edgar_filings dump (filed_at within 36h of the scan,
     mirroring theta_scanner._get_8k_catalyst).
  4. Full score_v2 + decide_fire(09:40) -> highest-scoring allowed candidate with
     valid compute_levels = the day's pick; none -> NO-TRADE (counted).
  5. Outcome: walk 1m bars from 09:40 on D through the next 5 trading sessions:
     per bar stop-touch checked BEFORE target-touch (resolver semantics); neither
     by 5 sessions -> EXPIRED with pct = session-5 close vs entry; not enough
     future data yet -> OPEN.

Run (ephemeral container, never prod):
  docker run --rm --cpus=2 -v /root/worktrees/v2-redesign/backend:/work -w /work \
    -e PYTHONPATH=/work -e V2_FAST_BACKTEST=0 -e POLYGON_API_KEY=... \
    edge-asset-management-backend \
    python scripts/scanner_v2_replay.py --start 2026-05-19 --end 2026-07-02 --max-days 12
  ... repeat until all days present, then add --summarize (needs --v1-csv dump).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# app imports — all pure-importable (verified in the bare backend image)
from app.engines.scanner.funnel import _coarse
from app.engines.scanner.definitions import equity_templates
from app.engines.scanner.levels import compute_levels
from app.engines.scanner.v2.scoring import score_v2
from app.engines.scanner.v2.gates import decide_fire
from app.engines.options.theta_scanner import (
    _session_vwap, _premarket_dollar_volume, _last3_higher_highs, _CATALYST_WEIGHTS,
)

ET = ZoneInfo("America/New_York")
SCAN_MIN = 9 * 60 + 40          # decision point: 09:40 ET (RTH window of decide_fire)
PM_START_MIN = 4 * 60           # 04:00 ET
POLY = "https://api.polygon.io"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".scanner_v2_replay_cache")
API_KEY = os.environ.get("POLYGON_API_KEY", "")

CSV_FIELDS = [
    "date", "status", "ticker", "template", "score", "entry", "stop", "target", "rr",
    "outcome", "outcome_pct", "outcome_note", "fire_window", "fire_reason", "confirmed",
    "price_940", "vwap_940", "gap_940_pct", "gap_close_pct", "rel_vol_940",
    "rel_vol_close", "premkt_dollar_vol", "session_dollar_vol_940",
    "catalyst_weight", "catalyst_reason", "no_trade_reason",
]


# ── HTTP + caches ────────────────────────────────────────────────────────────

def _get(url: str, params: dict, timeout: float = 25.0):
    """GET with 429/5xx retry. Returns parsed JSON dict or None."""
    for attempt in range(6):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                time.sleep(0.12)  # be polite to the shared key
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None  # 4xx other than 429: no point retrying
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None


def _cache_path(name: str) -> str:
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, name)


def grouped_daily(d: date) -> dict:
    """{ticker: {"c": close, "v": vol}} for date d (trimmed cache). {} = no data."""
    p = _cache_path(f"grouped_{d.isoformat()}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    j = _get(f"{POLY}/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}",
             {"adjusted": "true", "apiKey": API_KEY})
    if not j or j.get("status") not in ("OK", "DELAYED"):
        return {}  # fetch failure: do NOT cache, so a retry can succeed later
    out = {}
    if (j.get("resultsCount") or 0) > 100:
        for row in j.get("results") or []:
            t = row.get("T")
            if t:
                out[t] = {"c": row.get("c"), "v": row.get("v")}
    with open(p, "w") as f:
        json.dump(out, f)
    return out


def bars_1m(ticker: str, d1: date, d2: date) -> list:
    """Unadjusted 1m bars for [d1, d2] (same params as _polygon_1min_bars, plus
    multi-day range + limit for the outcome walk). Cached."""
    p = _cache_path(f"bars_{ticker}_{d1.isoformat()}_{d2.isoformat()}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    j = _get(f"{POLY}/v2/aggs/ticker/{ticker.upper()}/range/1/minute/{d1.isoformat()}/{d2.isoformat()}",
             {"adjusted": "false", "sort": "asc", "limit": 50000, "apiKey": API_KEY})
    if not j or j.get("status") not in ("OK", "DELAYED"):
        return []  # fetch failure: do NOT cache
    bars = j.get("results") or []
    with open(p, "w") as f:
        json.dump(bars, f)
    return bars


# ── ET time helpers ──────────────────────────────────────────────────────────

def bar_et(b: dict) -> tuple:
    """(et_date_iso, minutes_since_midnight_ET) for a 1m bar."""
    dt = datetime.fromtimestamp(int(b["t"]) / 1000.0, tz=timezone.utc).astimezone(ET)
    return dt.date().isoformat(), dt.hour * 60 + dt.minute


def truncate_0940(bars: list, d_iso: str) -> list:
    """Bars on date d fully complete by the 09:40 decision (start <= 09:39 ET)."""
    out = []
    for b in bars:
        di, m = bar_et(b)
        if di == d_iso and m < SCAN_MIN:
            out.append(b)
    return out


def window_vol(bars: list, d_iso: str, lo_min: int = PM_START_MIN, hi_min: int = SCAN_MIN) -> float:
    """Total share volume on date d for bars starting in [lo_min, hi_min)."""
    tot = 0.0
    for b in bars:
        di, m = bar_et(b)
        if di == d_iso and lo_min <= m < hi_min:
            tot += float(b.get("v") or 0)
    return tot


# ── Catalyst replay (edgar_filings dump) ────────────────────────────────────

def load_edgar(path: str) -> dict:
    """'ticker|filed_at|item_codes' dump -> {ticker: [(filed_at_utc, [codes]), ...]}."""
    out: dict = {}
    if not path or not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            parts = line.rstrip("\n").split("|")
            if len(parts) < 3:
                continue
            tk, filed, codes_raw = parts[0].strip(), parts[1].strip(), "|".join(parts[2:])
            try:
                ts = datetime.fromisoformat(filed)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            try:
                codes = json.loads(codes_raw) if codes_raw else []
                if not isinstance(codes, list):
                    codes = []
            except Exception:
                codes = []
            out.setdefault(tk.upper(), []).append((ts.astimezone(timezone.utc), codes))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def catalyst_at(edgar: dict, ticker: str, scan_utc: datetime) -> tuple:
    """Mirror _get_8k_catalyst at a historical instant: most recent filing within
    36h before scan time; weight = max catalyst weight over its item codes."""
    rows = edgar.get(ticker.upper()) or []
    lo = scan_utc - timedelta(hours=36)
    best = None
    for ts, codes in rows:
        if lo < ts <= scan_utc:
            best = (ts, codes)   # rows sorted asc -> ends at most recent
    if not best:
        return 1.0, ""
    max_w, reason = 1.0, ""
    for c in best[1]:
        w = _CATALYST_WEIGHTS.get(c, 1.0)
        if w > max_w:
            max_w, reason = w, f"8-K item {c}"
    return max_w, reason


# ── Outcome walk (resolver semantics on 1m bars) ─────────────────────────────

def score_outcome(ticker: str, d: date, entry: float, stop: float, target: float) -> tuple:
    """(outcome, pct, note): first stop/target touch from 09:40 on D through the
    next 5 trading sessions. Per bar STOP is checked before TARGET — same
    conservative ordering as _resolve_email_signal_outcomes. Neither in 5
    sessions -> expired (pct = session-5 close vs entry); not enough future
    data -> open (pct = last available close vs entry)."""
    bars = bars_1m(ticker, d, d + timedelta(days=10))
    d_iso = d.isoformat()
    sessions: list = []      # ordered ET dates > D seen in the tape
    last_close, last_seen = None, None
    for b in bars:
        di, m = bar_et(b)
        if di == d_iso and m < SCAN_MIN:
            continue                      # pre-decision tape: not walkable
        if di != d_iso:
            if di not in sessions:
                if len(sessions) >= 5:    # past the 5-session expiry horizon
                    break
                sessions.append(di)
        lo, hi, cl = float(b.get("l") or 0), float(b.get("h") or 0), float(b.get("c") or 0)
        if cl > 0:
            last_close, last_seen = cl, f"{di} {m // 60:02d}:{m % 60:02d}"
        if lo > 0 and lo <= stop:
            return "loss", round((stop - entry) / entry * 100.0, 2), f"stop touch {di} {m // 60:02d}:{m % 60:02d} ET"
        if hi >= target:
            return "win", round((target - entry) / entry * 100.0, 2), f"target touch {di} {m // 60:02d}:{m % 60:02d} ET"
    if last_close is None:
        return "open", None, "no post-decision bars yet"
    pct = round((last_close - entry) / entry * 100.0, 2)
    if len(sessions) >= 5:
        return "expired", pct, f"no touch in 5 sessions (close {last_seen} ET)"
    return "open", pct, f"only {len(sessions)} future sessions so far (close {last_seen} ET)"


# ── Per-day replay ───────────────────────────────────────────────────────────

def prev_trading_day(d: date, max_back: int = 7):
    for back in range(1, max_back + 1):
        pd = d - timedelta(days=back)
        if pd.weekday() >= 5:
            continue
        g = grouped_daily(pd)
        if g:
            return pd, g
    return None, None


def replay_day(d: date, edgar: dict, enrich_cap: int = 12, top_per_tpl: int = 3) -> dict:
    d_iso = d.isoformat()
    row = {k: "" for k in CSV_FIELDS}
    row["date"] = d_iso

    g_today = grouped_daily(d)
    if not g_today:
        row["status"] = "NO-DATA"
        row["no_trade_reason"] = "no grouped-daily data (market holiday or feed gap)"
        return row
    pdate, g_prev = prev_trading_day(d)
    if not g_prev:
        row["status"] = "NO-DATA"
        row["no_trade_reason"] = "no prior-day grouped data"
        return row

    # Stage 0: snapshot rows exactly like _fetch_market_snapshot (D full-day rows —
    # the acknowledged coarse-stage lookahead; $1M day $-vol floor mirrored).
    rows = []
    for tk, today in g_today.items():
        prev = g_prev.get(tk)
        if not prev:
            continue
        try:
            price = float(today.get("c") or 0)
            prev_close = float(prev.get("c") or 0)
            day_vol = int(today.get("v") or 0)
            prev_vol = int(prev.get("v") or 0)
            if price <= 0 or prev_close <= 0 or day_vol <= 0:
                continue
            if price * day_vol < 1_000_000:
                continue
            rows.append({"ticker": tk, "day": {"c": price, "v": day_vol},
                         "prevDay": {"c": prev_close, "v": prev_vol},
                         "lastTrade": {"p": price}})
        except Exception:
            continue

    # QQQ context truncated to 09:40 (price@0940 vs prev-day grouped close).
    context: dict = {}
    qqq_prev = float((g_prev.get("QQQ") or {}).get("c") or 0)
    qqq_bars = truncate_0940(bars_1m("QQQ", d, d), d_iso)
    if qqq_prev > 0 and qqq_bars:
        qqq_px = float(qqq_bars[-1].get("c") or 0)
        if qqq_px > 0:
            context["qqq_day_pct"] = round((qqq_px - qqq_prev) / qqq_prev * 100.0, 2)
            context["qqq_above_prev_close"] = qqq_px > qqq_prev

    # Stage 1: coarse per template + provisional V2 rank (mirrors shadow.py).
    per_tpl, need = [], {}
    for tpl in equity_templates():
        cands = [c for c in (_coarse(tpl, r) for r in rows) if c]
        for c in cands:
            c["_v2_prov"] = score_v2(c, context).total
        cands.sort(key=lambda x: x["_v2_prov"], reverse=True)
        top = cands[:top_per_tpl]
        per_tpl.append((tpl, top))
        for c in top:
            need[c["ticker"]] = max(need.get(c["ticker"], 0.0), c["_v2_prov"])
    allowed = {tk for tk, _ in sorted(need.items(), key=lambda kv: kv[1], reverse=True)[:enrich_cap]}

    if not allowed:
        row["status"] = "NO-TRADE"
        row["no_trade_reason"] = "no coarse candidates on any template"
        return row

    # Stage 2: enrich from <=09:39 bars; re-anchor price/gap/rel-vol to scan time.
    scan_utc = datetime(d.year, d.month, d.day, SCAN_MIN // 60, SCAN_MIN % 60, tzinfo=ET
                        ).astimezone(timezone.utc)
    # _coarse UPPERCASES tickers while Polygon grouped keys keep class notation
    # (e.g. "HPEpC") — mirror live's behavior with an upper-keyed lookup. Bars are
    # fetched with the uppercased symbol exactly like the live shadow does (for
    # preferred-class names that fetch finds nothing -> no confirmation -> blocked,
    # which is also live behavior).
    prev_upper = {k.upper(): v for k, v in g_prev.items()}
    enrich: dict = {}
    for tk in sorted(allowed):
        bars_today = truncate_0940(bars_1m(tk, d, d), d_iso)
        bars_prev_all = bars_1m(tk, pdate, pdate)
        prev_close = float(prev_upper[tk]["c"])
        prev_day_vol = float(prev_upper[tk]["v"] or 0)
        e: dict = {"bars": bars_today}
        if bars_today:
            px = float(bars_today[-1].get("c") or 0)
            if px > 0:
                e["price_940"] = px
                e["gap_940"] = round((px - prev_close) / prev_close * 100.0, 2)
            cum = window_vol(bars_today, d_iso)
            prev_win = window_vol(bars_prev_all, pdate.isoformat())
            den = max(prev_win, 0.02 * prev_day_vol, 1.0)  # floor: dead prior premarket
            e["rel_vol_940"] = round(cum / den, 2)
            e["sess_dvol_940"] = sum(float(b.get("c") or 0) * float(b.get("v") or 0)
                                     for b in bars_today)
            try:
                e["pm_dvol"] = _premarket_dollar_volume(bars_today)
            except Exception:
                pass
            try:
                vwap = _session_vwap(bars_today)
                e["vwap"] = vwap
                e["confirmed"] = bool(vwap and e.get("price_940") and
                                      e["price_940"] > float(vwap) and
                                      _last3_higher_highs(bars_today))
            except Exception:
                e["confirmed"] = False
        e["cat_w"], e["cat_reason"] = catalyst_at(edgar, tk, scan_utc)
        enrich[tk] = e

    # Stage 3: full re-score per (template, candidate); best per ticker (shadow's
    # finalists rule) — but on scan-time values, not the grouped close.
    finalists: dict = {}
    for tpl, top in per_tpl:
        for c in top:
            tk = c["ticker"]
            if tk not in allowed:
                continue
            e = enrich[tk]
            cand = dict(c)
            cand["catalyst_weight"] = e["cat_w"]
            cand["catalyst_reason"] = e["cat_reason"] or None
            if e.get("price_940"):
                cand["price"] = e["price_940"]
            if e.get("gap_940") is not None:
                cand["gap_pct"] = e["gap_940"]           # rs_vs_qqq day_pct fallback too
            if e.get("rel_vol_940") is not None:
                cand["rel_vol"] = e["rel_vol_940"]
            if e.get("sess_dvol_940") is not None:
                cand["dollar_vol"] = e["sess_dvol_940"]
            if e.get("pm_dvol") is not None:
                cand["premarket_dollar_vol"] = e["pm_dvol"]
            cand["confirmed"] = bool(e.get("confirmed"))
            bd = score_v2(cand, context)
            prevf = finalists.get(tk)
            if prevf is None or bd.total > prevf[2].total:
                finalists[tk] = (tpl, cand, bd)

    ranked = sorted(finalists.values(), key=lambda f: f[2].total, reverse=True)

    # Stage 4: highest-scoring candidate that fires at 09:40 AND forms valid levels.
    pick = None
    blocked_notes = []
    for tpl, cand, bd in ranked:
        fire = decide_fire(SCAN_MIN, cand)
        if not fire.allowed:
            blocked_notes.append(f"{cand['ticker']} {bd.total:.1f}: {fire.reason}")
            continue
        lv = compute_levels("long", cand["price"], enrich[cand["ticker"]]["bars"],
                            rr=tpl.levels.rr_ratio, atr_stop_mult=tpl.levels.atr_stop_mult)
        if not lv.ok:
            blocked_notes.append(f"{cand['ticker']} {bd.total:.1f}: fires but no valid levels "
                                 f"(rr {lv.rr})")
            continue
        pick = (tpl, cand, bd, fire, lv)
        break

    if pick is None:
        row["status"] = "NO-TRADE"
        row["no_trade_reason"] = " | ".join(blocked_notes[:3]) or "no candidates"
        return row

    tpl, cand, bd, fire, lv = pick
    tk = cand["ticker"]
    e = enrich[tk]
    outcome, pct, note = score_outcome(tk, d, lv.entry, lv.stop, lv.target)
    row.update({
        "status": "PICK", "ticker": tk, "template": tpl.key, "score": bd.total,
        "entry": lv.entry, "stop": lv.stop, "target": lv.target, "rr": lv.rr,
        "outcome": outcome, "outcome_pct": pct if pct is not None else "",
        "outcome_note": note, "fire_window": fire.window, "fire_reason": fire.reason,
        "confirmed": cand.get("confirmed"),
        "price_940": e.get("price_940", ""), "vwap_940": round(e["vwap"], 4) if e.get("vwap") else "",
        "gap_940_pct": e.get("gap_940", ""),
        "gap_close_pct": "",   # filled from the coarse dict below
        "rel_vol_940": e.get("rel_vol_940", ""),
        "rel_vol_close": "", "premkt_dollar_vol": round(e.get("pm_dvol") or 0),
        "session_dollar_vol_940": round(e.get("sess_dvol_940") or 0),
        "catalyst_weight": e.get("cat_w", ""), "catalyst_reason": e.get("cat_reason", ""),
    })
    # original grouped-close values for reference
    for _tpl2, top in per_tpl:
        for c0 in top:
            if c0["ticker"] == tk:
                row["gap_close_pct"] = c0["gap_pct"]
                row["rel_vol_close"] = c0["rel_vol"]
                break
    return row


# ── Summary / report ─────────────────────────────────────────────────────────

def _stats(picks: list) -> dict:
    """picks: [(outcome, pct)] -> WR/expectancy stats. WR = wins/(wins+losses)."""
    wins = [p for o, p in picks if o == "win"]
    losses = [p for o, p in picks if o == "loss"]
    expired = [p for o, p in picks if o == "expired"]
    open_ = [p for o, p in picks if o == "open"]
    resolved = [p for o, p in picks if o in ("win", "loss", "expired") and p is not None]
    decided = len(wins) + len(losses)
    return {
        "picks": len(picks), "wins": len(wins), "losses": len(losses),
        "expired": len(expired), "open": len(open_),
        "wr": (100.0 * len(wins) / decided) if decided else None,
        "expectancy": (sum(resolved) / len(resolved)) if resolved else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
    }


def _fmt(x, suf="%"):
    return "n/a" if x is None else f"{x:+.2f}{suf}" if suf else f"{x:.1f}"


def summarize(csv_path: str, v1_csv: str, md_path: str, start: str, end: str) -> str:
    with open(csv_path) as f:
        rows = sorted(csv.DictReader(f), key=lambda r: r["date"])
    v1 = {}
    if v1_csv and os.path.exists(v1_csv):
        with open(v1_csv) as f:
            for line in f:
                parts = line.rstrip("\n").split("|")
                if len(parts) >= 8 and parts[0].strip():
                    dt, tk, sc, en, st, tg, oc, pct = [p.strip() for p in parts[:8]]
                    v1.setdefault(dt, []).append(
                        {"ticker": tk, "score": sc, "outcome": oc,
                         "pct": float(pct) if pct else None})

    v2_picks = [(r["outcome"], float(r["outcome_pct"]) if r["outcome_pct"] else None)
                for r in rows if r["status"] == "PICK"]
    v1_picks = [(p["outcome"], p["pct"]) for ps in v1.values() for p in ps]
    s2, s1 = _stats(v2_picks), _stats(v1_picks)
    no_trade = sum(1 for r in rows if r["status"] == "NO-TRADE")
    no_data = sum(1 for r in rows if r["status"] == "NO-DATA")
    scan_days = sum(1 for r in rows if r["status"] in ("PICK", "NO-TRADE"))

    wr2 = "n/a" if s2["wr"] is None else f"{s2['wr']:.1f}%"
    wr1 = "n/a" if s1["wr"] is None else f"{s1['wr']:.1f}%"
    summary_lines = [
        f"== SUMMARY: V2 replay vs V1 actual ({start} .. {end}) ==",
        (f"V2 replay : trading days={scan_days}  picks={s2['picks']}  "
         f"NO-TRADE days={no_trade}  no-data days={no_data}"),
        (f"            W-L-E-O={s2['wins']}-{s2['losses']}-{s2['expired']}-{s2['open']}  "
         f"WR={wr2}  expectancy/pick={_fmt(s2['expectancy'])}  "
         f"avg win={_fmt(s2['avg_win'])}  avg loss={_fmt(s2['avg_loss'])}"),
        (f"V1 actual : picks={s1['picks']} (email_signals_history, shadow=false, deduped)"),
        (f"            W-L-E-O={s1['wins']}-{s1['losses']}-{s1['expired']}-{s1['open']}  "
         f"WR={wr1}  expectancy/pick={_fmt(s1['expectancy'])}  "
         f"avg win={_fmt(s1['avg_win'])}  avg loss={_fmt(s1['avg_loss'])}"),
        ("WR = wins/(wins+losses); expectancy = mean outcome_pct over resolved "
         "(win+loss+expired); open picks excluded from both."),
    ]
    summary = "\n".join(summary_lines)

    md = ["# Scanner V2 (\"Bellwether\") historical replay — " + f"{start} .. {end}", ""]
    md += ["Replay of the V2 pipeline (funnel coarse -> score_v2 -> 09:40 ET fire gates ->",
           "structure levels) on historical Polygon data, scored with the prod resolver's",
           "win/loss/expired rules, side-by-side with V1's actual emailed picks.", ""]
    md += ["```", summary, "```", "", "## Per-day results", ""]
    md += ["| date | V2 pick | score | entry / stop / target | V2 outcome | V2 pct | V1 pick | V1 outcome | V1 pct |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        d = r["date"]
        v1d = v1.get(d, [])
        v1_cell = ", ".join(p["ticker"] for p in v1d) or "—"
        v1_oc = ", ".join(p["outcome"] or "?" for p in v1d) or "—"
        v1_pct = ", ".join("" if p["pct"] is None else f"{p['pct']:+.2f}%" for p in v1d) or "—"
        if r["status"] == "PICK":
            lv = f"{r['entry']} / {r['stop']} / {r['target']}"
            pct = f"{float(r['outcome_pct']):+.2f}%" if r["outcome_pct"] else "—"
            md.append(f"| {d} | **{r['ticker']}** ({r['template']}) | {r['score']} | {lv} "
                      f"| {r['outcome']} | {pct} | {v1_cell} | {v1_oc} | {v1_pct} |")
        else:
            why = (r["no_trade_reason"] or "")[:120].replace("|", "/")
            md.append(f"| {d} | *{r['status']}* — {why} | — | — | — | — "
                      f"| {v1_cell} | {v1_oc} | {v1_pct} |")
    md += ["", "## Methodology", "",
           "- Universe/coarse: Polygon grouped-daily D vs prior trading day, filtered like",
           "  `_fetch_market_snapshot` ($1M day $-vol floor) then `funnel._coarse` per equity",
           "  template; provisional `score_v2` ranks; top 3/template, best 12 unique enriched.",
           "- Enrichment (scan-time honest): 1m bars complete by 09:40 ET only -> price@09:40,",
           "  gap vs prior close, time-of-day-matched rel-vol (cum vol 04:00-09:39 D / same",
           "  window D-1, denominator floored at 2% of D-1 full-day volume), premarket $-vol,",
           "  session VWAP, confirmation proxy = above VWAP + last-3 higher highs (shadow.py's",
           "  proxy), 8-K catalyst from edgar_filings history (36h lookback at 09:40, mirroring",
           "  `_get_8k_catalyst`). QQQ context likewise truncated at 09:40.",
           "- Pick: highest score_v2 with `decide_fire(09:40)` allowed AND valid",
           "  `compute_levels` (template rr/atr params); else NO-TRADE (counted).",
           "- Outcome: 1m bars from 09:40 D through +5 trading sessions; per bar STOP checked",
           "  before TARGET (prod resolver ordering); neither -> EXPIRED at session-5 close;",
           "  insufficient future data -> OPEN (excluded from WR/expectancy).",
           "- V1 comparison: actual emailed picks (email_signals_history, shadow=false),",
           "  deduped per (day, ticker) — duplicate re-sends of the same pick collapse to one",
           "  — with their prod-resolver outcomes.", "",
           "## Limitations (read before quoting the numbers)", "",
           "1. **Coarse-stage lookahead**: the stage-1 universe uses day D's FULL grouped row",
           "   (close + full-day volume). A name that only qualified via afternoon action can",
           "   enter the candidate set. All scoring/confirmation/levels use <=09:39 data, but",
           "   the candidate *set* has residual lookahead.",
           "2. **Live V2 sees different data**: prod's Polygon key is delayed-tier; the live",
           "   scan's grouped 'day' is actually D-1 (prev = D-2). This replay assumes same-day",
           "   pre-open universe knowledge — i.e. it measures 'V2 with a real-time snapshot",
           "   feed', not V2 exactly as deployed on the delayed key.",
           "3. **Rel-vol scale**: score_v2 was calibrated on full-day grouped rel-vol; the",
           "   replay feeds a 04:00-09:39 time-matched ratio (scan-time knowable). Log scaling",
           "   absorbs most of the difference but values can saturate when the prior-day",
           "   premarket was dead (denominator floor).",
           "4. **Session $-vol under-scaled**: liquidity_quality's session component sees the",
           "   09:40 cumulative, not the full-day $-vol it was scaled for (premkt $-vol, 60%",
           "   of the blend, is unaffected).",
           "5. **Single decision point**: one pick/day at 09:40 (RTH window). Premarket-window",
           "   fires (06:00-09:29) are NOT replayed, though live V2 could fire there.",
           "6. **Outcome basis**: 1m UNADJUSTED bars (matches unadjusted entries) vs the",
           "   resolver's adjusted daily bars; same-bar stop/target ambiguity resolved",
           "   conservatively (stop first). Corporate actions inside the 5-day window would",
           "   misprice (none observed in this window).",
           "7. **Sample size**: ~1 month of trading days; wide confidence intervals — a",
           "   handful of outcomes moves WR by several points.",
           "8. **V1 rows scored by prod**: V1 stats inherit whatever biases the prod resolver",
           "   has (e.g. daily-bar stop-first ordering). 2026-06-19 was a market holiday with",
           "   no grouped data, yet V1 recorded a (delayed-key artifact) pick — kept in V1",
           "   stats, shown as NO-DATA for V2.",
           "9. **Catalyst feature is dead in prod**: every row of `edgar_filings` (8,352 rows",
           "   since 2026-05-12) has an EMPTY ticker column, so `_get_8k_catalyst` can never",
           "   match — catalyst_weight was 1.0 ('no catalyst') for every V1 pick AND every",
           "   candidate in this replay. Faithful to live, but it means (a) the V2 catalyst",
           "   component contributed a flat 2.8/100 to everyone, and (b) V2's premarket fire",
           "   window (requires liquidity AND catalyst) can never open until this is fixed —",
           "   flagged separately for repair.", ""]
    with open(md_path, "w") as f:
        f.write("\n".join(md))
    return summary


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-05-19")
    ap.add_argument("--end", default="2026-07-02")
    ap.add_argument("--csv", default="docs/v2/scanner-v2-replay.csv")
    ap.add_argument("--md", default="docs/v2/scanner-v2-replay.md")
    ap.add_argument("--v1-csv", default=os.path.join(CACHE, "v1_picks.psv"))
    ap.add_argument("--edgar-csv", default=os.path.join(CACHE, "edgar_filings.psv"))
    ap.add_argument("--max-days", type=int, default=100, help="max NEW days this invocation")
    ap.add_argument("--summarize", action="store_true", help="write the .md report")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    done = set()
    if os.path.exists(args.csv):
        with open(args.csv) as f:
            done = {r["date"] for r in csv.DictReader(f)}
    new_file = not done and not os.path.exists(args.csv)

    edgar = load_edgar(args.edgar_csv)
    print(f"[replay] window {start}..{end}  already-done={len(done)}  "
          f"edgar tickers={len(edgar)}  key={'set' if API_KEY else 'MISSING'}", flush=True)

    os.makedirs(os.path.dirname(args.csv) or ".", exist_ok=True)
    f = open(args.csv, "a", newline="")
    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
    if new_file:
        w.writeheader()
        f.flush()

    processed = 0
    d = start
    while d <= end and processed < args.max_days:
        if d.weekday() >= 5 or d.isoformat() in done:
            d += timedelta(days=1)
            continue
        t0 = time.time()
        try:
            row = replay_day(d, edgar)
        except Exception as ex:
            row = {k: "" for k in CSV_FIELDS}
            row.update({"date": d.isoformat(), "status": "ERROR",
                        "no_trade_reason": f"{type(ex).__name__}: {ex}"})
        w.writerow(row)
        f.flush()
        processed += 1
        print(f"[replay] {d} {row['status']:8s} {row.get('ticker', ''):6s} "
              f"score={row.get('score', '')} outcome={row.get('outcome', '')} "
              f"pct={row.get('outcome_pct', '')} ({time.time() - t0:.1f}s)", flush=True)
        d += timedelta(days=1)
    f.close()
    print(f"[replay] invocation done: {processed} new day(s)", flush=True)

    if args.summarize:
        print(summarize(args.csv, args.v1_csv, args.md, args.start, args.end), flush=True)


if __name__ == "__main__":
    main()

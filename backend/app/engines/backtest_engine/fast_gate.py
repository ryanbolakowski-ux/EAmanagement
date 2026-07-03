"""FAST-BT-V1: exact-parity fast twin of the futures Market Activity Gate
entry point, for backtests only.

The canonical gate (app/engines/strategy_engine/market_activity_gate.py) is
shared with live/paper trading and its gate logic is explicitly marked "do not
change". In a two-week backtest profile it cost 7.8s of 26s: ~4.2s converting
the same DataFrame tail to candle dicts bar after bar (per-element Timestamp
boxing + .to_pydatetime()), and ~2.5s inside f_expansion, which re-walks a
14-bar ATR window for each of 50 median samples through dict lookups.

This module re-implements ONLY those two pieces:

  fast_candles_from_df       one vectorized tz-convert + to_pydatetime() call
                             instead of per-row boxing; identical dict output.
  _fast_f_expansion          precomputes the true-range list once, then takes
                             the same statistics.fmean over the same windows —
                             float-identical samples, median, ratio, score.
  fast_market_gate           verbatim copy of market_gate wired to the fast
                             expansion factor; every other factor fn is the
                             ORIGINAL imported one.
  fast_evaluate_activity_gate verbatim copy of evaluate_activity_gate wired to
                             the fast conversion + gate.

Used only when ICTStrategy._fast_backtest is set (BacktestRunner under
V2_FAST_BACKTEST != "0"). Live/paper paths keep importing the original module.
Parity is guarded by tests/test_fast_backtest_parity.py — do not let this file
drift from the canonical logic.
"""
from __future__ import annotations
import os as _os
import statistics
import pandas as _pd

from app.engines.strategy_engine.market_activity_gate import (
    GateConfig, GateResult, Factor, _gate_log,
    _atr, _clamp01, _ramp, _in_window, _session_vwap, _rsi,
    f_efficiency, f_rel_volume, f_speed, f_liquidity,
    is_futures_symbol, _INSUFFICIENT_DATA_REASONS,
)


def fast_candles_from_df(df, tail: int = 200, memo: dict | None = None) -> list[dict]:
    """Exact-parity twin of market_activity_gate.candles_from_df.

    Same candle dicts (tz-aware ET datetime + float OHLCV); the timestamp
    boxing is done once for the whole tail via DatetimeIndex.to_pydatetime()
    instead of one Timestamp.__getitem__ + .to_pydatetime() per row.

    `memo` (optional): per-run {int64 ns timestamp -> candle dict} cache the
    caller owns. Backtest gate windows overlap ~80% bar-to-bar over the SAME
    immutable resampled frame, so already-converted candles are reused. The
    dicts are never mutated downstream (the gate reads; the volume-strip path
    in fast_evaluate_activity_gate builds NEW dicts), so sharing is safe.
    Callers must not share one memo across different frames/instruments."""
    if df is None or len(df) == 0:
        return []
    idx = df.index
    if not isinstance(idx, _pd.DatetimeIndex):
        try:
            idx = _pd.to_datetime(idx, utc=True)
        except Exception:
            return []
    if idx.tz is None:
        idx = idx.tz_localize("UTC")     # naive assumed UTC (matches is_in_session)
    t = min(tail, len(df))
    idx_t = idx[-t:]

    def _build(ts_py, o, h, lo, c, v):
        out = []
        if v is not None:
            for i in range(len(ts_py)):
                out.append({"ts": ts_py[i], "open": float(o[i]), "high": float(h[i]),
                            "low": float(lo[i]), "close": float(c[i]), "volume": float(v[i])})
        else:
            for i in range(len(ts_py)):
                out.append({"ts": ts_py[i], "open": float(o[i]), "high": float(h[i]),
                            "low": float(lo[i]), "close": float(c[i])})
        return out

    cols = list(df.columns)
    has_vol = "volume" in cols

    def _col_arrays(from_pos):
        # One block pull instead of five column getitems — same float64
        # values, just fetched via a single .to_numpy() on the frame.
        vals = df.to_numpy()[-t:][from_pos:]
        o = vals[:, cols.index("open")]; h = vals[:, cols.index("high")]
        lo = vals[:, cols.index("low")]; c = vals[:, cols.index("close")]
        v = vals[:, cols.index("volume")] if has_vol else None
        return o, h, lo, c, v

    if memo is None:
        ts_py = idx_t.tz_convert("US/Eastern").to_pydatetime()
        return _build(ts_py, *_col_arrays(0))

    keys = idx_t.asi8.tolist()           # python ints -> cheap dict hashing
    first_missing = None
    for i, k in enumerate(keys):
        if k not in memo:
            first_missing = i
            break
    if first_missing is not None:
        # Windows advance monotonically, so misses are a contiguous suffix in
        # practice — convert [first_missing:] with plain (view) slices. Any
        # already-memoized key inside that suffix just gets rebuilt to an
        # identical dict, which is harmless.
        ts_py = idx_t[first_missing:].tz_convert("US/Eastern").to_pydatetime()
        recs = _build(ts_py, *_col_arrays(first_missing))
        for j, rec in enumerate(recs):
            memo[keys[first_missing + j]] = rec
    return [memo[k] for k in keys]


def _fast_f_expansion(c, cfg, atr_now):
    """Exact-parity twin of market_activity_gate.f_expansion.

    The original calls _atr(c[:end], p) for each end, re-reading the same
    true ranges from dicts ~p times each. Here the TR list is computed once;
    each sample is statistics.fmean over the SAME floats in the SAME order,
    so every sample / the median / the ratio is float-identical."""
    n = len(c)
    samples, start = [], max(cfg.atr_period + 1, n - cfg.median_lookback)
    if start <= n:
        p = cfg.atr_period
        first_i = start - p              # >= 1 because start >= p + 1
        tr = [0.0] * n
        prev_close = c[first_i - 1]["close"]
        for i in range(first_i, n):
            ci = c[i]
            h = ci["high"]; l = ci["low"]
            tr[i] = max(h - l, abs(h - prev_close), abs(l - prev_close))
            prev_close = ci["close"]
        fmean = statistics.fmean
        for end in range(start, n + 1):
            samples.append(fmean(tr[end - p:end]))
    if atr_now is None or len(samples) < 5:
        return None
    med = statistics.median(samples)
    ratio = (atr_now / med) if med > 0 else 0.0
    return Factor("expansion", _ramp(ratio, cfg.expansion_lo, cfg.expansion_hi),
                  ratio, f"ATR {ratio:.2f}x median")


def _fast_f_vwap(c, cfg, atr_now, vwap):
    """f_vwap with _session_vwap precomputed by the caller (it is also needed
    by the bias hint — the canonical gate walks the session twice per call)."""
    if vwap is None or atr_now in (None, 0) or vwap[-1] is None:
        return None
    price = c[-1]["close"]
    ext = abs(price - vwap[-1]) / atr_now             # extension in ATR units
    k = cfg.vwap_slope_lookback
    prior = vwap[-k] if len(vwap) > k and vwap[-k] is not None else vwap[-1]
    slope = (vwap[-1] - prior)
    score = _ramp(ext, cfg.vwap_ext_lo, cfg.vwap_ext_hi)
    return Factor("vwap", score, ext,
                  f"{ext:.2f} ATR from VWAP, slope {slope:+.2f}")


def _fast_f_rsi(cfg, r):
    """f_rsi with _rsi precomputed by the caller (shared with the bias hint)."""
    if r is None:
        return None
    push = abs(r - 50)
    return Factor("rsi", _ramp(push, cfg.rsi_push_lo, cfg.rsi_push_hi),
                  r, f"RSI {r:.0f}")


def _fast_bias_hint(c, cfg, vwap, r):
    """_bias_hint with _rsi/_session_vwap precomputed (identical values)."""
    score = 0
    if len(c) > cfg.speed_lookback:
        score += 1 if c[-1]["close"] > c[-(cfg.speed_lookback + 1)]["close"] else -1
    if r is not None:
        score += 1 if r > 50 else -1
    if vwap and vwap[-1] is not None:
        score += 1 if c[-1]["close"] > vwap[-1] else -1
    return "long" if score >= 2 else "short" if score <= -2 else "neutral"


def fast_market_gate(candles, cfg: GateConfig = GateConfig()) -> GateResult:
    """Verbatim copy of market_activity_gate.market_gate with f_expansion
    swapped for the float-identical _fast_f_expansion, and the session VWAP /
    RSI computed once instead of twice (f_vwap+f_rsi and again in _bias_hint)."""
    if not candles:
        return GateResult(False, 0.0, "no data")

    in_window = _in_window(candles[-1]["ts"], cfg.windows)

    atr_now = _atr(candles, cfg.atr_period)
    if atr_now is None:
        return GateResult(False, 0.0, "not enough history for ATR", in_window=in_window)

    _vwap = _session_vwap(candles)
    _r = _rsi(candles, cfg.rsi_period)
    candidates = [
        _fast_f_expansion(candles, cfg, atr_now),
        f_efficiency(candles, cfg),
        f_rel_volume(candles, cfg),
        f_speed(candles, cfg, atr_now),
        _fast_f_vwap(candles, cfg, atr_now, _vwap),
        _fast_f_rsi(cfg, _r),
        f_liquidity(candles, cfg),
    ]
    factors = [f for f in candidates if f is not None]
    if len(factors) < 3:
        return GateResult(False, 0.0, "not enough factors available yet",
                          in_window=in_window, factors=factors)

    # HARD cutoff: volatility spike (uncontrolled)
    exp = next((f for f in factors if f.name == "expansion"), None)
    if exp and cfg.expansion_ceiling and exp.value > cfg.expansion_ceiling:
        return GateResult(False, 0.0, f"too violent — ATR {exp.value:.2f}x median",
                          in_window=in_window, factors=factors)

    # HARD cutoff: dead volume
    rv = next((f for f in factors if f.name == "rel_volume"), None)
    if rv and rv.value < cfg.volume_floor:
        return GateResult(False, 0.0, f"dead — volume {rv.value:.2f}x avg",
                          in_window=in_window, factors=factors)

    # blended activity score (+ optional prime-window bonus)
    num = den = 0.0
    for f in factors:
        w = cfg.weights.get(f.name, 0.0)
        num += w * f.score
        den += w
    base = (num / den) if den > 0 else 0.0
    score = _clamp01(base + (cfg.window_bonus if in_window else 0.0))
    bias = _fast_bias_hint(candles, cfg, _vwap, _r)
    where = "prime window" if in_window else "off-hours"

    if score >= cfg.go_threshold:
        return GateResult(True, score, f"GO ({where}) — score {score:.2f}",
                          bias=bias, in_window=in_window, factors=factors)
    return GateResult(False, score,
                      f"stand down ({where}) — score {score:.2f} (< {cfg.go_threshold})",
                      bias=bias, in_window=in_window, factors=factors)


def fast_evaluate_activity_gate(instrument: str, df, cfg: "GateConfig | None" = None,
                                candle_memo: dict | None = None):
    """Verbatim copy of market_activity_gate.evaluate_activity_gate wired to
    fast_candles_from_df + fast_market_gate. Same abstain/verdict contract.
    `candle_memo`: see fast_candles_from_df — must be private to one frame."""
    if not is_futures_symbol(instrument):
        return None
    if cfg is None:
        cfg = GateConfig()
        cfg.go_threshold = 0.40
        _thr = _os.environ.get("FUTURES_GATE_GO_THRESHOLD")
        if _thr:
            try:
                _v = float(_thr)
                if 0.0 < _v < 1.0:
                    cfg.go_threshold = _v
                else:
                    _gate_log.warning(
                        "FUTURES_GATE_GO_THRESHOLD=%r out of (0,1) range; "
                        "ignoring, using %.2f", _thr, cfg.go_threshold)
            except (TypeError, ValueError):
                _gate_log.warning(
                    "FUTURES_GATE_GO_THRESHOLD=%r not parseable; using %.2f",
                    _thr, cfg.go_threshold)
    # Need enough bars for the multi-factor read (ATR + the longest lookback).
    need = cfg.atr_period + cfg.median_lookback + cfg.hunt_recent_bars + 2
    candles = fast_candles_from_df(df, tail=max(need, 80), memo=candle_memo)
    # Drop trailing FORMING bar(s) — same rule as the canonical entry point.
    while (len(candles) > 1
           and not candles[-1].get("volume")
           and any(c.get("volume") for c in candles[:-1])):
        candles = candles[:-1]
    if len(candles) < cfg.atr_period + 5:
        return None
    # Unreliable volume -> strip it so the gate judges on price action.
    _recent = candles[-min(len(candles), cfg.volume_lookback):]
    _nz = sum(1 for c in _recent if c.get("volume"))
    if (not candles[-1].get("volume")) or _nz < max(5, len(_recent) // 2):
        candles = [{k: v for k, v in c.items() if k != "volume"} for c in candles]
    res = fast_market_gate(candles, cfg)
    if (not res.go) and any(res.reason.startswith(p) for p in _INSUFFICIENT_DATA_REASONS):
        return None  # data availability, not a movement verdict -> abstain
    return res

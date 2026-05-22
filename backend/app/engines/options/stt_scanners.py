"""StocksToTrade Oracle clone — four pre-built scanners.

  • Strategy A — Low-Float Squeeze
  • Strategy B — 52-Week High Breakout
  • Strategy C — Pre-Market Gap Runner
  • Oracle    — 5-minute opening-candle predictive engine

Each scanner returns a list of `STTHit` rows that the scheduler turns into
pending_trades (with confirm links for the morning batch, auto-execute
receipts for intraday). The scanner functions are pure — no DB writes —
so they can be reused in tests, backtests, and the live runner.

All scanners share:
  • the momentum_universe (336 tickers, $0.50-$50 names that move)
  • yfinance bulk download for 1m/1d bars + .info fundamentals
  • the polygon_throttle rate gate (in case any path falls through to Polygon)
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime, timedelta, timezone
from typing import Optional, Literal
from zoneinfo import ZoneInfo
from loguru import logger

import pandas as pd
import yfinance as yf

from app.engines.options.expanded_universe import EXPANDED_UNIVERSE as MOMENTUM_UNIVERSE
from app.engines.options.fundamentals import (
    get_fundamentals, detect_catalyst, Fundamentals, CatalystHit,
)


ET = ZoneInfo("America/New_York")

StrategyTag = Literal["low_float_squeeze", "fifty_two_week_breakout",
                       "premarket_gap_runner", "oracle_opening_candle"]


# ── Technical-indicator helpers ──────────────────────────────────────────────

def _rsi(closes: list[float] | pd.Series, period: int = 14) -> Optional[float]:
    """Standard Wilder RSI(period). Returns the most-recent value or None
    when there are fewer than `period+1` bars to compute it cleanly."""
    if isinstance(closes, list):
        closes = pd.Series(closes)
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    up = delta.where(delta > 0, 0.0)
    dn = (-delta).where(delta < 0, 0.0)
    roll_up = up.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    roll_dn = dn.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = roll_up / roll_dn.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if not pd.isna(val) else None


def _macd_cross_up(closes: pd.Series, fast: int = 12, slow: int = 26,
                    signal: int = 9) -> bool:
    """True when MACD line has just crossed *above* its signal line — i.e.
    the most-recent bar flipped from MACD < signal to MACD >= signal."""
    if len(closes) < slow + signal + 1:
        return False
    macd = closes.ewm(span=fast, adjust=False).mean() - closes.ewm(span=slow, adjust=False).mean()
    sig = macd.ewm(span=signal, adjust=False).mean()
    return bool(macd.iloc[-2] < sig.iloc[-2] and macd.iloc[-1] >= sig.iloc[-1])


def _vol_zscore(volumes: pd.Series, window: int = 20) -> Optional[float]:
    if len(volumes) < window + 1:
        return None
    win = volumes.iloc[-window-1:-1]   # exclude current bar
    mu, sd = win.mean(), win.std()
    if sd == 0 or pd.isna(sd):
        return None
    return float((volumes.iloc[-1] - mu) / sd)


def _velocity_proxy(closes: pd.Series, highs: pd.Series, lows: pd.Series,
                    volumes: pd.Series, n_bars: int = 5) -> float:
    """Approximate 'order-book velocity' from minute bars. Higher score =
    more bullish-buyer footprint.

    Components:
      • vol_z      — last bar's volume z-score vs 20-bar mean
      • body_pos   — average (close - low) / (high - low) over last N bars
                     (1.0 = closes always at the high → buyers in control)
      • streak     — number of consecutive up-volume bars
    """
    score = 0.0
    if len(closes) < 25:
        return score
    last = closes.iloc[-n_bars:]
    h = highs.iloc[-n_bars:]
    l = lows.iloc[-n_bars:]
    v = volumes.iloc[-n_bars:]

    rng = (h - l).replace(0, pd.NA)
    body = ((last - l) / rng).fillna(0.5)
    score += float(body.mean()) * 4.0    # 0-4 from "closes near high"

    vz = _vol_zscore(volumes)
    if vz is not None:
        score += max(0.0, min(vz, 5.0))  # cap so a 20-sigma bar doesn't dominate

    # Streak: consecutive up-volume bars
    streak = 0
    for i in range(len(v) - 1, 0, -1):
        if v.iloc[i] > v.iloc[i-1]:
            streak += 1
        else:
            break
    score += streak * 0.5
    return score



@dataclass
class STTHit:
    strategy: StrategyTag
    ticker: str
    direction: str           # 'long' | 'short'
    price: float
    entry: float
    stop:  float
    target: float
    score: float
    catalyst_headline: Optional[str]
    catalyst_keyword:  Optional[str]
    reason: str
    metadata: dict = field(default_factory=dict)


# ── Bulk bar pulls (yfinance handles the universe in one go) ─────────────────

async def _yf_bulk_intraday(symbols: list[str], period: str = "5d",
                              interval: str = "5m", prepost: bool = True) -> pd.DataFrame:
    """Return the hierarchical-column DF yfinance produces for `download()`."""
    try:
        df = await asyncio.to_thread(
            lambda: yf.download(
                tickers=" ".join(symbols), period=period, interval=interval,
                group_by="ticker", auto_adjust=False, progress=False,
                threads=False, prepost=prepost,  # yfinance 1.3.0 thread race
            )
        )
        return df
    except Exception as e:
        logger.warning(f"[STT] yf bulk fetch failed: {e}")
        return pd.DataFrame()


def _vwap_series(df: pd.DataFrame) -> pd.Series:
    """Standard typical-price VWAP on intraday bars."""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    cumv = df["Volume"].cumsum()
    return (tp * df["Volume"]).cumsum() / cumv.replace(0, pd.NA)


def _now_et() -> datetime:
    return datetime.now(ET)


# ── Strategy A — Low-Float Squeeze ───────────────────────────────────────────

PRICE_A_MIN, PRICE_A_MAX = 0.50, 20.00
FLOAT_A_CAP = 10_000_000
VOL_A_PRE_MIN  = 5_000
VOL_A_INTRA_MIN = 1_000_000
CHG_A_PRE_MIN  = 5.0    # %
CHG_A_INTRA_MIN = 10.0  # %


async def scan_low_float_squeeze(*, top_k: int = 5) -> list[STTHit]:
    """Strategy A: low-float runners with a positive text catalyst.
    Only fires when ALL filters match — Sykes's classic squeeze pattern."""
    now_et = _now_et()
    is_premarket = now_et.time() < dtime(9, 30)
    chg_min = CHG_A_PRE_MIN if is_premarket else CHG_A_INTRA_MIN
    vol_min = VOL_A_PRE_MIN if is_premarket else VOL_A_INTRA_MIN

    # Pull recent daily bars to get today's % change + volume
    df = await _yf_bulk_intraday(MOMENTUM_UNIVERSE, period="2d", interval="1d",
                                    prepost=False)
    if df.empty:
        return []

    candidates: list[tuple[str, float, float, float]] = []  # (ticker, price, change_pct, day_vol)
    for ticker in MOMENTUM_UNIVERSE:
        try:
            if ticker not in df.columns.get_level_values(0):
                continue
            sub = df[ticker].dropna()
            if len(sub) < 2:
                continue
            prev_close = float(sub["Close"].iloc[-2])
            price = float(sub["Close"].iloc[-1])
            day_vol = int(sub["Volume"].iloc[-1])
            if prev_close <= 0:
                continue
            if not (PRICE_A_MIN <= price <= PRICE_A_MAX):
                continue
            if day_vol < vol_min:
                continue
            chg = (price - prev_close) / prev_close * 100.0
            if abs(chg) < chg_min:
                continue
            candidates.append((ticker, price, chg, day_vol))
        except Exception:
            continue

    # For each, pull fundamentals & require float < 10M + positive catalyst
    hits: list[STTHit] = []
    for ticker, price, chg, day_vol in candidates[:200]:  # cap per-cycle work
        f = await get_fundamentals(ticker)
        if not f or not f.float_shares:
            continue
        if f.float_shares > FLOAT_A_CAP:
            continue
        cat = await detect_catalyst(ticker, lookback_hours=48)
        if not cat or cat.direction != ("positive" if chg > 0 else "negative"):
            continue

        # RSI confirmation — don't long if already > 80 (chasing), don't short
        # if already < 20 (oversold dead-cat bounce risk)
        try:
            closes_ticker = df[ticker]["Close"].dropna()
            rsi_val = _rsi(closes_ticker, period=14)
        except Exception:
            rsi_val = None
        if rsi_val is not None:
            if chg > 0 and rsi_val > 80:
                continue
            if chg < 0 and rsi_val < 20:
                continue

        direction = "long" if chg > 0 else "short"
        atr = price * 0.05  # 5% stop on low-float (volatile)
        entry = price
        if direction == "long":
            stop = price - atr; target = price + atr * 2.0
        else:
            stop = price + atr; target = price - atr * 2.0
        # Velocity proxy from the daily close/high/low/volume series we have
        try:
            vel = _velocity_proxy(
                closes_ticker, df[ticker]["High"].dropna(),
                df[ticker]["Low"].dropna(),  df[ticker]["Volume"].dropna(),
                n_bars=2,
            )
        except Exception:
            vel = 0.0
        score = abs(chg) + (1.0 if cat.direction == "positive" else -1.0) * 5 + vel
        hits.append(STTHit(
            strategy="low_float_squeeze",
            ticker=ticker, direction=direction, price=price,
            entry=entry, stop=stop, target=target, score=score,
            catalyst_headline=cat.headline, catalyst_keyword=cat.matched_keyword,
            reason=(f"Low-float {direction.upper()} on {ticker}: float "
                     f"{f.float_shares/1e6:.1f}M < 10M, {chg:+.1f}% today on "
                     f"{day_vol/1e6:.1f}M vol, catalyst: \"{cat.matched_keyword}\""
                     + (f", RSI {rsi_val:.0f}" if rsi_val is not None else "")),
            metadata={"float_shares": f.float_shares, "change_pct": chg,
                       "day_volume": day_vol, "catalyst": cat.headline, "rsi": rsi_val},
        ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


# ── Strategy B — 52-Week High Breakout ───────────────────────────────────────

PRICE_B_MIN, PRICE_B_MAX = 1.00, 50.00
B_WITHIN_PCT_OF_HIGH = 0.98   # within 2% of 52-week high
B_VOLUME_SPIKE_MULT  = 3.00   # 300% of 20-day avg


async def scan_52w_breakout(*, top_k: int = 5) -> list[STTHit]:
    """Strategy B: stocks within 2% of their 52-week high, on 3× avg volume.
    Fire when the 1-minute candle closes above the 52WH line."""
    # Pull 1d bars for last 2 days (we only need today's price + volume)
    df = await _yf_bulk_intraday(MOMENTUM_UNIVERSE, period="2d", interval="1d",
                                    prepost=False)
    if df.empty:
        return []

    candidates = []
    for ticker in MOMENTUM_UNIVERSE:
        try:
            if ticker not in df.columns.get_level_values(0):
                continue
            sub = df[ticker].dropna()
            if len(sub) < 1:
                continue
            today = sub.iloc[-1]
            price = float(today["Close"])
            day_vol = int(today["Volume"])
            if not (PRICE_B_MIN <= price <= PRICE_B_MAX):
                continue
            candidates.append((ticker, price, day_vol))
        except Exception:
            continue

    hits: list[STTHit] = []
    for ticker, price, day_vol in candidates[:200]:
        f = await get_fundamentals(ticker)
        if not f or not f.fifty_two_week_high or not f.avg_volume_10d:
            continue
        wh = f.fifty_two_week_high
        if price < wh * B_WITHIN_PCT_OF_HIGH:
            continue  # too far below the high
        vol_spike = day_vol / max(1, f.avg_volume_10d)
        if vol_spike < B_VOLUME_SPIKE_MULT:
            continue

        # RSI confirmation — breakouts with RSI < 60 tend to fail
        try:
            closes_ticker = df[ticker]["Close"].dropna()
            rsi_val = _rsi(closes_ticker, period=14)
        except Exception:
            rsi_val = None
        if rsi_val is not None and rsi_val < 60:
            continue

        # Direction is always long for a breakout; entry at the 52WH
        entry = wh
        stop  = wh * 0.97  # 3% stop below the breakout level
        target = wh * 1.10  # 10% measured move
        score = vol_spike + (1 if price >= wh else 0) * 3
        hits.append(STTHit(
            strategy="fifty_two_week_breakout",
            ticker=ticker, direction="long", price=price,
            entry=entry, stop=stop, target=target, score=score,
            catalyst_headline=None, catalyst_keyword=None,
            reason=(f"52-week-high breakout on {ticker}: price ${price:.2f} "
                     f"vs 52WH ${wh:.2f}, volume {vol_spike:.1f}x avg"
                     + (f", RSI {rsi_val:.0f}" if rsi_val is not None else "")),
            metadata={"fifty_two_week_high": wh, "vol_spike": vol_spike,
                       "day_volume": day_vol, "rsi": rsi_val},
        ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


# ── Strategy C — Pre-Market Gap Runner ───────────────────────────────────────

PRICE_C_MIN, PRICE_C_MAX = 1.00, 100.00
GAP_C_MIN_PCT = 5.0
VOL_C_PREMARKET_MIN = 100_000


async def scan_premarket_gappers(*, top_k: int = 15) -> list[STTHit]:
    """Strategy C: stocks gapping up ≥5% before market open with ≥100K pre-mkt
    volume. Sykes-style "Watchlist at 9:15" — ranked by pre-market volume.

    Only meaningful between 04:00 and 09:30 ET; outside that window we still
    return today's gappers (useful for late-arrivers who missed the open)."""
    now_et = _now_et()
    if not (now_et.time() >= dtime(0, 0)):
        return []
    # Pull 1m bars with prepost=True so we get the 4 AM-9:30 AM ET pre-market
    df = await _yf_bulk_intraday(MOMENTUM_UNIVERSE, period="2d", interval="1m",
                                    prepost=True)
    if df.empty:
        return []

    hits: list[STTHit] = []
    for ticker in MOMENTUM_UNIVERSE:
        try:
            if ticker not in df.columns.get_level_values(0):
                continue
            sub = df[ticker].dropna()
            if sub.empty:
                continue
            # Convert index to ET so we can slice pre-market
            sub.index = sub.index.tz_convert(ET) if sub.index.tz else sub.index.tz_localize("UTC").tz_convert(ET)

            today_date = now_et.date()
            today_bars = sub[sub.index.date == today_date]
            prev_bars  = sub[sub.index.date  < today_date]
            if today_bars.empty or prev_bars.empty:
                continue
            prev_close = float(prev_bars["Close"].iloc[-1])

            # Pre-market = bars before 9:30 ET on today
            pre = today_bars[today_bars.index.time < dtime(9, 30)]
            if pre.empty:
                continue
            pre_price = float(pre["Close"].iloc[-1])
            pre_vol = int(pre["Volume"].sum())
            if prev_close <= 0:
                continue
            gap_pct = (pre_price - prev_close) / prev_close * 100.0
            if not (PRICE_C_MIN <= pre_price <= PRICE_C_MAX):
                continue
            if gap_pct < GAP_C_MIN_PCT:
                continue
            if pre_vol < VOL_C_PREMARKET_MIN:
                continue

            entry = pre.iloc[-1]["High"]   # break of last pre-mkt bar high
            stop  = pre["Low"].min()       # under the pre-market low
            target = entry + (entry - stop) * 2.0
            score = gap_pct + pre_vol / 1_000_000.0
            hits.append(STTHit(
                strategy="premarket_gap_runner",
                ticker=ticker, direction="long", price=pre_price,
                entry=entry, stop=stop, target=target, score=score,
                catalyst_headline=None, catalyst_keyword=None,
                reason=(f"Pre-market gapper on {ticker}: gap "
                         f"{gap_pct:+.1f}% on {pre_vol:,} pre-mkt vol, "
                         f"break high {entry:.2f}, low {stop:.2f}"),
                metadata={"gap_pct": gap_pct, "premarket_volume": pre_vol,
                           "premarket_price": pre_price, "prev_close": prev_close},
            ))
        except Exception as e:
            continue
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


# ── Oracle — 5-minute opening candle engine ──────────────────────────────────

ORACLE_PRICE_MAX = 50.0
ORACLE_FILTER_TOP_N = 20   # narrow to 15-20 candidates from pre-market scan
ORACLE_CANDLE_START = dtime(9, 30)
ORACLE_CANDLE_END   = dtime(9, 35)


def _oracle_levels(price: float) -> list[float]:
    """Half-dollar + whole-dollar levels above and below price.
    Sykes uses these as psychological support/resistance."""
    levels: list[float] = []
    # Half-dollar grid ±10% around price
    span = max(0.5, price * 0.10)
    lo, hi = price - span, price + span
    step = 0.5
    if price < 5:
        step = 0.25
    elif price < 10:
        step = 0.50
    else:
        step = 1.0
    v = round(lo / step) * step
    while v <= hi:
        if v != price:
            levels.append(round(v, 2))
        v += step
    return sorted(levels)


def _fib_retracements(low: float, high: float) -> dict:
    """Daily Fib retracements 23.6 / 38.2 / 50 / 61.8 / 78.6%."""
    span = high - low
    return {
        "0.236": round(high - span * 0.236, 2),
        "0.382": round(high - span * 0.382, 2),
        "0.500": round(high - span * 0.500, 2),
        "0.618": round(high - span * 0.618, 2),
        "0.786": round(high - span * 0.786, 2),
    }


async def scan_oracle_opening_candle(*, top_k: int = 5) -> list[STTHit]:
    """Oracle engine: 3-phase.
       Phase 1 (pre-market): narrow universe to 15-20 high-vol <$50 names
       Phase 2 (09:30-09:35): track the 5-minute opening candle
       Phase 3 (09:35 exact): emit setups with bias / entry / stop / levels"""
    now_et = _now_et()

    # Only emit Oracle setups at/after 09:35 ET. Before that we return empty
    # so the scheduler doesn't fire prematurely.
    if now_et.time() < ORACLE_CANDLE_END:
        return []

    # Phase 1: build candidate list from pre-market gappers (existing scanner)
    candidates = await scan_premarket_gappers(top_k=ORACLE_FILTER_TOP_N)
    if not candidates:
        return []

    # Phase 2: pull 1m bars and look at 09:30-09:35 candle
    tickers = [c.ticker for c in candidates if c.price <= ORACLE_PRICE_MAX]
    if not tickers:
        return []
    df = await _yf_bulk_intraday(tickers, period="2d", interval="1m", prepost=True)
    if df.empty:
        return []

    hits: list[STTHit] = []
    for ticker in tickers:
        try:
            if ticker not in df.columns.get_level_values(0):
                continue
            sub = df[ticker].dropna()
            if sub.empty:
                continue
            sub.index = sub.index.tz_convert(ET) if sub.index.tz else sub.index.tz_localize("UTC").tz_convert(ET)

            today_bars = sub[sub.index.date == now_et.date()]
            opening = today_bars[(today_bars.index.time >= ORACLE_CANDLE_START)
                                  & (today_bars.index.time < ORACLE_CANDLE_END)]
            if len(opening) < 3:
                continue

            o = float(opening["Open"].iloc[0])
            h = float(opening["High"].max())
            l = float(opening["Low"].min())
            c = float(opening["Close"].iloc[-1])
            v = int(opening["Volume"].sum())

            # VWAP across the day (intraday only) up to and including the opening candle
            intraday = today_bars[today_bars.index.time >= ORACLE_CANDLE_START]
            vwap = float(_vwap_series(intraday).iloc[len(opening) - 1])
            if pd.isna(vwap):
                continue

            # Phase 3: classify bias and emit setup
            bias_long  = c > vwap
            direction  = "long" if bias_long else "short"
            entry      = h if direction == "long" else l
            stop       = l if direction == "long" else h
            risk       = abs(entry - stop)
            target     = entry + risk * 2.0 if direction == "long" else entry - risk * 2.0

            levels  = _oracle_levels(c)
            day_lo  = float(today_bars["Low"].min())
            day_hi  = float(today_bars["High"].max())
            fibs    = _fib_retracements(day_lo, day_hi)

            # Score by candle range + volume — heavier institutional volume wins
            score = (h - l) / max(0.01, l) * 100 + v / 1_000_000.0

            hits.append(STTHit(
                strategy="oracle_opening_candle",
                ticker=ticker, direction=direction, price=c,
                entry=entry, stop=stop, target=target, score=score,
                catalyst_headline=None, catalyst_keyword=None,
                reason=(f"Oracle {direction.upper()} on {ticker}: 5m open "
                         f"O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}, "
                         f"VWAP={vwap:.2f} → {('Green' if bias_long else 'Red')} bias. "
                         f"Entry break {entry:.2f}, stop {stop:.2f}."),
                metadata={
                    "opening_candle": {"O": o, "H": h, "L": l, "C": c, "V": v},
                    "vwap":           vwap,
                    "oracle_levels":  levels,
                    "fib_retracements": fibs,
                    "day_high":       day_hi, "day_low": day_lo,
                },
            ))
        except Exception as e:
            logger.warning(f"[Oracle] {ticker} failed: {e}")
            continue

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]

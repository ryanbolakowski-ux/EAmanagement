"""Behaviour tests for the AMD family (build step 5): Power of 3 (PO3),
Judas Swing, and London Sweep into NY - all over the shared ``AMDCore``
skeleton (proposal SS3.3 / SS3.4 / SS3.5 / SS3.9c).

Run standalone (inside the backend container / image):
    pytest backend/tests/test_setup_amd_family.py -v -p no:cacheprovider

The canonical AMD sequence proven for each variant:
  * **Accumulation** - the configured range (Asian 20:00-00:00 ET) is mapped.
  * **Manipulation** - a liquidity sweep of one side of that range fires in the
    variant's manipulation killzone (low sweep -> long bias, high -> short).
  * **Distribution** - after the sweep, an MSS + displacement leaving an FVG in
    the OPPOSITE direction fires in the entry killzone; entry at the FVG CE,
    stop 2 ticks beyond the sweep extreme, target the opposing-side draw, RR
    >= the variant minimum.

Positive cases (PO3 long + short, Judas, London) fire the right direction with
valid geometry and RR>=min. Negative cases (no sweep / sweep in the wrong
session / no MSS / RR too small / max-per-day) return None. Finally, the
registry resolves PO3/Judas/London to their dedicated setups while the
still-unported NY PM + SMT fall back to None.

All bars are on the 1m execution TF. Timestamps are UTC; the ET clock is
derived from them (winter 2024-01-09, EST = UTC-5):
  * Asian 20:00-00:00 ET == 01:00-05:00 UTC.
  * London 02:00-05:00 ET == 07:00-10:00 UTC (LONDON_OPEN 02:00-03:00 ET ==
    07:00-08:00 UTC).
  * NY entry 09:30-11:00 ET == 14:30-16:00 UTC.
"""
import pandas as pd
import pytest

from app.engines.ict import registry as reg
from app.engines.ict.context import ICTContext
from app.engines.ict.setups.amd_core import AMDCore
from app.engines.ict.setups.po3 import PowerOfThree
from app.engines.ict.setups.judas_swing import JudasSwing
from app.engines.ict.setups.london_into_ny import LondonSweepIntoNY
from app.engines.strategy_engine.base_strategy import StrategyConfig, SignalType
from app.engines.strategy_engine.indicators import (
    detect_liquidity_sweeps, detect_fvgs, find_swing_highs, find_swing_lows,
)
from app.engines.ict.primitives import detect_mss, session_range

TICK = 0.25  # ES


# ===========================================================================
# Synthetic-data builders.
# ===========================================================================
def _frame(rows, start_utc):
    idx = pd.date_range(start=start_utc, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


def _bar(o, h, l, c, v=1000):
    return [float(o), float(h), float(l), float(c), v]


def _zigzag(pivots, fill=2):
    """OHLC rows passing through ``pivots`` with ``fill`` intermediate bars, so
    each interior pivot is a STRICT isolated swing (unique extreme in +-3). A
    pivot higher than both neighbours -> swing high; lower -> swing low; the
    intermediate bars interpolate strictly between the surrounding pivots."""
    rows = []
    for k in range(len(pivots) - 1):
        a = pivots[k]; b = pivots[k + 1]
        is_high = k > 0 and pivots[k] > pivots[k - 1] and pivots[k] > pivots[k + 1]
        is_low = k > 0 and pivots[k] < pivots[k - 1] and pivots[k] < pivots[k + 1]
        if is_high:
            rows.append(_bar(a - 0.5, a, a - 1.0, a - 0.5))
        elif is_low:
            rows.append(_bar(a + 0.5, a + 1.0, a, a + 0.5))
        else:
            rows.append(_bar(a, a + 0.4, a - 0.4, a))
        for j in range(1, fill + 1):
            frac = j / (fill + 1)
            mid = a + (b - a) * frac
            lo_b, hi_b = min(a, b), max(a, b)
            m = max(lo_b + 0.3, min(hi_b - 0.3, mid))
            rows.append(_bar(m, m + 0.3, m - 0.3, m))
    last = pivots[-1]
    rows.append(_bar(last, last + 0.4, last - 0.4, last))
    return rows


# --- Asian accumulation block: HIGH 5030, LOW 5000, ends on the low side so no
# intermediate Asian swing high sits between a low-FVG and 5030 (only 5030). ---
def _asian_block(start_utc="2024-01-09 04:20"):
    return _frame(_zigzag([5015, 5030, 5000], fill=2), start_utc)


# Distribution displacement (3 candles) that leaves a BULLISH FVG [5001,5004]
# (CE 5002.5), closing 5008 to break a recent swing high (<=5003) with a strong
# body. Used for the LONG variants. The preceding sweep wicks just below 5000.
_LONG_DISTRIBUTION = [
    _bar(5000, 5002, 5000, 5001),        # bounce up (isolate the swept low)
    _bar(5001, 5003, 5000, 5002),
    _bar(5002, 5003, 5000, 5001),
    _bar(5001, 5002, 4999, 5001),        # SWEEP: shallow wick 4999 < 5000, close 5001 > 5000
    _bar(5001, 5001, 5000, 5001),        # c1 high 5001
    _bar(5001, 5005, 5000, 5004, 2000),  # displacement up (clears all London highs <=5003)
    _bar(5005, 5009, 5004, 5008, 1800),  # c3 strong body: low 5004>5001 -> FVG [5001,5004]; close 5008 breaks 5003 -> MSS up
]
# The down-trend pre-sweep London structure (lower highs 5003/5002.5, lower
# lows 5001/5000) so detect_mss classifies "down" then flips "up".
_LONG_PRE = [5002, 5003, 5001, 5002.5, 5000]


def _po3_long_bars():
    """PO3 long: Asian range mapped; London sweeps the low; London distribution
    MSS up + bullish FVG. Everything in the London window (07:0x-07:2x UTC ==
    02:0x ET)."""
    london = _frame(_zigzag(_LONG_PRE, fill=2) + _LONG_DISTRIBUTION, "2024-01-09 07:00")
    return {"1m": pd.concat([_asian_block(), london]), "15m": pd.concat([_asian_block(), london])}


# --- SHORT mirror: Asian HIGH 5030 swept, distribution MSS down + bearish FVG.
# Mirror the long around the range: end Asian on the HIGH side, up-trend pre, a
# high sweep just above 5030, then a displacement DOWN leaving a bearish FVG. ---
def _asian_block_short(start_utc="2024-01-09 04:20"):
    # End on the HIGH side so no intermediate Asian swing low sits between a
    # high-FVG and the range low 5000 (only 5000).
    return _frame(_zigzag([5015, 5000, 5030], fill=2), start_utc)


_SHORT_PRE = [5028, 5027, 5029, 5027.5, 5030]
_SHORT_DISTRIBUTION = [
    _bar(5030, 5030, 5028, 5029),         # pull down (isolate the swept high)
    _bar(5029, 5030, 5027, 5028),
    _bar(5028, 5030, 5027, 5029),
    _bar(5029, 5031, 5029, 5029),         # SWEEP: shallow wick 5031 > 5030, close 5029 < 5030
    _bar(5029, 5030, 5029, 5029),         # c1 low 5029
    _bar(5029, 5030, 5025, 5026, 2000),   # displacement down (clears all London lows >=5027)
    _bar(5025, 5026, 5021, 5022, 1800),   # c3 strong body: high 5026<5029 -> bearish FVG [5026,5029]; close 5022 breaks 5027 -> MSS down
]


def _po3_short_bars():
    london = _frame(_zigzag(_SHORT_PRE, fill=2) + _SHORT_DISTRIBUTION, "2024-01-09 07:00")
    a = _asian_block_short()
    return {"1m": pd.concat([a, london]), "15m": pd.concat([a, london])}


def _judas_bars():
    """Judas: same long geometry but the sweep + MSS land in the LONDON_OPEN
    window (02:00-03:00 ET == 07:00-08:00 UTC) AND within 60 min of each other
    (they are ~10 bars / minutes apart)."""
    london = _frame(_zigzag(_LONG_PRE, fill=2) + _LONG_DISTRIBUTION, "2024-01-09 07:10")
    a = _asian_block()
    return {"1m": pd.concat([a, london]), "15m": pd.concat([a, london])}


def _london_into_ny_bars():
    """London Sweep into NY: London (07:0x UTC == 02:0x ET) sweeps the Asian
    high; the NY window (14:30-16:00 UTC == 09:30-11:00 ET) reverses with an
    MSS down + bearish FVG. The manipulation and distribution are time-separated
    (London vs NY), the canonical structure of this setup."""
    # London manipulation block: up-trend into a sweep of the Asian HIGH 5030.
    london = _frame(_zigzag(_SHORT_PRE, fill=2) + [
        _bar(5030, 5030, 5028, 5029),
        _bar(5029, 5030, 5027, 5028),
        _bar(5028, 5030, 5027, 5029),
        _bar(5029, 5031, 5029, 5029),     # SWEEP of 5030 in London (07:xx UTC)
    ], "2024-01-09 07:00")
    # NY distribution block (14:40 UTC == 09:40 ET): re-establish a down-trend
    # structure local to NY then the MSS-down displacement + bearish FVG on the
    # last bar. Pre-NY pivots make lower highs/lows so detect_mss sees "down".
    ny = _frame(_zigzag([5029, 5028.5, 5030, 5028, 5031], fill=2) + [
        _bar(5031, 5031, 5029, 5030),     # c-context
        _bar(5030, 5030, 5029, 5029),     # c1 low 5029
        _bar(5029, 5030, 5025, 5026, 2000),  # displacement down
        _bar(5025, 5026, 5021, 5022, 1800),  # c3 high 5026<5029 -> bearish FVG [5026,5029]; close 5022 -> MSS down
    ], "2024-01-09 14:40")
    a = _asian_block_short()
    df = pd.concat([a, london, ny])
    return {"1m": df, "15m": df}


# ===========================================================================
# Config + context helpers.
# ===========================================================================
def _cfg(name, rr=3.0):
    return StrategyConfig(
        name=name, instruments=["ES"],
        primary_timeframe="15m", execution_timeframe="1m",
        higher_timeframes=["4H"], risk_reward_ratio=rr, fvg_min_size_ticks=1,
        max_contracts=2, session_filters=[],
    )


def _ctx(bars, name, extra=None, rr=3.0):
    c = ICTContext.from_bars(bars, "ES", _cfg(name, rr=rr))
    if extra is not None:
        c.extra = extra
    return c


# ===========================================================================
# 1) PO3 - fires LONG.
# ===========================================================================
def test_po3_fires_long():
    ctx = _ctx(_po3_long_bars(), "Power of 3 (PO3)")
    sig = PowerOfThree().evaluate(ctx)
    assert sig is not None, "PO3 must fire on the canonical Asian-low sweep + London distribution"
    assert sig.signal == SignalType.LONG
    # Entry at the bullish-FVG CE.
    assert sig.entry_price == pytest.approx(5002.5)
    # Stop 2 ticks below the sweep extreme (4999 - 0.5).
    assert sig.stop_loss == pytest.approx(4998.5)
    # Target = the opposing draw (Asian range HIGH 5030).
    assert sig.take_profit == pytest.approx(5030.0)
    assert sig.take_profit > sig.entry_price > sig.stop_loss
    rr = (sig.take_profit - sig.entry_price) / (sig.entry_price - sig.stop_loss)
    assert rr >= 3.0
    md = sig.metadata
    assert md["setup"] == "po3"
    assert md["swept_side"] == "low"
    assert md["mode"] == "reversal"
    assert md["entry_mode"] == "fvg_ce"
    assert md["max_trades_per_day"] == 1
    assert sig.contracts == 2


# ===========================================================================
# 2) PO3 - fires SHORT (high swept -> bearish distribution).
# ===========================================================================
def test_po3_fires_short():
    ctx = _ctx(_po3_short_bars(), "Power of 3 (PO3)")
    sig = PowerOfThree().evaluate(ctx)
    assert sig is not None, "PO3 must fire SHORT on the Asian-high sweep + bearish distribution"
    assert sig.signal == SignalType.SHORT
    # Entry at the bearish-FVG CE [5026,5029] -> 5027.5.
    assert sig.entry_price == pytest.approx(5027.5)
    # Stop 2 ticks above the sweep extreme (5031 + 0.5).
    assert sig.stop_loss == pytest.approx(5031.5)
    # Target = the opposing draw (Asian range LOW 5000).
    assert sig.take_profit == pytest.approx(5000.0)
    assert sig.take_profit < sig.entry_price < sig.stop_loss
    rr = (sig.entry_price - sig.take_profit) / (sig.stop_loss - sig.entry_price)
    assert rr >= 3.0
    assert sig.metadata["swept_side"] == "high"


# ===========================================================================
# 3) PO3 negatives.
# ===========================================================================
def test_po3_no_fire_when_no_sweep():
    """A flat Asian range with London that NEVER sweeps either extreme -> None."""
    london = _frame(_zigzag([5010, 5012, 5008, 5012, 5009], fill=2) + [
        _bar(5009, 5011, 5009, 5010), _bar(5010, 5012, 5009, 5011),
        _bar(5011, 5013, 5010, 5012), _bar(5012, 5014, 5011, 5013),
    ], "2024-01-09 07:00")
    a = _asian_block()
    ctx = _ctx({"1m": pd.concat([a, london]), "15m": pd.concat([a, london])}, "Power of 3 (PO3)")
    assert PowerOfThree().evaluate(ctx) is None


def test_po3_no_fire_when_sweep_in_wrong_session():
    """The SAME long geometry but shifted so the sweep + distribution land in
    the NY window (14:0x UTC == 09:0x ET), OUTSIDE PO3's London manipulation +
    entry killzone -> None (entry-killzone gate)."""
    london = _frame(_zigzag(_LONG_PRE, fill=2) + _LONG_DISTRIBUTION, "2024-01-09 14:00")
    a = _asian_block()
    ctx = _ctx({"1m": pd.concat([a, london]), "15m": pd.concat([a, london])}, "Power of 3 (PO3)")
    assert PowerOfThree().evaluate(ctx) is None


def test_po3_no_fire_when_no_mss():
    """Sweep of the low occurs, but price keeps going DOWN (no MSS up) -> None."""
    london = _frame(_zigzag(_LONG_PRE, fill=2) + [
        _bar(5000, 5002, 5000, 5001),
        _bar(5001, 5002, 4999, 5001),     # SWEEP of the low
        _bar(5001, 5001, 4996, 4997, 2000),   # continuation DOWN (no MSS up)
        _bar(4997, 4998, 4993, 4994, 1800),
        _bar(4994, 4995, 4990, 4991, 1700),
    ], "2024-01-09 07:00")
    a = _asian_block()
    ctx = _ctx({"1m": pd.concat([a, london]), "15m": pd.concat([a, london])}, "Power of 3 (PO3)")
    assert PowerOfThree().evaluate(ctx) is None


def test_po3_no_fire_when_rr_too_small():
    """A nearby opposing pool makes RR < 3 -> None. Demand a high min RR via
    config so the opposing draw (Asian HIGH 5030) is too close in R terms."""
    ctx = _ctx(_po3_long_bars(), "Power of 3 (PO3)", rr=10.0)
    # RR achievable here is ~6.9; demanding 10 makes the pool < min_rr away.
    assert PowerOfThree().evaluate(ctx) is None


def test_po3_max_one_trade_per_day():
    shared = {}
    ctx1 = _ctx(_po3_long_bars(), "Power of 3 (PO3)", extra=shared)
    assert PowerOfThree().evaluate(ctx1) is not None
    # A second identical setup on the SAME ET date (shared ledger) is capped.
    ctx2 = _ctx(_po3_long_bars(), "Power of 3 (PO3)", extra=shared)
    assert PowerOfThree().evaluate(ctx2) is None


# ===========================================================================
# 4) Judas Swing - fires (London-open false move reverses within 60 min).
# ===========================================================================
def test_judas_fires_long():
    ctx = _ctx(_judas_bars(), "Judas Swing")
    sig = JudasSwing().evaluate(ctx)
    assert sig is not None, "Judas must fire on the London-open sweep + reversal MSS within 60 min"
    assert sig.signal == SignalType.LONG
    assert sig.entry_price == pytest.approx(5002.5)
    assert sig.stop_loss == pytest.approx(4998.5)
    assert sig.take_profit > sig.entry_price > sig.stop_loss
    rr = (sig.take_profit - sig.entry_price) / (sig.entry_price - sig.stop_loss)
    assert rr >= 3.0
    assert sig.metadata["setup"] == "judas_swing"
    assert sig.metadata["max_trades_per_day"] == 2


def test_judas_no_fire_outside_london_open():
    """The same false move shifted to plain London (04:0x ET == 09:0x UTC),
    OUTSIDE the LONDON_OPEN 02:00-03:00 manipulation killzone -> None (the
    sweep is not in the manipulation window)."""
    london = _frame(_zigzag(_LONG_PRE, fill=2) + _LONG_DISTRIBUTION, "2024-01-09 09:10")
    a = _asian_block()
    ctx = _ctx({"1m": pd.concat([a, london]), "15m": pd.concat([a, london])}, "Judas Swing")
    assert JudasSwing().evaluate(ctx) is None


# ===========================================================================
# 5) London Sweep into NY - fires SHORT (London sweeps high, NY reverses down).
# ===========================================================================
def test_london_into_ny_fires_short():
    ctx = _ctx(_london_into_ny_bars(), "London Sweep into NY")
    sig = LondonSweepIntoNY().evaluate(ctx)
    assert sig is not None, "London-into-NY must fire on the London high sweep + NY reversal"
    assert sig.signal == SignalType.SHORT
    assert sig.entry_price == pytest.approx(5027.5)
    assert sig.take_profit < sig.entry_price < sig.stop_loss
    rr = (sig.entry_price - sig.take_profit) / (sig.stop_loss - sig.entry_price)
    assert rr >= 3.0
    assert sig.metadata["setup"] == "london_into_ny"
    assert sig.metadata["swept_side"] == "high"


def test_london_into_ny_no_fire_when_entry_not_in_ny():
    """If the distribution/MSS lands in London (07:xx UTC) instead of the NY
    entry window (14:30-16:00 UTC), the entry-killzone gate -> None."""
    # Reuse the SHORT London-only build (sweep + MSS both in London). London-
    # into-NY requires the entry in NY, so this must stand aside.
    ctx = _ctx(_po3_short_bars(), "London Sweep into NY")
    assert LondonSweepIntoNY().evaluate(ctx) is None


# ===========================================================================
# 6) Registry resolution: PO3/Judas/London dedicated; NY PM + SMT fall back.
# ===========================================================================
def test_get_setup_resolves_amd_family():
    assert isinstance(reg.get_setup("Power of 3 (PO3)"), PowerOfThree)
    assert isinstance(reg.get_setup("PO3"), PowerOfThree)
    assert isinstance(reg.get_setup("x", {"ict_setup": "po3"}), PowerOfThree)
    assert isinstance(reg.get_setup("Judas Swing"), JudasSwing)
    assert isinstance(reg.get_setup("x", {"ict_setup": "judas_swing"}), JudasSwing)
    assert isinstance(reg.get_setup("London Sweep into NY"), LondonSweepIntoNY)
    assert isinstance(reg.get_setup("x", {"ict_setup": "london_into_ny"}), LondonSweepIntoNY)


@pytest.mark.parametrize("other", [
    "NY PM Reversal", "SMT Divergence Reversal",
])
def test_unported_strategies_still_fall_back(other):
    """The still-unported NY PM + SMT must resolve to None (= generic fallback),
    proving porting the AMD family did not disturb the fallback path."""
    assert reg.get_setup(other) is None

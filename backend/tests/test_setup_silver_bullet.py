"""Behaviour tests for the dedicated ICT Silver Bullet setup (build step 4).

Run standalone (inside the backend container / image):
    pytest backend/tests/test_setup_silver_bullet.py -v -p no:cacheprovider

Proves the user-locked SS3.2 rules:
  * A synthetic 10:15 ET bullish FVG with 1H bias UP and opposing liquidity
    >= 2R away FIRES a LONG with valid geometry (tgt > entry > stop), entry at
    the FVG CE, stop 2 ticks beyond the FVG low.
  * The SAME setup shifted to 09:45 ET (before the 10-11 window) -> None.
  * An FVG whose nearest opposing liquidity is < 2R away -> None (RR gate).
  * A second qualifying setup the same ET day -> None (max 1 trade/day).
  * 1H bias DOWN with a bullish FVG -> None (bias disagreement).
  * get_setup("ICT Silver Bullet") returns the dedicated setup (and the short
    rule_tree id "silver_bullet" too); PO3/Judas/SMT/London/NY-PM still None
    (the generic fallback is unaffected).

The bars are 5m primary + 1H bias. ET is derived from the bar timestamps
(14:00 UTC == 10:00 ET on this date), and the LATEST bar drives the window
gate (mirrors live ``on_bar`` where the latest bar is "now").
"""
import pandas as pd
import pytest

from app.engines.ict import registry as reg
from app.engines.ict.context import ICTContext
from app.engines.ict.setups.silver_bullet import SilverBullet
from app.engines.strategy_engine.base_strategy import StrategyConfig, SignalType


TICK = 0.25  # ES


# ---------------------------------------------------------------------------
# Builders.
# ---------------------------------------------------------------------------
def _cfg(rr=2.0, name="ICT Silver Bullet"):
    return StrategyConfig(
        name=name, instruments=["ES"],
        primary_timeframe="5m", execution_timeframe="1m",
        higher_timeframes=["1H"], risk_reward_ratio=rr, fvg_min_size_ticks=1,
        max_contracts=2, session_filters=[],
    )


def _htf(direction="up", n=18, start="2024-03-04 04:00"):
    """1H frame whose EMA(9) sits above/below EMA(21) -> bullish/bearish bias.

    Kept SHORT and ending before the 5m window so it never dominates the
    context's ``now_et`` (which the window gate derives from the 5m frame
    anyway). >=15 bars so the EMA bias is defined.
    """
    idx = pd.date_range(start=start, periods=n, freq="1h", tz="UTC")
    rows = []
    p = 4960.0 if direction == "up" else 5040.0
    step = 2.0 if direction == "up" else -2.0
    for i in range(n):
        o = p; c = o + step; h = max(o, c) + 1; l = min(o, c) - 1
        rows.append([o, h, l, c, 1000 + i]); p = c
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


def _primary_bull_fvg(et_min=615, target_high=None, end_pad=3):
    """5m primary bars carrying exactly ONE bullish FVG after 10:00 ET, whose
    completing (3rd) candle lands at ET minute ``et_min`` (615 == 10:15).

    Bars ramp UP SMOOTHLY before the window (no stray gaps), the displacement
    candle ``j-1`` opens a clean gap so the FVG is [5001, 5002] (CE 5001.5),
    then post-FVG bars RETRACE down with LOWER highs that never exceed the
    entry - so the ONLY liquidity above the entry is the controlled
    ``target_high`` we plant. This lets the test dial RR precisely. The frame
    ENDS ``end_pad`` bars after the FVG (still inside 10-11 ET) so the
    window gate (keyed on the last bar) passes.
    """
    start = "2024-03-04 13:00"  # 09:00 ET -> pre-window context
    n = 48
    idx = pd.date_range(start=start, periods=n, freq="5min", tz="UTC")
    et = idx.tz_convert("US/Eastern")
    mins = et.hour * 60 + et.minute
    j = [i for i, m in enumerate(mins) if m == et_min][0]

    # Tight FLAT band around 5000.6 for ALL bars (high 5000.85, low 5000.35).
    # Consecutive bars overlap heavily, so NO stray FVG forms anywhere in the
    # pre/post context - the ONLY bullish FVG is the displacement one at bar j.
    rows = [[5000.6, 5000.85, 5000.35, 5000.6, 1000 + i] for i in range(n)]
    df = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])

    # Pin the three FVG candles so the ONLY bullish FVG is the one at bar j:
    #   bullish FVG at i  <=>  low[i] > high[i-2].
    #   * c1 (j-2): high = 5001; its low (5000.5) <= the flat-band high (5000.85)
    #     of bar j-4, so c1 itself does NOT open an earlier gap.
    #   * displacement (j-1): low = 5000.4 <= high[j-3] (5000.85) -> no gap at j-1.
    #   * c3 (j): low = 5002 > high[j-2] = 5001 -> the gap [5001, 5002].
    df.iloc[j - 2] = [5000.6, 5001.0, 5000.5, 5000.9, 1500]    # c1: high = 5001
    df.iloc[j - 1] = [5000.7, 5002.5, 5000.4, 5002.2, 1600]    # displacement up
    df.iloc[j]     = [5002.2, 5002.6, 5002.0, 5002.4, 1700]    # c3: low = 5002 > 5001
    # FVG = [5001, 5002]; CE = 5001.5. stop = 5001 - 2*0.25 = 5000.5; risk = 1.0.

    # Retrace bars after the FVG: dip back toward the CE with LOWER highs that
    # do NOT exceed the entry (so they are not "opposing liquidity above").
    for k in range(1, end_pad + 1):
        if j + k >= n:
            break
        df.iloc[j + k] = [5001.5, 5001.5, 5001.0, 5001.2, 1800 + k]

    # Plant the opposing-liquidity high (the draw the displacement runs toward)
    # a couple of bars later, still in-window, ABOVE everything else. Only its
    # HIGH matters as the pool; keep close modest so it is not a new FVG.
    if target_high is not None:
        tj = min(j + 2, n - 1)
        df.iloc[tj] = [5001.4, float(target_high), 5001.0, 5001.3, 1900]

    end = min(j + end_pad + 1, n)
    return df.iloc[:end].copy()


def _bars(et_min=615, bias="up", target_high=5005.5, end_pad=3):
    return {
        "5m": _primary_bull_fvg(et_min=et_min, target_high=target_high, end_pad=end_pad),
        "1H": _htf("up" if bias == "up" else "down"),
    }


def _ctx(bars, extra=None):
    c = ICTContext.from_bars(bars, "ES", _cfg())
    if extra is not None:
        c.extra = extra
    return c


# ---------------------------------------------------------------------------
# 1) Fires LONG: 10:15 bullish FVG, 1H up, liquidity >= 2R away.
# ---------------------------------------------------------------------------
def test_fires_long_in_window_with_bias_and_rr():
    # target_high 5005.5 -> reward = 5005.5 - 5001.5 = 4.0; risk = 1.0 -> 4R (>=2).
    ctx = _ctx(_bars(et_min=615, bias="up", target_high=5005.5))
    sig = SilverBullet().evaluate(ctx)

    assert sig is not None, "must fire on the in-window bias-aligned FVG with RR>=2"
    assert sig.signal == SignalType.LONG
    # Entry at the FVG CE (consequent encroachment).
    assert sig.entry_price == pytest.approx(5001.5)
    # Stop 2 ticks beyond the FVG low (5001 - 0.5).
    assert sig.stop_loss == pytest.approx(5000.5)
    # Target is the nearest opposing liquidity (the planted session high).
    assert sig.take_profit == pytest.approx(5005.5)
    # Valid long geometry.
    assert sig.take_profit > sig.entry_price > sig.stop_loss
    # RR honoured (>= 2).
    rr = (sig.take_profit - sig.entry_price) / (sig.entry_price - sig.stop_loss)
    assert rr >= 2.0
    # Self-identification + metadata.
    assert sig.metadata.get("setup") == "silver_bullet"
    assert sig.metadata.get("session") == "SILVER_BULLET"
    assert sig.metadata.get("entry_mode") == "fvg_ce"
    assert sig.metadata.get("bias") == "bullish"
    assert sig.metadata.get("fvg_type") == "bullish"
    assert sig.metadata.get("max_trades_per_day") == 1
    assert sig.contracts == 2


# ---------------------------------------------------------------------------
# 2) Before the window (09:45 ET) -> None.
# ---------------------------------------------------------------------------
def test_no_fire_before_window_0945():
    # 585 == 09:45 ET. Same FVG geometry, but the latest bar is < 10:00 -> gate.
    ctx = _ctx(_bars(et_min=585, bias="up", target_high=5005.5))
    assert SilverBullet().evaluate(ctx) is None


def test_no_fire_after_window_1115():
    # 675 == 11:15 ET, past the 11:00 close -> done for the day.
    ctx = _ctx(_bars(et_min=675, bias="up", target_high=5005.5))
    assert SilverBullet().evaluate(ctx) is None


# ---------------------------------------------------------------------------
# 3) RR gate: nearest opposing liquidity < 2R away -> None.
# ---------------------------------------------------------------------------
def test_no_fire_when_liquidity_under_2R():
    # risk = 1.0; entry = 5001.5. A pool at 5003.0 is only 1.5R away (< 2R).
    ctx = _ctx(_bars(et_min=615, bias="up", target_high=5003.0))
    assert SilverBullet().evaluate(ctx) is None


# ---------------------------------------------------------------------------
# 4) Max 1 trade/day: a second qualifying setup the same ET day -> None.
# ---------------------------------------------------------------------------
def test_max_one_trade_per_day():
    shared = {}
    ctx1 = _ctx(_bars(et_min=615, bias="up", target_high=5005.5), extra=shared)
    first = SilverBullet().evaluate(ctx1)
    assert first is not None

    # A second, later-in-window qualifying setup sharing the same ET-date
    # ledger (ctx.extra) must be capped.
    ctx2 = _ctx(_bars(et_min=645, bias="up", target_high=5005.5), extra=shared)
    assert SilverBullet().evaluate(ctx2) is None


# ---------------------------------------------------------------------------
# 5) Bias disagreement: 1H down but a bullish FVG -> None.
# ---------------------------------------------------------------------------
def test_no_fire_when_bias_disagrees():
    ctx = _ctx(_bars(et_min=615, bias="down", target_high=5005.5))
    assert SilverBullet().evaluate(ctx) is None


# ---------------------------------------------------------------------------
# 6) Registry: dedicated for Silver Bullet; others still fall back.
# ---------------------------------------------------------------------------
def test_get_setup_returns_dedicated_for_silver_bullet():
    s = reg.get_setup("ICT Silver Bullet")
    assert isinstance(s, SilverBullet)
    # also resolvable via the bare seed alias and the rule_tree id
    assert isinstance(reg.get_setup("Silver Bullet"), SilverBullet)
    assert isinstance(reg.get_setup("x", {"ict_setup": "silver_bullet"}), SilverBullet)


@pytest.mark.parametrize("other", [
    "Power of 3", "PO3", "Judas Swing", "SMT Divergence Reversal",
    "London Sweep into NY", "NY PM Reversal", "Reversal Swing",
    "IOFED Precision Entry", "AMD Strategy", "ICT 2022 Model (AMD)",
])
def test_other_strategies_still_fall_back(other):
    """Porting Silver Bullet must not affect any un-ported strategy: they all
    still resolve to None (= use the generic engine fallback)."""
    assert reg.get_setup(other) is None

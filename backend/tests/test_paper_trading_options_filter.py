"""BUG C: the OptionsPaperPanel dropdown filter must include 'unknown'
(template) strategies so users with empty-instrument templates don't end
up with "only a few strategies" visible.

The frontend filter is mirrored as a python function here so we can unit
test the predicate without spinning up a browser. The classify rule lives
in frontend/src/utils/assetClass.ts and backend/app/engines/strategy_classification.py.

Run: pytest backend/tests/test_paper_trading_options_filter.py -v -p no:cacheprovider
"""
from __future__ import annotations


def _classify(instruments):
    """Mirror frontend/src/utils/assetClass.ts classifyAssetClass."""
    if not instruments:
        return "unknown"
    syms = [s.upper().strip() for s in instruments if s]
    if not syms:
        return "unknown"
    import re
    occ = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
    if any(occ.match(s) for s in syms):
        return "options"
    futures = {"ES","NQ","RTY","YM","MES","MNQ","M2K","MYM"}
    if any(s in futures for s in syms):
        return "futures"
    return "stock"


def _opt_strat_filter(s: dict) -> bool:
    """Mirror PaperTrading.tsx OptionsPaperPanel.optStrats post-2026-06-04."""
    cls = s.get("asset_class") or _classify(s.get("instruments") or [])
    status = (s.get("status") or "").lower()
    return cls in ("options", "stock", "unknown") and status in ("active", "draft", "paused")


# Jaceford12's actual strategies as of 2026-06-04
JACE_STRATEGIES = [
    {"name": "ICT 2022 Model (AMD)",            "status": "ACTIVE", "instruments": ["ES", "NQ"]},
    {"name": "IOFED Precision Entry",           "status": "ACTIVE", "instruments": ["ES", "NQ"]},
    {"name": "Power of 3 (PO3)",                "status": "ACTIVE", "instruments": ["ES", "NQ", "YM"]},
    {"name": "Theta Scanner",                   "status": "ACTIVE", "instruments": []},
    {"name": "Reversal Swing",                  "status": "DRAFT",  "instruments": ["ES", "NQ"]},
    {"name": "52-Week High Breakout",           "status": "PAUSED", "instruments": []},
    {"name": "AMD Strategy",                    "status": "PAUSED", "instruments": ["ES", "NQ"]},
    {"name": "Breakout (Options)",              "status": "PAUSED", "instruments": ["SPY", "QQQ", "NVDA", "TSLA", "AMD"]},
    {"name": "Earnings/Catalyst (Options)",     "status": "PAUSED", "instruments": ["NVDA", "TSLA", "AAPL", "META", "AMZN", "GOOGL"]},
    {"name": "Futures Signal Scanner (ICT)",    "status": "PAUSED", "instruments": ["ES", "NQ", "RTY", "YM"]},
    {"name": "FVG Inversion Tap",               "status": "PAUSED", "instruments": ["ES", "NQ"]},
    {"name": "ICT Silver Bullet",               "status": "PAUSED", "instruments": ["ES", "NQ"]},
    {"name": "Judas Swing",                     "status": "PAUSED", "instruments": ["ES", "NQ", "YM"]},
    {"name": "Liquidity Sweep + FVG",           "status": "PAUSED", "instruments": ["ES", "NQ"]},
    {"name": "London Sweep into NY",            "status": "PAUSED", "instruments": ["ES", "NQ", "YM"]},
    {"name": "Low-Float Squeeze",               "status": "PAUSED", "instruments": []},
    {"name": "Momentum Gappers",                "status": "PAUSED", "instruments": []},
    {"name": "NY PM Reversal",                  "status": "PAUSED", "instruments": ["ES", "NQ", "YM"]},
    {"name": "Oracle - 5-Minute Opening Candle", "status": "PAUSED", "instruments": []},
    {"name": "Pre-Market Gap Runner",           "status": "PAUSED", "instruments": []},
    {"name": "Reversal Swing",                  "status": "PAUSED", "instruments": ["ES", "NQ"]},
    {"name": "SMT Divergence Reversal",         "status": "PAUSED", "instruments": ["ES", "NQ"]},
    {"name": "The Wheel (Options)",             "status": "PAUSED", "instruments": ["SPY", "AAPL", "MSFT", "JPM", "KO"]},
    {"name": "Trend Pullback (Options)",        "status": "PAUSED", "instruments": ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]},
    {"name": "Vertical Spread (Options)",       "status": "PAUSED", "instruments": ["SPY", "QQQ", "NVDA", "AAPL"]},
]


def test_jace_sees_all_options_and_template_strategies():
    visible = [s["name"] for s in JACE_STRATEGIES if _opt_strat_filter(s)]
    # The 5 options-classified setups
    assert "Breakout (Options)" in visible
    assert "Earnings/Catalyst (Options)" in visible
    assert "The Wheel (Options)" in visible
    assert "Trend Pullback (Options)" in visible
    assert "Vertical Spread (Options)" in visible
    # The 5 empty-instrument stock templates (the BUG C fix unblocks these)
    assert "52-Week High Breakout" in visible, "template excluded - BUG C regression"
    assert "Low-Float Squeeze" in visible
    assert "Momentum Gappers" in visible
    assert "Oracle - 5-Minute Opening Candle" in visible
    assert "Pre-Market Gap Runner" in visible
    # The Theta Scanner (active, empty) should be visible too
    assert "Theta Scanner" in visible

    # Futures-classified setups must NOT show up
    assert "ICT 2022 Model (AMD)" not in visible
    assert "Futures Signal Scanner (ICT)" not in visible
    assert "Reversal Swing" not in visible

    assert len(visible) >= 11, (
        f"BUG C REGRESSION: jace should see at least 11 strategies in the "
        f"Options Paper dropdown; got {len(visible)}: {visible}"
    )

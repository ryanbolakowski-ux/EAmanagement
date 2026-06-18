"""Regression tests for the two paper-trading bugs:
 1. Open-position unrealized P&L used wrong tick math for micros (MNQ showed -$2,400 instead of -$96).
 2. (persistence high-water mark fix is logic-verified; see runner._save_new_trades.)"""
from datetime import datetime
from app.engines.paper_trading import runner as R

class _Pos:
    def __init__(s, inst, d, e, c, sl, tp):
        s.instrument=inst; s.direction=d; s.entry_price=e; s.contracts=c
        s.stop_loss=sl; s.take_profit=tp; s.entry_time=datetime.utcnow()
class _Trader:
    def __init__(s, pos, last): s._position=pos; s._last_price=last

def test_mnq_open_pnl_uses_correct_tick_math():
    R._active_traders.clear()
    R._active_traders["sess:MNQ"]=_Trader(_Pos("MNQ","long",30689.75,1,30639.75,30796.88), 30641.75)
    out=R.get_open_positions(); R._active_traders.clear()
    assert len(out)==1
    # correct MNQ: (30641.75-30689.75)/0.25 * 0.50 * 1 = -96.0  (the bug gave -2400)
    assert abs(out[0]["unrealized_pnl"] - (-96.0)) < 0.01, out[0]["unrealized_pnl"]

def test_es_open_pnl_unchanged():
    R._active_traders.clear()
    R._active_traders["sess:ES"]=_Trader(_Pos("ES","long",6000.0,1,5990.0,6020.0), 5995.0)
    out=R.get_open_positions(); R._active_traders.clear()
    assert abs(out[0]["unrealized_pnl"] - (-250.0)) < 0.01, out[0]["unrealized_pnl"]

def test_micros_all_have_tick_tables():
    from app.engines.paper_trading.paper_trader import TICK_SIZES, TICK_VALUES
    for m in ("MNQ","MES","M2K","MYM"):
        assert m in TICK_SIZES and m in TICK_VALUES

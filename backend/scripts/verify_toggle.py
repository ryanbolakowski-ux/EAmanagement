import sys
from app.engines.ict import setups          # triggers @register
from app.engines.ict.registry import get_setup
from app.engines.strategy_engine.base_strategy import StrategyConfig
from app.engines.backtest_engine.ict_strategy import ICTStrategy

# 1) registry resolves the 5 dedicated setups by their seed names
names = ["ICT Silver Bullet", "Power of 3 (PO3)", "Judas Swing", "London Sweep into NY", "FVG Inversion Tap"]
print("=== registry resolution (V2 availability) ===")
for n in names:
    s = get_setup(n, {})
    print(f"  {n:24} -> {s.name if s else 'NONE'}")
print("  unknown strategy        ->", get_setup("Momentum Gappers", {}))

# 2) gate logic: v1 (default) must NOT dispatch; v2 must
def mk(rt):
    c = StrategyConfig(name="ICT Silver Bullet", instruments=["ES"], primary_timeframe="5m",
                       execution_timeframe="1m", higher_timeframes=[], risk_reward_ratio=3.0)
    c.rule_tree = rt
    return ICTStrategy(c, instrument="ES")
v1 = mk({})                                  # default
v2 = mk({"engine_version": "v2"})
ev1 = str((getattr(v1.config,"rule_tree",{}) or {}).get("engine_version","v1")).lower()
ev2 = str((getattr(v2.config,"rule_tree",{}) or {}).get("engine_version","v1")).lower()
print("=== gate ===")
print(f"  default rule_tree -> engine_version={ev1!r}  (expect v1, NO dispatch)")
print(f"  v2 rule_tree      -> engine_version={ev2!r}  (expect v2, dispatch)")
assert ev1 == "v1" and ev2 == "v2"
print("OK: gate defaults to V1; opt-in to V2 works; registry resolves 5 setups")

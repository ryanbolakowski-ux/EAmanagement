"""SLTP-OVERRIDES-V1 gate: rule_tree-gated SL/TP recalibration knobs.

Three OPTIONAL rule_tree keys on ICTStrategy (read once at construction):
  - sl_buffer_ticks (int, default 2, clamp 0-50): buffer past the sweep/swing
    extreme in BOTH structure stop branches (was hardcoded 2 ticks).
  - sl_widen_mult (float, default 1.0, clamp 0.5-4.0): multiplies the FINAL
    stop distance after branch selection, BEFORE the max-risk caps (caps
    still bind); the 12-tick fallback stop is multiplied too.
  - max_rr (float, default None -> class MAX_RR 3.0, clamp 1.0-10.0):
    overrides the take-profit R:R clamp.

PARITY PROOF: tests/fixtures/sltp_parity_baseline.json was captured by
running THIS file with --capture-baseline against the PRE-OVERRIDE engine
(worktree commit 51fd433, before ict_strategy.py gained the override code).
Every scenario stores float.hex() of the computed stop_loss/take_profit plus
the branch that chose it. test_parity_absent_keys() re-runs the identical
scenarios on the CURRENT engine with configs that DO NOT carry the new keys
(empty rule_tree, None rule_tree, and a rule_tree holding only pre-existing
keys) and requires bit-for-bit identical hex floats and identical branches.
Absent keys => byte-identical behavior.

Direct-invoke (no pytest in the container):
    python tests/test_sltp_overrides.py                  # run all tests
    python tests/test_sltp_overrides.py --capture-baseline  # regen baseline
        (ONLY valid against a pre-override engine — documented above)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sltp_parity_baseline.json"

INSTRUMENT = "NQ"   # tick 0.25
TICK = 0.25
ENTRY = 20000.0

# rule_tree variants that must ALL be byte-identical to the baseline: the
# keys existing strategy rows actually use (prior recon), never our new ones.
PARITY_RULE_TREES = [
    {},          # default
    None,        # tolerant-of-None requirement
    {"order_flow": {"enabled": True}, "confluences": ["fvg"],
     "use_rsi_filter": False, "use_vwap_filter": False,
     "take_profit_mode": "auto", "disable_activity_gate": False,
     "v2_frontier": {"note": "existing-keys only"}},
]


# ---------------------------------------------------------------- fixtures
def _df(lows, highs):
    n = len(lows)
    idx = pd.date_range("2026-06-01 09:30", periods=n, freq="1min")
    lows = np.asarray(lows, dtype=float)
    highs = np.asarray(highs, dtype=float)
    return pd.DataFrame({
        "open": (lows + highs) / 2, "high": highs, "low": lows,
        "close": (lows + highs) / 2, "volume": np.full(n, 100.0),
    }, index=idx)


def swing_low_df():
    """20 bars, unique swing low 19985.0 at index 10 (global min)."""
    lows = [19990.0, 19991.0, 19990.5, 19992.0, 19991.5,
            19990.25, 19989.0, 19988.0, 19987.5, 19986.5,
            19985.0, 19986.0, 19987.0, 19988.5, 19989.5,
            19990.75, 19991.25, 19992.5, 19992.25, 19991.75]
    return _df(lows, [x + 2.0 for x in lows])


def swing_high_df():
    """20 bars, unique swing high 20015.0 at index 10 (global max)."""
    highs = [20010.0, 20009.0, 20009.5, 20008.0, 20008.5,
             20009.75, 20011.0, 20012.0, 20012.5, 20013.5,
             20015.0, 20014.0, 20013.0, 20011.5, 20010.5,
             20009.25, 20008.75, 20007.5, 20007.75, 20008.25]
    return _df([x - 2.0 for x in highs], highs)


def no_swing_df():
    """20 bars, strictly monotonic lows AND highs -> no swing points."""
    lows = [19990.0 + 0.25 * i for i in range(20)]
    return _df(lows, [x + 1.0 for x in lows])


def tp_spike_df(spike, direction="long"):
    """30 flat bars with one unique high (long) / low (short) spike at idx 15.
    All other extremes stay on the entry side, so the spike is the only
    swing past entry that _compute_take_profit can target."""
    if direction == "long":
        highs = [19998.0 + 0.05 * i for i in range(30)]
        highs[15] = spike
        return _df([h - 1.0 for h in highs], highs)
    lows = [20002.0 - 0.05 * i for i in range(30)]
    lows[15] = spike
    return _df(lows, [lo + 1.0 for lo in lows])


def make_strategy(rule_tree, **cfg_overrides):
    cfg = dict(name="sltp-fixture", instruments=[INSTRUMENT],
               primary_timeframe="15m", execution_timeframe="1m",
               risk_reward_ratio=2.0, stop_loss_type="structure")
    cfg.update(cfg_overrides)
    config = StrategyConfig(**cfg)
    config.rule_tree = rule_tree
    return ICTStrategy(config, instrument=INSTRUMENT)


# ------------------------------------------------------------- scenarios
def run_scenarios(rule_tree):
    """Every scenario returns (value, branch). Deterministic synthetic
    fixtures exercise the sweep branch, the swing branch, the 12-tick
    fallback, both caps, and the TP swing/RR/clamp branches."""
    out = {}

    def stop(sid, st, entry, direction, df, exec_df=None, sweep_level=None):
        sl = st._compute_stop_loss(entry, direction, df, exec_df=exec_df,
                                   sweep_level=sweep_level)
        out[sid] = (float(sl), st._last_stop_choice.get("branch"))

    def target(sid, st, entry, sl, direction, df):
        tp = st._compute_take_profit(entry, sl, direction, df, htf_df=None)
        out[sid] = (float(tp), st._last_tp_choice.get("branch"))

    st = make_strategy(rule_tree)
    # --- sweep branch (explicit sweep_level anchors the stop)
    stop("sweep_long", st, ENTRY, "long", None, sweep_level=19980.0)
    stop("sweep_short", st, ENTRY, "short", None, sweep_level=20020.0)
    # 400-tick sweep cap (=100.0 pts on NQ) binds: raw 19889.5 -> 19900.0
    stop("sweep_long_capped", st, ENTRY, "long", None, sweep_level=19890.0)
    # --- swing branch (extreme swing of last 20 bars, lookback=2)
    stop("swing_long", st, ENTRY, "long", swing_low_df())
    stop("swing_short", st, ENTRY, "short", swing_high_df())
    # --- 12-tick fallback (no swings in the window)
    stop("fallback_long", st, ENTRY, "long", no_swing_df())
    stop("fallback_short", st, ENTRY, "short", no_swing_df())
    # --- swing-branch cap via stop_loss_ticks (20 ticks = 5.0 pts)
    st_capped = make_strategy(rule_tree, stop_loss_type="ticks", stop_loss_ticks=20)
    stop("swing_long_ticks_capped", st_capped, ENTRY, "long", swing_low_df())
    # --- take-profit branches
    target("tp_rr_long", st, ENTRY, 19990.0, "long", None)          # 2R fallback
    target("tp_rr_short", st, ENTRY, 20010.0, "short", None)
    target("tp_swing_long", st, ENTRY, 19995.0, "long", tp_spike_df(20008.0))
    target("tp_swing_short", st, ENTRY, 20005.0, "short",
           tp_spike_df(19992.0, "short"))
    # swing at exactly 4R (risk 5.0, level 20020.0) -> old 3R clamp cuts to 20015
    target("tp_clamp_long", st, ENTRY, 19995.0, "long", tp_spike_df(20020.5))
    return out


def capture_baseline():
    data = {sid: {"value": v, "hex": float(v).hex(), "branch": b}
            for sid, (v, b) in run_scenarios({}).items()}
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({
        "_comment": "Captured from the PRE-OVERRIDE engine (worktree commit "
                    "51fd433) via `python tests/test_sltp_overrides.py "
                    "--capture-baseline`. Do NOT regenerate from an engine "
                    "that already contains SLTP-OVERRIDES-V1.",
        "scenarios": data,
    }, indent=2, sort_keys=True))
    print(f"baseline written: {FIXTURE} ({len(data)} scenarios)")


def load_baseline():
    return json.loads(FIXTURE.read_text())["scenarios"]


# ----------------------------------------------------------------- tests
def test_baseline_sanity():
    """The frozen baseline itself must match the hand-computed old math —
    guards against a fixture captured off the wrong fixtures/branch."""
    base = load_baseline()
    expect = {
        "sweep_long": (19980.0 - 2 * TICK, "sweep"),            # 19979.5
        "sweep_short": (20020.0 + 2 * TICK, "sweep"),           # 20020.5
        "sweep_long_capped": (ENTRY - 400 * TICK, "sweep"),     # 19900.0
        "swing_long": (19985.0 - 2 * TICK, "swing"),            # 19984.5
        "swing_short": (20015.0 + 2 * TICK, "swing"),           # 20015.5
        "fallback_long": (ENTRY - 12 * TICK, "ticks_fallback"), # 19997.0
        "fallback_short": (ENTRY + 12 * TICK, "ticks_fallback"),# 20003.0
        "swing_long_ticks_capped": (ENTRY - 20 * TICK, "swing"),# 19995.0
        "tp_rr_long": (ENTRY + 10.0 * 2.0, "rr"),               # 20020.0
        "tp_rr_short": (ENTRY - 10.0 * 2.0, "rr"),              # 19980.0
        "tp_swing_long": (20008.0 - 2 * TICK, "swing"),         # 20007.5
        "tp_swing_short": (19992.0 + 2 * TICK, "swing"),        # 19992.5
        "tp_clamp_long": (ENTRY + 5.0 * 3.0, "rr_cap"),         # 20015.0 (3R cap)
    }
    assert set(base) == set(expect), (set(base) ^ set(expect))
    for sid, (val, br) in expect.items():
        got = base[sid]
        assert got["value"] == val and got["hex"] == float(val).hex(), \
            f"{sid}: baseline {got['value']} != hand-computed {val}"
        assert got["branch"] == br, f"{sid}: branch {got['branch']} != {br}"


def test_parity_absent_keys():
    """(a) Configs WITHOUT the new keys reproduce the pre-override engine
    bit-for-bit (float.hex equality) on sweep + swing + fallback SL branches
    and swing/RR/clamped TP branches, for empty/None/existing-keys rule_trees."""
    base = load_baseline()
    for rt in PARITY_RULE_TREES:
        got = run_scenarios(rt)
        for sid, rec in base.items():
            val, branch = got[sid]
            assert float(val).hex() == rec["hex"], (
                f"PARITY BROKEN rt={rt!r} {sid}: {float(val).hex()} "
                f"({val}) != baseline {rec['hex']} ({rec['value']})")
            assert branch == rec["branch"], (
                f"PARITY BROKEN rt={rt!r} {sid}: branch {branch} != {rec['branch']}")


def test_sl_buffer_ticks():
    """(b) sl_buffer_ticks=6 moves sweep AND swing stops exactly 4 ticks
    further; the 12-tick fallback (bufferless) is untouched."""
    base = load_baseline()
    got = run_scenarios({"sl_buffer_ticks": 6})
    four_ticks = 4 * TICK
    for sid in ("sweep_long", "swing_long"):
        assert got[sid][0] == base[sid]["value"] - four_ticks, \
            f"{sid}: {got[sid][0]} != {base[sid]['value']} - {four_ticks}"
    for sid in ("sweep_short", "swing_short"):
        assert got[sid][0] == base[sid]["value"] + four_ticks, \
            f"{sid}: {got[sid][0]} != {base[sid]['value']} + {four_ticks}"
    for sid in ("fallback_long", "fallback_short"):
        assert got[sid][0] == base[sid]["value"], f"{sid} moved: {got[sid][0]}"


def test_sl_widen_mult():
    """(c) sl_widen_mult=1.5 scales the FINAL stop distance 1.5x pre-cap;
    both caps (400-tick sweep, stop_loss_ticks swing) still bind; the
    12-tick fallback is multiplied too."""
    base = load_baseline()
    got = run_scenarios({"sl_widen_mult": 1.5})
    # uncapped: distance scales exactly 1.5x
    for sid, sign in (("sweep_long", -1), ("swing_long", -1),
                      ("sweep_short", +1), ("swing_short", +1)):
        base_dist = abs(ENTRY - base[sid]["value"])
        expect = ENTRY + sign * base_dist * 1.5
        assert got[sid][0] == expect, f"{sid}: {got[sid][0]} != {expect}"
    # caps still enforced (both would exceed their cap after x1.5)
    assert got["sweep_long_capped"][0] == ENTRY - 400 * TICK  # 19900.0
    assert got["swing_long_ticks_capped"][0] == ENTRY - 20 * TICK  # 19995.0
    # fallback: 12 ticks * 1.5 = 4.5 pts
    assert got["fallback_long"][0] == ENTRY - 12 * TICK * 1.5   # 19995.5
    assert got["fallback_short"][0] == ENTRY + 12 * TICK * 1.5  # 20004.5


def test_max_rr():
    """(d) max_rr=4.5 lets the 4R structure target through untouched (old
    clamp cut it to 3R); absent key still clamps at 3.0 (baseline parity)."""
    base = load_baseline()
    assert base["tp_clamp_long"]["value"] == ENTRY + 5.0 * 3.0  # old 3R cut
    got = run_scenarios({"max_rr": 4.5})
    # spike 20020.5 -> swing target 20020.0 = exactly 4R on 5.0 risk
    assert got["tp_clamp_long"][0] == 20020.0, got["tp_clamp_long"]
    assert got["tp_clamp_long"][1] == "swing", "4R target should be unclamped"
    # other TP scenarios (< 3R) unaffected by a LOOSER clamp
    for sid in ("tp_rr_long", "tp_swing_long", "tp_swing_short"):
        assert got[sid][0] == base[sid]["value"], f"{sid} moved: {got[sid][0]}"


def test_invalid_values_clamp_and_warn():
    """(e) Out-of-range values clamp to [0,50]/[0.5,4.0]/[1.0,10.0] and warn
    via loguru; non-numeric values are ignored (defaults kept) and warn."""
    from loguru import logger
    msgs = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        base = load_baseline()
        # buffer 999 -> 50 ticks (12.5 pts)
        got = run_scenarios({"sl_buffer_ticks": 999})
        assert got["sweep_long"][0] == 19980.0 - 50 * TICK   # 19967.5
        # buffer -5 -> 0 ticks (stop AT the sweep level)
        got = run_scenarios({"sl_buffer_ticks": -5})
        assert got["sweep_long"][0] == 19980.0
        # mult 0.1 -> 0.5 (fallback 12 ticks * 0.5 = 1.5 pts)
        got = run_scenarios({"sl_widen_mult": 0.1})
        assert got["fallback_long"][0] == ENTRY - 12 * TICK * 0.5  # 19998.5
        # mult 9.0 -> 4.0
        got = run_scenarios({"sl_widen_mult": 9.0})
        assert got["fallback_long"][0] == ENTRY - 12 * TICK * 4.0  # 19988.0
        # max_rr 99 -> 10.0: 12R spike (20060.5, risk 5.0) clamps at 10R
        st = make_strategy({"max_rr": 99})
        tp = st._compute_take_profit(ENTRY, 19995.0, "long",
                                     tp_spike_df(20060.5), htf_df=None)
        assert tp == ENTRY + 5.0 * 10.0, tp                  # 20050.0
        # non-numeric -> override ignored, byte-identical to baseline
        got = run_scenarios({"sl_widen_mult": "abc", "sl_buffer_ticks": None,
                             "max_rr": float("nan")})
        for sid, rec in base.items():
            assert float(got[sid][0]).hex() == rec["hex"], \
                f"non-numeric override changed {sid}"
    finally:
        logger.remove(sink)
    clamp_warns = [m for m in msgs if "[SLTP-OVERRIDES]" in m]
    assert len(clamp_warns) >= 8, \
        f"expected >=8 loguru warnings, got {len(clamp_warns)}:\n" + "\n".join(msgs)


TESTS = [test_baseline_sanity, test_parity_absent_keys, test_sl_buffer_ticks,
         test_sl_widen_mult, test_max_rr, test_invalid_values_clamp_and_warn]

if __name__ == "__main__":
    if "--capture-baseline" in sys.argv:
        capture_baseline()
        sys.exit(0)
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)

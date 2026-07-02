"""Walk-forward optimization tests (oos_fraction plumbing).

Covers BOTH optimizer implementations with synthetic data:
  * app/engines/optimization_engine/opt_worker.py — the REAL path (the route's
    ProcessPoolExecutor runs run_combo),
  * app/engines/optimization_engine/optimizer.py — the standalone grid search.

Proves the contract points:
  * oos_fraction=0 is EXACT V1 behavior (single full-window run, identical
    result shape — no train_/oos_/wf_ keys, no extra ranked-entry keys),
  * oos_fraction=0.3 ranks by the metric computed on the OOS holdout (a combo
    that wins in-sample but loses out-of-sample must NOT rank first),
  * split boundaries are correct (train head + held-out tail partition the
    window at start + (1 - oos_fraction) * span, train end backed off by
    TRAIN_END_EPSILON so the split-boundary bar lands ONLY in the holdout).

Two layers:
  * The `_fake_runner` tests replace BacktestRunner with a deterministic fake
    (synthetic metric surface as a function of the window + combo) to pin the
    split/rank plumbing without strategy behavior.
  * The "real engine" tests at the bottom run the REAL BacktestRunner +
    DataHandler + ICTStrategy — regression coverage for the destructive
    filter_date_range interaction (a shared handler filtered to the train
    window used to leave the OOS pass, and every later combo in the same
    worker process, with at most the single split-boundary bar).

No DB, no network.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.engines.optimization_engine import opt_worker
from app.engines.optimization_engine.opt_worker import (
    TRAIN_END_EPSILON,
    split_walkforward,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 11, tzinfo=timezone.utc)   # 10-day span

# Exact top-level key set of a V1 run_combo result (parity contract).
V1_KEYS = {"params", "net_profit", "profit_factor", "win_rate",
           "effective_win_rate", "max_drawdown", "total_trades", "sharpe_ratio"}


def _metrics(pf: float) -> SimpleNamespace:
    """Synthetic BacktestMetricsResult carrying only what the code reads."""
    return SimpleNamespace(
        net_profit=pf * 100.0, profit_factor=pf, win_rate=50.0,
        effective_win_rate=50.0, max_drawdown_pct=5.0, total_trades=10,
        sharpe_ratio=1.0,
    )


class _FakeDH:
    """Stands in for the per-process DataHandler. run_combo takes a cheap
    per-window `unfiltered_copy()` in walk-forward mode (the real handler's
    filter is destructive), so the stub must be copyable too. The fake
    runner never reads bars from it."""

    def unfiltered_copy(self):
        return _FakeDH()


class _FakeRunner:
    """Deterministic backtest: profit factor depends on (rr, window kind).
    rr=1.0 looks GREAT in-sample but falls apart out-of-sample; rr=2.0 is
    mediocre in-sample but holds up on the holdout — the classic overfit
    shape walk-forward exists to catch."""
    SURFACE = {
        # (rr, window): profit_factor
        (1.0, "train"): 10.0, (1.0, "oos"): 0.5, (1.0, "full"): 10.0,
        (2.0, "train"): 3.0,  (2.0, "oos"): 4.0, (2.0, "full"): 3.0,
    }
    calls: list = []   # (rr, window, start, end) per .run()

    def __init__(self, strategy, data_handler, bt_config):
        self.rr = float(getattr(getattr(strategy, "config", strategy), "risk_reward_ratio", 0.0))
        self.bt = bt_config

    def _window(self, split):
        if self.bt.start_date == START and self.bt.end_date == END:
            return "full"
        # Train windows end TRAIN_END_EPSILON before the split (the split
        # bar belongs only to the holdout); OOS windows start AT the split.
        return "train" if self.bt.end_date < split else "oos"

    def run(self):
        split = split_walkforward(START, END, 0.3)
        window = self._window(split)
        _FakeRunner.calls.append((self.rr, window, self.bt.start_date, self.bt.end_date))
        return _metrics(self.SURFACE[(self.rr, window)])


@pytest.fixture()
def _fake_runner(monkeypatch):
    """Patch BOTH import sites: run_combo does a call-time from-import off the
    backtest_runner module; optimizer.py bound the name at module import.
    The fake never touches the DataHandler — the real-engine tests below are
    the ones that exercise the BacktestRunner/DataHandler interaction."""
    _FakeRunner.calls = []
    import app.engines.backtest_engine.backtest_runner as btr
    import app.engines.optimization_engine.optimizer as optzr
    monkeypatch.setattr(btr, "BacktestRunner", _FakeRunner)
    monkeypatch.setattr(optzr, "BacktestRunner", _FakeRunner)
    monkeypatch.setitem(opt_worker._WORKER, "dh", _FakeDH())
    monkeypatch.setitem(opt_worker._WORKER, "instrument", "ES")
    yield


def _strat(rr: float = 2.0) -> dict:
    return {"name": "wf-test", "instruments": ["ES"], "primary_timeframe": "15m",
            "execution_timeframe": "1m", "higher_timeframes": ["1H"],
            "risk_reward_ratio": rr, "stop_loss_type": "ticks", "stop_loss_ticks": 8,
            "max_contracts": 1, "session_filters": [], "fvg_min_size_ticks": 4,
            "fvg_max_size_ticks": None, "max_daily_loss": None,
            "max_trades_per_day": None, "rule_tree": {}}


# ── split boundaries ─────────────────────────────────────────────────────

def test_split_boundaries_correct():
    # 10-day span, 30% holdout -> split at day 7 (start + 7 days).
    assert split_walkforward(START, END, 0.3) == START + timedelta(days=7)
    # 25% of an 8-day span -> split at day 6.
    s2, e2 = START, START + timedelta(days=8)
    assert split_walkforward(s2, e2, 0.25) == s2 + timedelta(days=6)
    # Degenerate 0 keeps the whole window as "train".
    assert split_walkforward(START, END, 0.0) == END


# ── opt_worker.run_combo (the REAL ProcessPool path) ─────────────────────

def test_run_combo_oos_zero_is_v1_parity(_fake_runner):
    idx, res = opt_worker.run_combo(0, {"risk_reward_ratio": 1.0}, _strat(),
                                    START, END, 0.0)
    assert idx == 0
    assert "_error" not in res, res.get("_tb")
    # EXACT V1 result shape — no walk-forward keys may leak in.
    assert set(res.keys()) == V1_KEYS
    # Exactly ONE backtest, over the FULL window.
    assert _FakeRunner.calls == [(1.0, "full", START, END)]
    assert res["profit_factor"] == 10.0


def test_run_combo_default_arg_matches_v1(_fake_runner):
    """Callers that don't pass oos_fraction at all (V1 call shape) get V1."""
    idx, res = opt_worker.run_combo(3, {"risk_reward_ratio": 2.0}, _strat(),
                                    START, END)
    assert "_error" not in res, res.get("_tb")
    assert set(res.keys()) == V1_KEYS
    assert [c[1] for c in _FakeRunner.calls] == ["full"]


def test_run_combo_walkforward_splits_and_reports_both_sets(_fake_runner):
    idx, res = opt_worker.run_combo(1, {"risk_reward_ratio": 1.0}, _strat(),
                                    START, END, 0.3)
    assert "_error" not in res, res.get("_tb")
    split = START + timedelta(days=7)
    # Two runs: train head then oos tail. The train end backs off one
    # epsilon so the bar sitting exactly on the split is holdout-only
    # (filter_date_range is inclusive on both ends).
    assert _FakeRunner.calls == [
        (1.0, "train", START, split - TRAIN_END_EPSILON),
        (1.0, "oos", split, END),
    ]
    # Top-level metric = OOS (so the route's existing sort ranks by OOS).
    assert res["profit_factor"] == 0.5
    # Both prefixed metric sets present and correct.
    assert res["train_profit_factor"] == 10.0
    assert res["oos_profit_factor"] == 0.5
    assert res["wf_split"] == split.isoformat()
    assert res["oos_fraction"] == 0.3


def test_run_combo_walkforward_rank_order_flips(_fake_runner):
    """rr=1.0 dominates in-sample; rr=2.0 dominates out-of-sample. Ranking by
    the top-level metric (what the route sorts on) must pick rr=2.0 under
    walk-forward and rr=1.0 under V1."""
    results_v1 = {}
    results_wf = {}
    for rr in (1.0, 2.0):
        _, r = opt_worker.run_combo(0, {"risk_reward_ratio": rr}, _strat(), START, END, 0.0)
        results_v1[rr] = r["profit_factor"]
        _, r = opt_worker.run_combo(0, {"risk_reward_ratio": rr}, _strat(), START, END, 0.3)
        results_wf[rr] = r["profit_factor"]
    assert max(results_v1, key=results_v1.get) == 1.0   # in-sample winner
    assert max(results_wf, key=results_wf.get) == 2.0   # holdout winner


# ── optimizer.run_optimization (standalone grid search, same logic) ──────

def _run_optimizer(oos_fraction: float):
    from app.engines.strategy_engine.base_strategy import StrategyConfig
    from app.engines.backtest_engine.backtest_runner import BacktestConfig
    from app.engines.optimization_engine.optimizer import run_optimization

    class FakeStrategy:
        def __init__(self, config):
            self.config = config

    base = StrategyConfig(name="wf-test", instruments=["ES"])
    bt = BacktestConfig(instrument="ES", start_date=START, end_date=END,
                        primary_timeframe="15m", all_timeframes=["15m", "1m"])
    return run_optimization(
        base_config=base,
        parameter_grid={"risk_reward_ratio": [1.0, 2.0]},
        data_handler=_FakeDH(),
        backtest_config=bt,
        strategy_class=FakeStrategy,
        optimization_metric="profit_factor",
        top_n=10,
        oos_fraction=oos_fraction,
    )


def test_optimizer_oos_zero_is_v1_shape_and_rank(_fake_runner):
    ranked = _run_optimizer(0.0)
    assert [r["parameters"]["risk_reward_ratio"] for r in ranked] == [1.0, 2.0]
    # Exact V1 entry shape: no walk-forward keys.
    assert all(set(r.keys()) == {"rank", "parameters", "metrics"} for r in ranked)
    # One backtest per combo, full window only.
    assert [c[1] for c in _FakeRunner.calls] == ["full", "full"]


def test_optimizer_walkforward_ranks_by_oos_metric(_fake_runner):
    ranked = _run_optimizer(0.3)
    # rr=2.0 (oos pf 4.0) must outrank rr=1.0 (oos pf 0.5) despite losing
    # in-sample 10.0 vs 3.0.
    assert [r["parameters"]["risk_reward_ratio"] for r in ranked] == [2.0, 1.0]
    top = ranked[0]
    assert top["metrics"].profit_factor == 4.0            # ranked-on = OOS
    assert top["train_metrics"].profit_factor == 3.0
    assert top["oos_metrics"].profit_factor == 4.0
    # Both windows ran for both combos, split at the same boundary as
    # opt_worker (train end == split - epsilon, oos start == split, split
    # at start + 70% of span).
    split = START + timedelta(days=7)
    train_calls = [c for c in _FakeRunner.calls if c[1] == "train"]
    oos_calls = [c for c in _FakeRunner.calls if c[1] == "oos"]
    assert len(train_calls) == 2 and len(oos_calls) == 2
    assert all(c[2] == START and c[3] == split - TRAIN_END_EPSILON for c in train_calls)
    assert all(c[2] == split and c[3] == END for c in oos_calls)


# ── REAL engine: BacktestRunner + DataHandler, no fakes ──────────────────
# Regression for the destructive-filter bug: DataHandler.filter_date_range
# trims in place and only no-ops for an IDENTICAL range, so before the
# per-window handler copies, running train then OOS on the shared handler
# intersected the trims — the OOS window (and BOTH windows of every later
# combo in the same worker process) saw at most the single boundary bar,
# zeroing every OOS metric the ranking depends on.

def _real_dh(n_bars: int):
    """Real DataHandler over synthetic-but-plausible 1m OHLCV bars."""
    import numpy as np
    import pandas as pd
    from app.engines.backtest_engine.data_handler import DataHandler

    rng = np.random.default_rng(7)
    steps = rng.normal(0.0, 2.0, n_bars)
    close = 5000.0 + np.cumsum(steps)
    open_ = np.concatenate([[5000.0], close[:-1]])
    high = np.maximum(open_, close) + rng.uniform(0.25, 2.0, n_bars)
    low = np.minimum(open_, close) - rng.uniform(0.25, 2.0, n_bars)
    idx = pd.date_range("2026-01-05", periods=n_bars, freq="1min", tz="UTC")
    df = pd.DataFrame({"timestamp": idx, "open": open_, "high": high,
                       "low": low, "close": close,
                       "volume": np.full(n_bars, 1000.0)})
    dh = DataHandler(instrument="ES", base_timeframe="1m")
    dh.load_from_dataframe(df)
    dh.build_timeframes(["1m", "15m", "1H"])
    return dh


def _spy_on_filter(monkeypatch):
    """Wrap the REAL DataHandler.filter_date_range (still runs it) and record
    what each window is left holding after the trim."""
    from app.engines.backtest_engine.data_handler import DataHandler

    windows: list[dict] = []
    real_filter = DataHandler.filter_date_range

    def _recording_filter(self, f_start, f_end):
        real_filter(self, f_start, f_end)
        base = self._base_data
        windows.append({
            "start": f_start, "end": f_end, "bars": len(base),
            "first": base.index.min() if len(base) else None,
            "last": base.index.max() if len(base) else None,
        })

    monkeypatch.setattr(DataHandler, "filter_date_range", _recording_filter)
    return windows


def _utc_ts(dt):
    """Mirror BacktestRunner.run()'s start/end normalization."""
    import pandas as pd
    return pd.Timestamp(dt.replace(tzinfo=None)).tz_localize("UTC")


def test_run_combo_walkforward_real_datahandler_both_windows_see_bars(monkeypatch):
    """TWO consecutive walk-forward run_combo calls against ONE shared REAL
    DataHandler: every window of every combo must be backtested over its full
    bar range, the split-boundary bar must land ONLY in the holdout, and the
    shared per-process handler must stay pristine for the next combo."""
    n = 2881                       # 2880 one-minute steps -> split lands ON a bar
    dh = _real_dh(n)
    monkeypatch.setitem(opt_worker._WORKER, "dh", dh)
    monkeypatch.setitem(opt_worker._WORKER, "instrument", "ES")

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    end = start + timedelta(minutes=n - 1)
    split = split_walkforward(start, end, 0.25)
    assert split == start + timedelta(minutes=2160)   # sanity: split is a bar ts

    windows = _spy_on_filter(monkeypatch)

    for combo_idx, rr in enumerate((1.5, 2.5)):
        _, res = opt_worker.run_combo(combo_idx, {"risk_reward_ratio": rr},
                                      _strat(rr), start, end, 0.25)
        assert "_error" not in res, res.get("_tb")
        assert res["wf_split"] == split.isoformat()

    split_ts = _utc_ts(split)
    trains = [w for w in windows if w["start"] == _utc_ts(start)]
    ooses = [w for w in windows if w["start"] == split_ts]
    # BOTH combos ran BOTH windows against real data (4 distinct trims).
    assert len(trains) == 2 and len(ooses) == 2
    for w in trains:   # 75% head: every bar strictly before the split
        assert w["bars"] == 2160
        assert w["last"] < split_ts        # boundary bar NOT leaked into train
    for w in ooses:    # 25% tail: split bar onward — NOT a single leftover bar
        assert w["bars"] == 721
        assert w["first"] == split_ts      # boundary bar counted once, in OOS
    # The shared per-process handler is untouched for whatever runs next.
    assert len(dh._base_data) == n
    assert len(dh._resampled["1m"]) == n


def test_optimizer_walkforward_real_datahandler_both_windows_see_bars(monkeypatch):
    """Same regression for the standalone run_optimization path: both grid
    combos must see full train AND OOS windows off one shared caller-owned
    DataHandler, which stays pristine."""
    from app.engines.backtest_engine.backtest_runner import BacktestConfig
    from app.engines.backtest_engine.ict_strategy import ICTStrategy
    from app.engines.optimization_engine.optimizer import run_optimization
    from app.engines.strategy_engine.base_strategy import StrategyConfig

    n = 1441                       # 1440 one-minute steps -> split lands ON a bar
    dh = _real_dh(n)
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    end = start + timedelta(minutes=n - 1)
    split = split_walkforward(start, end, 0.25)
    assert split == start + timedelta(minutes=1080)

    windows = _spy_on_filter(monkeypatch)

    base = StrategyConfig(name="wf-real", instruments=["ES"],
                          primary_timeframe="15m", execution_timeframe="1m",
                          higher_timeframes=["1H"])
    bt = BacktestConfig(instrument="ES", start_date=start, end_date=end,
                        primary_timeframe="15m", all_timeframes=["15m", "1m", "1H"])
    ranked = run_optimization(
        base_config=base,
        parameter_grid={"risk_reward_ratio": [1.5, 2.5]},
        data_handler=dh,
        backtest_config=bt,
        strategy_class=ICTStrategy,
        optimization_metric="profit_factor",
        top_n=10,
        oos_fraction=0.25,
    )
    # No combo may be silently swallowed by the per-combo except.
    assert len(ranked) == 2
    assert all("train_metrics" in r and "oos_metrics" in r for r in ranked)

    split_ts = _utc_ts(split)
    trains = [w for w in windows if w["start"] == _utc_ts(start)]
    ooses = [w for w in windows if w["start"] == split_ts]
    assert len(trains) >= 1 and len(ooses) >= 1   # idempotent no-op after combo 1
    for w in trains:
        assert w["bars"] == 1080
        assert w["last"] < split_ts
    for w in ooses:
        assert w["bars"] == 361
        assert w["first"] == split_ts
    # Caller's handler untouched.
    assert len(dh._base_data) == n
    assert len(dh._resampled["1m"]) == n

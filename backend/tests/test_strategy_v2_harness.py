"""Tests for the strategy V2 harness + seed script (v2-redesign track).

Everything here runs WITHOUT the prod DB:
  * the harness is exercised end-to-end via --synthetic (deterministic bars
    through the REAL BacktestRunner), and
  * the seed script is exercised against a mocked async session.

Run: pytest tests/test_strategy_v2_harness.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import csv
import importlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

harness = importlib.import_module("scripts.strategy_v2_harness")
seed_mod = importlib.import_module("scripts.seed_strategy_v2")


# ─────────────────────────────────────────────────────────────────────────────
# Harness — synthetic end-to-end
# ─────────────────────────────────────────────────────────────────────────────

def _run_synthetic(tmp_path):
    rc = harness.main([
        "--synthetic", "--all",
        "--start", "2026-04-01", "--end", "2026-04-08",
        "--oos-fraction", "0.3",
        "--output-dir", str(tmp_path),
    ])
    assert rc == 0
    return tmp_path / harness.CSV_NAME, tmp_path / harness.MD_NAME


def test_synthetic_run_produces_both_legs_and_deltas(tmp_path):
    csv_path, md_path = _run_synthetic(tmp_path)
    assert csv_path.exists(), "CSV output missing"
    assert md_path.exists(), "markdown output missing"

    with open(csv_path) as fh:
        rows = list(csv.DictReader(fh))
    assert rows, "comparison table is empty"

    # Every synthetic book strategy gets full/in_sample/out_of_sample rows.
    strategies = {r["strategy"] for r in rows}
    assert len(strategies) == len(harness.SYNTHETIC_BOOK)
    for name in strategies:
        segs = {r["segment"] for r in rows if r["strategy"] == name}
        assert segs == {"full", "in_sample", "out_of_sample"}

    full_rows = [r for r in rows if r["segment"] == "full"]
    for r in full_rows:
        # Both legs actually traded…
        assert int(r["v1_trades"]) > 0, f"{r['strategy']}: V1 leg produced no trades"
        assert int(r["v2_trades"]) > 0, f"{r['strategy']}: V2 leg produced no trades"
        # …the deltas are populated…
        for k in ("delta_trades", "delta_wr_pct", "delta_net_usd"):
            assert r[k] != "", f"{r['strategy']}: {k} missing"
        # …and extra slippage can only cost money: V2 net strictly below V1.
        assert float(r["delta_net_usd"]) < 0, (
            f"{r['strategy']}: V2 (harsher slippage) net should be below V1 "
            f"(delta_net_usd={r['delta_net_usd']})")
        # Same bars + same signal logic -> identical trade sets across legs.
        assert int(r["v1_trades"]) == int(r["v2_trades"])

    md = md_path.read_text()
    assert "V1" in md and "V2" in md and "out_of_sample" in md


def test_synthetic_segments_partition_the_full_trade_set(tmp_path):
    csv_path, _ = _run_synthetic(tmp_path)
    with open(csv_path) as fh:
        rows = list(csv.DictReader(fh))
    by_key = {(r["strategy"], r["segment"]): r for r in rows}
    strategies = {r["strategy"] for r in rows}
    for name in strategies:
        full = by_key[(name, "full")]
        is_ = by_key[(name, "in_sample")]
        oos = by_key[(name, "out_of_sample")]
        for leg in ("v1", "v2"):
            assert (int(is_[f"{leg}_trades"]) + int(oos[f"{leg}_trades"])
                    == int(full[f"{leg}_trades"])), (
                f"{name}/{leg}: IS+OOS trade counts must partition the full set")
    # The split must actually hold trades out — at least one strategy trades OOS.
    assert any(int(by_key[(n, "out_of_sample")]["v1_trades"]) > 0 for n in strategies)


def test_synthetic_bars_are_deterministic():
    start, end = datetime(2026, 4, 1), datetime(2026, 4, 2)
    a = harness.generate_synthetic_bars(start, end, seed=42)
    b = harness.generate_synthetic_bars(start, end, seed=42)
    assert a.equals(b), "same seed must produce byte-identical bars"
    c = harness.generate_synthetic_bars(start, end, seed=43)
    assert not a["close"].equals(c["close"]), "different seed must move the tape"


# ─────────────────────────────────────────────────────────────────────────────
# OOS split boundaries
# ─────────────────────────────────────────────────────────────────────────────

def test_oos_split_boundaries():
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 11)
    sp = harness.compute_oos_split(start, end, 0.3)
    # Window edges preserved…
    assert sp.is_start == start
    assert sp.oos_end == end
    # …the cut sits exactly 30% back from the end, and IS/OOS share it.
    assert sp.is_end == sp.oos_start == datetime(2026, 1, 8)


def test_oos_split_zero_fraction_means_no_holdout():
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 11)
    sp = harness.compute_oos_split(start, end, 0.0)
    assert sp.oos_start == end
    assert sp.is_end == end


def test_oos_split_rejects_bad_inputs():
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 11)
    with pytest.raises(ValueError):
        harness.compute_oos_split(start, end, 1.0)   # nothing in-sample
    with pytest.raises(ValueError):
        harness.compute_oos_split(start, end, -0.1)  # negative fraction
    with pytest.raises(ValueError):
        harness.compute_oos_split(end, start, 0.3)   # inverted window


# ─────────────────────────────────────────────────────────────────────────────
# Seed script — plan / dry-run with a mocked session (no real DB)
# ─────────────────────────────────────────────────────────────────────────────

def _fake_strategy(name, status, instruments, user="user-1", sid=None):
    return SimpleNamespace(
        id=sid or f"id-{name}", user_id=user, name=name, status=status,
        instruments=instruments, rule_tree={}, description=None,
        primary_timeframe="15m", execution_timeframe="1m", higher_timeframes=["4H"],
        risk_reward_ratio=3.0, stop_loss_type="structure", stop_loss_ticks=None,
        breakeven_at_r=1.0, breakeven_mode="structure", max_contracts=10,
        session_filters=["LONDON", "NY_AM"], fvg_min_size_ticks=4,
        fvg_max_size_ticks=None, max_daily_loss=None, max_trades_per_day=5,
        kill_switch_enabled=True, cooldown_min=5, max_open_positions=1,
        starred=False,
    )


def _mock_session(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


def _book():
    from app.models.strategy import StrategyStatus
    return [
        _fake_strategy("AMD Strategy", StrategyStatus.ACTIVE, ["ES", "NQ"]),
        _fake_strategy("London Sweep into NY", StrategyStatus.ACTIVE, ["ES", "NQ", "YM"]),
        _fake_strategy("The Wheel (Options)", StrategyStatus.ACTIVE, ["SPY"]),
        _fake_strategy("Paused Futures", StrategyStatus.PAUSED, ["ES"]),
        _fake_strategy("Cloned Already", StrategyStatus.ACTIVE, ["ES"]),
        _fake_strategy("Cloned Already V2", StrategyStatus.DRAFT, ["ES"]),
    ]


def test_seed_dry_run_plan_with_mocked_session():
    from app.models.strategy import StrategyStatus
    rows = _book()
    session = _mock_session(rows)

    plan = asyncio.run(seed_mod.seed(session, execute=False))

    by_name = {p["source_name"]: p for p in plan}
    assert len(plan) == len(rows)  # one plan entry per row

    # Active futures rows get cloned to "<name> V2"…
    assert by_name["AMD Strategy"]["action"] == "create"
    assert by_name["AMD Strategy"]["new_name"] == "AMD Strategy V2"
    assert by_name["London Sweep into NY"]["action"] == "create"
    assert by_name["London Sweep into NY"]["new_name"] == "London Sweep into NY V2"
    # …options rows, paused rows, existing clones and clones-of-clones don't.
    assert by_name["The Wheel (Options)"]["action"] == "skip"
    assert "futures" in by_name["The Wheel (Options)"]["reason"]
    assert by_name["Paused Futures"]["action"] == "skip"
    assert by_name["Cloned Already"]["action"] == "skip"
    assert "already exists" in by_name["Cloned Already"]["reason"]
    assert by_name["Cloned Already V2"]["action"] == "skip"
    assert "already a V2 clone" in by_name["Cloned Already V2"]["reason"]

    # DRY-RUN wrote NOTHING and mutated NOTHING.
    session.add.assert_not_called()
    session.commit.assert_not_called()
    assert rows[0].name == "AMD Strategy"
    assert rows[0].status == StrategyStatus.ACTIVE
    assert rows[0].rule_tree == {}


def test_seed_execute_refuses_without_confirm_env(monkeypatch):
    monkeypatch.delenv(seed_mod.CONFIRM_ENV, raising=False)
    session = _mock_session(_book())
    with pytest.raises(RuntimeError, match=seed_mod.CONFIRM_ENV):
        asyncio.run(seed_mod.seed(session, execute=True))
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_seed_execute_writes_only_with_confirm_env(monkeypatch):
    monkeypatch.setenv(seed_mod.CONFIRM_ENV, "YES")
    import app.models  # noqa: F401 — full registry so Strategy() can instantiate
    from app.models.strategy import StrategyStatus
    rows = _book()
    session = _mock_session(rows)

    plan = asyncio.run(seed_mod.seed(session, execute=True))

    creates = [p for p in plan if p["action"] == "create"]
    assert len(creates) == 2
    assert session.add.call_count == 2
    session.commit.assert_awaited_once()
    # The written rows are DRAFT V2 clones with the v2_variant annotation;
    # source rows are untouched.
    for call in session.add.call_args_list:
        clone = call.args[0]
        assert clone.name.endswith(" V2")
        assert clone.status == StrategyStatus.DRAFT
        assert clone.rule_tree["v2_variant"]["seeded_by"] == "scripts.seed_strategy_v2"
    assert rows[0].status == StrategyStatus.ACTIVE
    assert rows[0].rule_tree == {}  # deep copy — annotation never leaks back


def test_clone_strategy_row_annotates_and_drafts():
    import app.models  # noqa: F401 — full registry so Strategy() can instantiate
    from app.models.strategy import StrategyStatus
    src = _fake_strategy("AMD Strategy", StrategyStatus.ACTIVE, ["ES", "NQ"],
                         sid="e0e35685-a5ea-4d37-ae8b-34b7f5e7f98a")
    clone = seed_mod.clone_strategy_row(src)
    assert clone.name == "AMD Strategy V2"
    assert clone.status == StrategyStatus.DRAFT
    assert clone.rule_tree["v2_variant"]["source_strategy_id"] == str(src.id)
    assert clone.rule_tree["v2_variant"]["source_name"] == "AMD Strategy"
    assert clone.instruments == ["ES", "NQ"]
    assert clone.max_contracts == 10
    assert clone.breakeven_mode == "structure"
    # Source rule_tree untouched (deep copy).
    assert src.rule_tree == {}
    assert src.name == "AMD Strategy"

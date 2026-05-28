"""Bug #2: optimization fails silently (logger NameError), GET /{id} 405,
no failure_reason surfaced, no pre-queue validation.

Proves: GET /{id} exists (404 not 405), invalid grids are rejected 400 with a
message, and a real small run reaches a terminal state (completed, or failed
WITH a visible failure_reason) instead of hanging silently.
"""
import time
import uuid
from datetime import datetime, timedelta, timezone
import pytest


def _make_active_strategy(client):
    r = client.post("/api/v1/strategies/", json={
        "name": f"pytest-opt-{uuid.uuid4().hex[:6]}",
        "instruments": ["ES"], "primary_timeframe": "15m", "execution_timeframe": "1m",
        "risk_reward_ratio": 2.0, "stop_loss_type": "ticks", "stop_loss_ticks": 10,
        "fvg_min_size_ticks": 4, "status": "active",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_get_run_is_not_405(client):
    """A random run id must 404 (route exists) — NOT 405 (route missing)."""
    rid = str(uuid.uuid4())
    r = client.get(f"/api/v1/optimization/{rid}")
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"


def test_invalid_grid_empty_rejected(client):
    sid = _make_active_strategy(client)
    try:
        now = datetime.now(timezone.utc)
        r = client.post("/api/v1/optimization/", json={
            "strategy_id": sid, "instrument": "ES",
            "optimization_metric": "profit_factor",
            "start_date": (now - timedelta(days=10)).isoformat(),
            "end_date": now.isoformat(),
            "parameter_grid": {},
        })
        assert r.status_code == 400, f"empty grid should 400, got {r.status_code}: {r.text}"
    finally:
        client.delete(f"/api/v1/strategies/{sid}")


def test_unknown_param_rejected(client):
    sid = _make_active_strategy(client)
    try:
        now = datetime.now(timezone.utc)
        r = client.post("/api/v1/optimization/", json={
            "strategy_id": sid, "instrument": "ES",
            "optimization_metric": "profit_factor",
            "start_date": (now - timedelta(days=10)).isoformat(),
            "end_date": now.isoformat(),
            "parameter_grid": {"totally_made_up_param": [1, 2, 3]},
        })
        assert r.status_code == 400, f"unknown param should 400, got {r.status_code}: {r.text}"
        assert "Unknown optimization parameter" in r.text
    finally:
        client.delete(f"/api/v1/strategies/{sid}")


def test_known_params_accepted_and_terminal(client):
    """Submit the exact param names from the bug report with a tiny 2-combo
    grid + short range. Must NOT fail with a NameError; must reach a terminal
    state and, if failed, expose failure_reason."""
    sid = _make_active_strategy(client)
    try:
        now = datetime.now(timezone.utc)
        r = client.post("/api/v1/optimization/", json={
            "strategy_id": sid, "instrument": "ES",
            "optimization_metric": "profit_factor",
            "start_date": (now - timedelta(days=5)).isoformat(),
            "end_date": now.isoformat(),
            # 1 combo per dimension on two dims = 2 combos: bounded + fast
            "parameter_grid": {
                "risk_reward_ratio": [2.0],
                "stop_loss_ticks": [10, 12],
            },
        })
        assert r.status_code == 202, f"valid grid should 202, got {r.status_code}: {r.text}"
        run_id = r.json()["id"]
        assert r.json()["status"] in ("queued", "running")

        # Poll the now-existing GET /{id} until terminal (cap ~150s)
        terminal = None
        deadline = time.time() + 150
        last = None
        while time.time() < deadline:
            g = client.get(f"/api/v1/optimization/{run_id}")
            assert g.status_code == 200, g.text
            last = g.json()
            if last["status"] in ("completed", "failed"):
                terminal = last
                break
            time.sleep(3)

        assert terminal is not None, f"run did not finish in time; last={last}"
        # The whole point of the logger fix: it should NOT fail with NameError.
        if terminal["status"] == "failed":
            assert terminal.get("failure_reason"), "failed run must expose a failure_reason"
            assert "name logger is not defined" not in (terminal["failure_reason"] or ""), \
                "the logger NameError must be fixed"
        else:
            assert terminal["status"] == "completed"
            assert terminal["completed_combinations"] >= 1
    finally:
        client.delete(f"/api/v1/strategies/{sid}")

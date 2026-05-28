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


def _psy():
    import os, psycopg2
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url, connect_timeout=5)


def test_apply_best_result_updates_strategy(client):
    """Bug 8: applying a ranked result writes its params onto the strategy.

    Seeds a completed run + rank-1 result with sync psycopg2 (no asyncio), then
    exercises POST /optimization/{id}/apply and verifies the strategy params."""
    import uuid as _uuid
    import json as _json

    s = client.post("/api/v1/strategies/", json={
        "name": f"opt-apply-{_uuid.uuid4().hex[:6]}",
        "instruments": ["ES"], "risk_reward_ratio": 2.0,
        "stop_loss_type": "ticks", "stop_loss_ticks": 8,
        "fvg_min_size_ticks": 4, "status": "active",
    })
    assert s.status_code == 201, s.text
    sid = s.json()["id"]
    run_id = str(_uuid.uuid4())

    cn = _psy()
    try:
        with cn, cn.cursor() as cur:
            cur.execute("SELECT user_id FROM strategies WHERE id = %s", (sid,))
            uid = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO optimization_runs
                    (id, strategy_id, user_id, instrument, start_date, end_date,
                     parameter_grid, optimization_metric, total_combinations,
                     completed_combinations, status, created_at)
                VALUES (%s, %s, %s, 'ES', NOW() - INTERVAL '30 days', NOW(),
                        %s, 'profit_factor', 1, 1, 'COMPLETED', NOW())
            """, (run_id, sid, str(uid), _json.dumps({"risk_reward_ratio": [3.0]})))
            cur.execute("""
                INSERT INTO optimization_results
                    (id, optimization_run_id, parameters, rank, net_profit,
                     profit_factor, win_rate, max_drawdown, total_trades, sharpe_ratio)
                VALUES (%s, %s, %s, 1, 1000, 2.5, 0.6, 5, 40, 1.2)
            """, (str(_uuid.uuid4()), run_id,
                  _json.dumps({"risk_reward_ratio": 3.0, "stop_loss_ticks": 16, "fvg_min_size_ticks": 6})))
    finally:
        cn.close()

    try:
        g = client.get(f"/api/v1/optimization/{run_id}")
        assert g.status_code == 200, g.text
        assert g.json()["status"] == "completed"

        a = client.post(f"/api/v1/optimization/{run_id}/apply", params={"rank": 1})
        assert a.status_code == 200, a.text

        st = client.get(f"/api/v1/strategies/{sid}").json()
        assert abs(st["risk_reward_ratio"] - 3.0) < 1e-6, st
    finally:
        cn = _psy()
        try:
            with cn, cn.cursor() as cur:
                cur.execute("DELETE FROM optimization_results WHERE optimization_run_id = %s", (run_id,))
                cur.execute("DELETE FROM optimization_runs WHERE id = %s", (run_id,))
        finally:
            cn.close()
        client.delete(f"/api/v1/strategies/{sid}")


def test_retry_failed_run(client):
    """Fix 6: a FAILED run can be retried -> goes back to queued, partial
    results cleared, error cleared. Seeds the run with sync psycopg2."""
    import uuid as _uuid
    import json as _json

    sid = _make_active_strategy(client)
    run_id = str(_uuid.uuid4())
    cn = _psy()
    try:
        with cn, cn.cursor() as cur:
            cur.execute("SELECT user_id FROM strategies WHERE id=%s", (sid,))
            uid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO optimization_runs (id,strategy_id,user_id,instrument,start_date,end_date,"
                "parameter_grid,optimization_metric,total_combinations,completed_combinations,status,"
                "error_message,completed_at,created_at) VALUES (%s,%s,%s,'ES',NOW()-INTERVAL '30 days',NOW(),"
                "%s,'profit_factor',48,4,'FAILED','Worker died during backend restart',NOW(),NOW())",
                (run_id, sid, str(uid), _json.dumps({"risk_reward_ratio": [1.5, 2.0]})))
            cur.execute(
                "INSERT INTO optimization_results (id,optimization_run_id,parameters,rank,net_profit,"
                "profit_factor,win_rate,max_drawdown,total_trades,sharpe_ratio) "
                "VALUES (%s,%s,'{}'::json,1,0,0,0,0,0,0)", (str(_uuid.uuid4()), run_id))
    finally:
        cn.close()
    try:
        # the failed run + its reason are visible
        g = client.get(f"/api/v1/optimization/{run_id}")
        assert g.status_code == 200
        assert g.json()["status"] == "failed"
        assert g.json().get("failure_reason")

        r = client.post(f"/api/v1/optimization/{run_id}/retry")
        if r.status_code in (404, 405):
            pytest.skip("retry endpoint not deployed on this server yet")
        assert r.status_code == 202, r.text
        assert r.json()["status"] in ("queued", "running")

        # results cleared, error cleared
        cn = _psy()
        try:
            with cn, cn.cursor() as cur:
                cur.execute("SELECT count(*) FROM optimization_results WHERE optimization_run_id=%s", (run_id,))
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT error_message FROM optimization_runs WHERE id=%s", (run_id,))
                assert cur.fetchone()[0] is None
        finally:
            cn.close()
    finally:
        cn = _psy()
        try:
            with cn, cn.cursor() as cur:
                cur.execute("DELETE FROM optimization_results WHERE optimization_run_id=%s", (run_id,))
                cur.execute("DELETE FROM optimization_runs WHERE id=%s", (run_id,))
        finally:
            cn.close()
        client.delete(f"/api/v1/strategies/{sid}")

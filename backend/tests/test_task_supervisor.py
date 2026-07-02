"""Unit tests for app.core.task_supervisor.supervise.

Proves the three contract points:
  * a crashing coroutine is restarted (with the alert hook fired),
  * the supervisor gives up after max_restarts crashes inside the window
    (final alert, task ends instead of hot-looping),
  * TASK_SUPERVISOR_ENABLED=0 falls through to a plain create_task —
    the crash propagates exactly like the pre-supervisor behavior.

All async work runs through asyncio.run() inside sync tests (same pattern as
the other unit tests in this suite — no pytest-asyncio dependency)."""
import asyncio

import pytest

from app.core import task_supervisor
from app.core.task_supervisor import supervise


class _AlertSpy:
    """Stands in for send_pipeline_failure_alert (which is async and talks to
    Redis/Resend in prod — neither exists in the test container)."""
    def __init__(self):
        self.calls = []

    async def __call__(self, reason, context=None, *, traceback_str=None, **kw):
        self.calls.append({"reason": reason, "context": context or {}})
        return 0


@pytest.fixture()
def alert_spy(monkeypatch):
    spy = _AlertSpy()
    import app.engines.pipeline_alerts as pa
    monkeypatch.setattr(pa, "send_pipeline_failure_alert", spy)
    return spy


def test_restarts_on_crash_then_honors_clean_exit(monkeypatch, alert_spy):
    monkeypatch.setenv("TASK_SUPERVISOR_ENABLED", "1")
    runs = {"n": 0}

    async def flaky():
        runs["n"] += 1
        if runs["n"] < 3:
            raise RuntimeError(f"boom {runs['n']}")
        # third run exits cleanly — supervisor must NOT restart after that

    async def main():
        task = supervise(flaky, "flaky_loop", max_restarts=5,
                         window_seconds=60, base_backoff=0.01)
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(main())
    assert runs["n"] == 3                      # 2 crashes + 1 clean run
    assert len(alert_spy.calls) == 2           # one alert per crash
    assert all("flaky_loop" in c["reason"] for c in alert_spy.calls)
    assert all(c["context"].get("final") is False for c in alert_spy.calls)


def test_gives_up_after_max_restarts(monkeypatch, alert_spy):
    monkeypatch.setenv("TASK_SUPERVISOR_ENABLED", "1")
    runs = {"n": 0}

    async def always_crashes():
        runs["n"] += 1
        raise ValueError("permanent failure")

    async def main():
        task = supervise(always_crashes, "dead_loop", max_restarts=2,
                         window_seconds=60, base_backoff=0.01)
        # Must COMPLETE (stay dead), not raise and not loop forever.
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(main())
    # max_restarts=2 -> initial run + 2 restarts = 3 executions, then dead.
    assert runs["n"] == 3
    # Every crash alerted; the last one is flagged final.
    assert len(alert_spy.calls) == 3
    assert alert_spy.calls[-1]["context"].get("final") is True
    assert "DEAD" in alert_spy.calls[-1]["reason"]


def test_flag_off_is_plain_create_task(monkeypatch):
    monkeypatch.setenv("TASK_SUPERVISOR_ENABLED", "0")
    runs = {"n": 0}

    async def crashes_once():
        runs["n"] += 1
        raise RuntimeError("unsupervised boom")

    async def main():
        task = supervise(crashes_once, "plain_task", base_backoff=0.01)
        # Plain create_task: the exception must propagate to the awaiter,
        # exactly like the pre-supervisor bare-create_task behavior.
        with pytest.raises(RuntimeError, match="unsupervised boom"):
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(main())
    assert runs["n"] == 1                      # no restart when flag is off


def test_cancellation_propagates_without_restart(monkeypatch, alert_spy):
    monkeypatch.setenv("TASK_SUPERVISOR_ENABLED", "1")
    runs = {"n": 0}

    async def sleeper():
        runs["n"] += 1
        await asyncio.sleep(3600)

    async def main():
        task = supervise(sleeper, "sleepy_loop", base_backoff=0.01)
        await asyncio.sleep(0.05)              # let the child start
        task.cancel()                          # lifespan shutdown path
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert runs["n"] == 1                      # cancel is not a crash
    assert alert_spy.calls == []

"""Supervised background tasks for the lifespan loops.

WHY THIS EXISTS: every long-running loop in app.main's lifespan (daily data
fetch, digest, comp expiry, premarket scheduler, intraday refresher, broker
balance sync, ...) was started with a bare asyncio.create_task(). If one of
those coroutines ever escapes its own try/except and raises, the task dies
silently — asyncio only prints "Task exception was never retrieved" at
garbage-collection time, nobody gets an alert, and the loop stays dead until
the next deploy. That is exactly the failure mode behind past "the emails
just stopped" incidents.

supervise() wraps the coroutine factory in a watchdog that:
  * logs the crash via loguru,
  * fires send_pipeline_failure_alert (which has its own Redis dedup, so a
    crash-loop can't spam the admin inbox),
  * restarts the task with exponential backoff (base_backoff * 2^n),
  * gives up after max_restarts crashes inside window_seconds — a final
    alert is sent and the task stays dead rather than hot-looping forever.

Flag-gated: TASK_SUPERVISOR_ENABLED (default "1"). Set to "0" to fall back
to a plain asyncio.create_task(factory()) — byte-for-byte the pre-supervisor
behavior — so this can be disabled in prod without a code change.
"""
import asyncio
import os
import time
from typing import Awaitable, Callable

from loguru import logger


def supervise(
    factory: Callable[[], Awaitable],
    name: str,
    max_restarts: int = 5,
    window_seconds: float = 3600,
    base_backoff: float = 5,
) -> asyncio.Task:
    """Run `factory()` as a supervised background task and return the Task.

    `factory` must be a ZERO-ARG callable returning a fresh awaitable each
    call (i.e. pass the coroutine function itself, not a coroutine object —
    a coroutine can only be awaited once, so restarts need a new one).
    Cancelling the returned task cancels the running child coroutine too.
    """
    if os.environ.get("TASK_SUPERVISOR_ENABLED", "1") == "0":
        # Kill switch: exact pre-supervisor behavior, no wrapper at all.
        return asyncio.create_task(factory())
    return asyncio.create_task(
        _supervisor_loop(factory, name, max_restarts, window_seconds, base_backoff),
        name=f"supervised:{name}",
    )


async def _supervisor_loop(
    factory: Callable[[], Awaitable],
    name: str,
    max_restarts: int,
    window_seconds: float,
    base_backoff: float,
) -> None:
    crash_times: list[float] = []  # monotonic timestamps of crashes in window
    while True:
        try:
            await factory()
            # A lifespan loop returning cleanly is unusual (they are all
            # `while True`) but it is NOT a crash — honor the return.
            logger.info(f"[supervisor] task '{name}' exited cleanly — not restarting")
            return
        except asyncio.CancelledError:
            # Shutdown path (lifespan finally-block .cancel()) — propagate.
            raise
        except Exception as e:
            import traceback as _tb
            tb_str = _tb.format_exc()
            logger.error(f"[supervisor] task '{name}' crashed: {type(e).__name__}: {e}")

            now = time.monotonic()
            crash_times = [t for t in crash_times if now - t < window_seconds]
            crash_times.append(now)
            gave_up = len(crash_times) > max_restarts

            # Alert (best-effort, isolated): send_pipeline_failure_alert has
            # its own Redis dedup window so a crash-loop can't spam admins.
            try:
                from app.engines.pipeline_alerts import send_pipeline_failure_alert
                await send_pipeline_failure_alert(
                    reason=(
                        f"Background task '{name}' "
                        + ("exceeded restart budget — staying DEAD" if gave_up else "crashed — restarting")
                    ),
                    context={
                        "job": "task_supervisor",
                        "task": name,
                        "error": f"{type(e).__name__}: {e}",
                        "crashes_in_window": len(crash_times),
                        "max_restarts": max_restarts,
                        "window_seconds": window_seconds,
                        "final": gave_up,
                    },
                    traceback_str=tb_str,
                )
            except Exception as alert_err:
                logger.warning(f"[supervisor] alert send failed for '{name}': {alert_err}")

            if gave_up:
                logger.error(
                    f"[supervisor] task '{name}' crashed {len(crash_times)} times within "
                    f"{window_seconds:.0f}s (budget {max_restarts}) — giving up; task stays dead"
                )
                return

            backoff = base_backoff * (2 ** (len(crash_times) - 1))
            logger.warning(
                f"[supervisor] restarting '{name}' in {backoff:.1f}s "
                f"(crash {len(crash_times)}/{max_restarts} within {window_seconds:.0f}s window)"
            )
            await asyncio.sleep(backoff)

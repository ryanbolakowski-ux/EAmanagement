"""Shared Polygon rate-limit gate.

Free-tier Polygon = 5 RPM. We cap our usage at 4 RPM to leave headroom
for the preview endpoint to land between session-runner bursts. Every
chain-pull / aggs-pull / underlying-quote in the options stack goes
through this single gate so we never overshoot — even when 5 runners
spin up at once."""
import asyncio
from datetime import datetime, timedelta, timezone


class _RateGate:
    def __init__(self, max_per_minute: int = 4):
        self._tokens = max_per_minute
        self._max = max_per_minute
        self._refill_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = datetime.now(timezone.utc)
            if now >= self._refill_at:
                self._tokens = self._max
                self._refill_at = now + timedelta(minutes=1)
            if self._tokens > 0:
                self._tokens -= 1
                return
            wait_s = (self._refill_at - now).total_seconds() + 1
        await asyncio.sleep(max(1, wait_s))
        await self.acquire()


# One shared gate for the entire process
gate = _RateGate(max_per_minute=4)

"""In-memory ring-buffer log sink for the Admin Systems Check dashboard.

A `collections.deque(maxlen=N)` is attached as a loguru sink so the last N log
records (any level) live in memory. The Systems Check endpoint reads from
this deque to surface recent errors without shelling out to ``docker logs``
from inside the container (which is brittle and requires the Docker socket).

Design constraints
------------------
* Process-local. Each backend worker has its own deque — for our deployment
  (single uvicorn worker behind nginx) this is fine. If we ever scale to
  multiple workers we will swap this for a Redis stream.
* No PII. We capture the rendered message + level + logger name + ISO
  timestamp ONLY. We never serialize the bound extras dict (which may
  contain user emails, broker tokens, etc.). The message itself is whatever
  the call-site put in `logger.info(...)`, so call-sites must continue to
  keep secrets out of log lines.
* Bounded memory. With maxlen=200 and an average rendered record of ~400B,
  the buffer caps at ~80KB. Safe to hold across the process lifetime.
* Idempotent install. ``install_ring_buffer_sink`` checks a module flag so
  reloads (uvicorn --reload during dev) don't double-register the sink.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from loguru import logger as _logger

# Hold the last 200 records. The Systems Check endpoint only ever returns
# the top 10 *errors* — but we keep info-level too so future expansions
# (e.g. "show last 20 warnings") don't require a re-deploy.
_BUFFER: "deque[dict[str, Any]]" = deque(maxlen=200)
_SINK_INSTALLED = False


def _sink(message) -> None:
    """Loguru sink. ``message`` is a loguru ``Message`` object with a
    ``.record`` dict. We extract a tiny subset (no extras) and append."""
    try:
        rec = message.record
        _BUFFER.append({
            "at": rec["time"].astimezone(timezone.utc).isoformat(),
            "level": rec["level"].name,
            "logger": rec.get("name") or "",
            "message": str(rec.get("message") or "")[:500],
        })
    except Exception:
        # The sink must NEVER raise — that would interrupt the log pipeline
        # and surface as ``logger.add`` errors on the next call.
        pass


def install_ring_buffer_sink() -> None:
    """Attach the deque as a loguru sink. Idempotent."""
    global _SINK_INSTALLED
    if _SINK_INSTALLED:
        return
    try:
        _logger.add(_sink, level="INFO", enqueue=False, backtrace=False,
                    diagnose=False)
        _SINK_INSTALLED = True
    except Exception:
        # Defensive — if loguru misbehaves we want the backend to still boot.
        pass


def get_recent_records(level: str | None = None, limit: int = 10) -> list[dict]:
    """Return the most recent log records, newest first. Optional level
    filter (e.g. ``level="ERROR"``)."""
    items = list(_BUFFER)
    if level:
        items = [r for r in items if r.get("level") == level.upper()]
    items.reverse()
    return items[:limit]

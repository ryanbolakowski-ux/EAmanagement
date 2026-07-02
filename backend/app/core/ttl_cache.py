"""Bounded, thread-safe TTL cache for the module-level dict caches.

WHY THIS EXISTS: every in-process cache in the codebase (preview chains,
fundamentals, yfinance results, proxy scales, ...) was a bare module-level
dict where the TTL was only checked on READ. An entry that is written once
and never read again — or read only while fresh — is NEVER removed, so the
dict grows forever. For caches keyed on user-controlled input (e.g. the
options preview-chain cache keyed on (underlying, side, dte_min, dte_max))
that is an unbounded memory leak an authenticated user can drive at will.

TTLCache keeps the exact get/set call shape of a dict (`cache.get(key)`,
`cache[key] = value`, `key in cache`, `len(cache)`) so swapping it in
changes NOTHING about the call-site semantics — the sites keep their own
(fetched_at, value) tuples and their own freshness checks. What it adds:

  * expired entries are pruned on every set (not just lazily on read),
  * insertion past `maxsize` evicts the oldest entries first,
  * all mutation happens under a single threading.Lock, so the sync
    yfinance/watcher threads and the event loop can share one cache safely.

Stdlib only (threading + OrderedDict); safe to import from anywhere.
"""
import threading
import time
from collections import OrderedDict
from typing import Any, Hashable, Optional


class TTLCache:
    """Dict-like cache with a per-cache TTL and a hard size bound."""

    def __init__(self, maxsize: int = 128, ttl_seconds: float = 3600.0):
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.maxsize = int(maxsize)
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        # key -> (expires_at_monotonic, value); insertion-ordered so the
        # oldest entry is always first for maxsize eviction.
        self._data: "OrderedDict[Hashable, tuple[float, Any]]" = OrderedDict()

    # ── dict-mirroring API ──────────────────────────────────────────────
    def get(self, key: Hashable, default: Optional[Any] = None) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return default
            expires_at, value = hit
            if time.monotonic() >= expires_at:
                # Lazy expiry on read, same as the old manual TTL checks.
                del self._data[key]
                return default
            return value

    def set(self, key: Hashable, value: Any) -> None:
        now = time.monotonic()
        with self._lock:
            # Prune EVERYTHING expired on every write — this is the fix for
            # "TTL only checked on read, expired entries persist".
            expired = [k for k, (exp, _) in self._data.items() if now >= exp]
            for k in expired:
                del self._data[k]
            if key in self._data:
                # Re-set refreshes both the TTL and the eviction order.
                del self._data[key]
            self._data[key] = (now + self.ttl_seconds, value)
            # Evict oldest-inserted entries past the bound (never the new key —
            # maxsize >= 1 and the new entry is last).
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def __getitem__(self, key: Hashable) -> Any:
        sentinel = object()
        value = self.get(key, sentinel)
        if value is sentinel:
            raise KeyError(key)
        return value

    def __setitem__(self, key: Hashable, value: Any) -> None:
        self.set(key, value)

    def __contains__(self, key: Hashable) -> bool:
        sentinel = object()
        return self.get(key, sentinel) is not sentinel

    def __len__(self) -> int:
        # Count only live (unexpired) entries so len() mirrors what get()
        # would actually serve.
        now = time.monotonic()
        with self._lock:
            return sum(1 for exp, _ in self._data.values() if now < exp)

    def pop(self, key: Hashable, default: Optional[Any] = None) -> Any:
        with self._lock:
            hit = self._data.pop(key, None)
        if hit is None:
            return default
        expires_at, value = hit
        return default if time.monotonic() >= expires_at else value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

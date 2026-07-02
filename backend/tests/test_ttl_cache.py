"""Unit tests for app.core.ttl_cache.TTLCache — the bounded replacement for
the module-level dict caches (preview chains, fundamentals, yfinance, ...).

Covers the three properties the swap depends on:
  * ttl expiry: expired entries are invisible to get()/__contains__,
  * pruning on SET (the original bug: TTL only checked on read, so
    write-once-never-read entries persisted forever),
  * maxsize eviction: oldest-inserted entries evicted first.
No network, no DB — pure in-process."""
import threading
import time

import pytest

from app.core.ttl_cache import TTLCache


def test_basic_get_set_mirrors_dict():
    c = TTLCache(maxsize=8, ttl_seconds=60)
    c["a"] = (1, "one")
    c.set("b", (2, "two"))
    assert c.get("a") == (1, "one")
    assert c["b"] == (2, "two")
    assert c.get("missing") is None
    assert c.get("missing", "dflt") == "dflt"
    assert "a" in c and "b" in c and "missing" not in c
    assert len(c) == 2
    with pytest.raises(KeyError):
        _ = c["missing"]


def test_ttl_expiry_on_read():
    c = TTLCache(maxsize=8, ttl_seconds=0.05)
    c["k"] = "v"
    assert c.get("k") == "v"
    time.sleep(0.08)
    assert c.get("k") is None
    assert "k" not in c
    assert len(c) == 0


def test_expired_entries_pruned_on_set():
    """The verified finding: with a bare dict, TTL was only checked on read so
    entries written once and never re-read persisted forever. TTLCache must
    physically drop them on the next set()."""
    c = TTLCache(maxsize=100, ttl_seconds=0.05)
    for i in range(10):
        c[f"old{i}"] = i
    assert len(c._data) == 10
    time.sleep(0.08)
    c["fresh"] = "x"           # this set() must sweep the 10 expired entries
    assert len(c._data) == 1   # internal store, not just the live-entry view
    assert c.get("fresh") == "x"


def test_maxsize_evicts_oldest_first():
    c = TTLCache(maxsize=3, ttl_seconds=60)
    c["a"] = 1
    c["b"] = 2
    c["c"] = 3
    c["d"] = 4                 # over the bound: "a" (oldest) must go
    assert len(c) == 3
    assert "a" not in c
    assert c.get("b") == 2 and c.get("c") == 3 and c.get("d") == 4


def test_reset_refreshes_eviction_order():
    c = TTLCache(maxsize=2, ttl_seconds=60)
    c["a"] = 1
    c["b"] = 2
    c["a"] = 11                # re-set: "a" becomes the newest entry
    c["c"] = 3                 # now "b" is oldest and must be the eviction
    assert "b" not in c
    assert c.get("a") == 11 and c.get("c") == 3


def test_thread_safety_smoke():
    """Hammer set/get from several threads: no exceptions, bound respected."""
    c = TTLCache(maxsize=32, ttl_seconds=60)
    errors = []

    def worker(tid):
        try:
            for i in range(500):
                c[(tid, i % 40)] = i
                c.get((tid, (i + 7) % 40))
                _ = (tid, i % 40) in c
        except Exception as e:   # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(c._data) <= 32


def test_invalid_construction_rejected():
    with pytest.raises(ValueError):
        TTLCache(maxsize=0, ttl_seconds=60)
    with pytest.raises(ValueError):
        TTLCache(maxsize=8, ttl_seconds=0)

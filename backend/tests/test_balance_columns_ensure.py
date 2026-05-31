"""Unit tests for _ensure_balance_columns.

We don't need a real DB to verify this helper's contract: it only ever
issues two ADD COLUMN IF NOT EXISTS statements and a commit. We stub the
DB with a recorder and assert on the executed SQL.
"""
import asyncio
import pytest


class _NullResult:
    def first(self): return None
    def fetchone(self): return None
    def scalar(self): return None


class RecorderDB:
    """Async-DB stub that records every executed statement."""
    def __init__(self, *, fail_on=None):
        self.statements: list[str] = []
        self.commits = 0
        self.rolled_back = 0
        self.fail_on = fail_on  # raise on the Nth call (1-indexed)

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        if self.fail_on is not None and len(self.statements) == self.fail_on:
            raise RuntimeError("forced failure for test")
        return _NullResult()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rolled_back += 1


def _reset_helper_flag():
    """Force the next call to actually run the ALTER, regardless of process state."""
    from app.api.routes import live_trading as lt
    lt._balance_cols_checked = False


def _run(coro):
    return asyncio.run(coro)


def test_ensure_creates_both_columns():
    """First call must issue ALTERs for BOTH starting_equity and cached_cash."""
    _reset_helper_flag()
    from app.api.routes.live_trading import _ensure_balance_columns

    db = RecorderDB()
    _run(_ensure_balance_columns(db))

    joined = "\n".join(db.statements)
    assert "starting_equity" in joined, joined
    assert "cached_cash" in joined, joined
    # Both should be ADD COLUMN IF NOT EXISTS (idempotent Postgres DDL).
    assert "ADD COLUMN IF NOT EXISTS" in joined or "add column if not exists" in joined.lower()
    assert db.commits >= 1


def test_ensure_idempotent():
    """Second call short-circuits via the module flag — no execute, no commit.
    Then reset the flag and re-run: should still succeed (Postgres' IF NOT
    EXISTS makes the SQL itself idempotent; we just verify no exception)."""
    _reset_helper_flag()
    from app.api.routes.live_trading import _ensure_balance_columns

    db1 = RecorderDB()
    _run(_ensure_balance_columns(db1))
    n1 = len(db1.statements)
    assert n1 >= 2

    # Second call without resetting the flag → no DB activity at all.
    db2 = RecorderDB()
    _run(_ensure_balance_columns(db2))
    assert db2.statements == [], db2.statements
    assert db2.commits == 0

    # Reset and call again on a fresh DB → re-issues the ALTERs without raising.
    _reset_helper_flag()
    db3 = RecorderDB()
    _run(_ensure_balance_columns(db3))
    assert len(db3.statements) == n1
    assert db3.commits >= 1

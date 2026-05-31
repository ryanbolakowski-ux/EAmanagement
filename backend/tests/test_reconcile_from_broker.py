"""Unit tests for the broker-history reconcile path.

These tests DO NOT touch any real database. We stub the AsyncSession with
a FakeDB that records every SQL statement executed and returns canned
SELECT results, so we can assert:

  * The right number of INSERTs are issued.
  * No UPDATE is ever issued against an existing row.
  * Idempotency: re-running with the same broker history → 0 inserts.
  * The Tradier history normaliser handles single-dict + empty responses.

The cases:
  1. test_inserts_missing_closes      — broker returns 3 fills, 1 already
     in the trades table → exactly 2 INSERTs, both tagged 'tradier_reconcile'.
  2. test_idempotent_when_all_matched — 2nd run inserts 0.
  3. test_never_updates_existing      — pre-seeded row's row_id never appears
     in any UPDATE statement.
  4. test_handles_single_event_dict   — history.event as a dict normalises.
  5. test_handles_empty_history       — empty/missing → [], no exception.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


# ─────────────────────────────────────────────────────────────────────────
# FakeDB — a stand-in for AsyncSession that records every executed
# statement and returns scripted SELECT rows.
# ─────────────────────────────────────────────────────────────────────────

class _FakeResultOne:
    def __init__(self, row):
        self._row = row
    def first(self):
        return self._row
    def fetchone(self):
        return self._row
    def scalar(self):
        return self._row[0] if self._row else None
    def scalar_one_or_none(self):
        return self._row[0] if self._row else None


class FakeDB:
    """Pretend-AsyncSession that records SQL and serves canned SELECTs.

    Configure `existing_rows`: a list of dicts representing rows currently
    in the `trades` table for the test user. The fake matches incoming
    SELECTs against the same predicates the reconcile code uses.
    """
    def __init__(self, *, existing_rows=None):
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0
        self.rolled_back = 0
        self.existing_rows = list(existing_rows or [])

    async def execute(self, stmt, params=None):
        sql = str(stmt) if not isinstance(stmt, str) else stmt
        params = dict(params or {})
        self.executed.append((sql, params))
        sql_l = sql.lower().strip()

        # SELECT by broker_order_id
        if "where user_id = :uid and broker_order_id = :oid" in sql_l:
            oid = params.get("oid")
            uid = params.get("uid")
            for row in self.existing_rows:
                if row.get("user_id") == uid and row.get("broker_order_id") == oid:
                    return _FakeResultOne((row["id"],))
            return _FakeResultOne(None)

        # SELECT for match-by-(inst, qty, price-window[, time-window])
        if "from trades" in sql_l and "where user_id = :uid" in sql_l \
                and "and instrument = :inst" in sql_l:
            uid = params.get("uid")
            inst = params.get("inst")
            qty = params.get("qty")
            p_lo = params.get("p_lo")
            p_hi = params.get("p_hi")
            t_lo = params.get("t_lo")
            t_hi = params.get("t_hi")
            for row in self.existing_rows:
                if row.get("user_id") != uid: continue
                if row.get("instrument") != inst: continue
                if int(abs(row.get("contracts") or 0)) != int(qty or 0): continue
                ep = row.get("entry_price")
                xp = row.get("exit_price")
                price_match = False
                if ep is not None and p_lo <= ep <= p_hi:
                    price_match = True
                if xp is not None and p_lo <= xp <= p_hi:
                    price_match = True
                if not price_match: continue
                if t_lo is not None and t_hi is not None:
                    et = row.get("entry_time")
                    xt = row.get("exit_time")
                    time_match = False
                    if et is not None and t_lo <= et <= t_hi: time_match = True
                    if xt is not None and t_lo <= xt <= t_hi: time_match = True
                    if et is None and xt is None: time_match = True
                    if not time_match: continue
                return _FakeResultOne((row["id"],))
            return _FakeResultOne(None)

        # INSERT — record it; reconcile assigns its own id.
        if sql_l.startswith("insert into trades"):
            # Auto-promote inserts into existing_rows so a 2nd reconcile
            # call sees them via broker_order_id lookup.
            self.existing_rows.append({
                "id":              params.get("id"),
                "user_id":         params.get("uid"),
                "instrument":      params.get("inst"),
                "contracts":       params.get("qty"),
                "entry_price":     params.get("price"),
                "exit_price":      params.get("price"),
                "entry_time":      params.get("ts"),
                "exit_time":       params.get("ts"),
                "broker_order_id": params.get("oid"),
                "_inserted_by_reconcile": True,
                "notes":           params.get("notes"),
                "exit_reason":     "tradier_reconcile",
                "pnl":             params.get("pnl"),
                "commission":      params.get("comm"),
            })
            return _FakeResultOne(None)

        if sql_l.startswith("update"):
            return _FakeResultOne(None)

        # Default
        return _FakeResultOne(None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rolled_back += 1

    # Convenience asserters
    def inserts(self):
        return [(s, p) for s, p in self.executed if s.lower().strip().startswith("insert into trades")]

    def updates(self):
        return [(s, p) for s, p in self.executed if s.lower().strip().startswith("update")]


# ─────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────

class _FakeBroker:
    """Minimal stand-in for TradierBroker."""
    def __init__(self, fills):
        self._fills = fills
    async def connect(self): return True
    async def disconnect(self): pass
    async def get_account_history(self, limit=500):
        return list(self._fills)


def _install_fake_broker(monkeypatch, fills):
    """Make build_broker_from_account return our fake.

    reconcile.py does:
      from app.engines.live_trading.broker_factory import build_broker_from_account
    *inside* its function body, so we patch the source module rather than
    the reconcile module's namespace.
    """
    monkeypatch.setattr(
        "app.engines.live_trading.broker_factory.build_broker_from_account",
        lambda account: _FakeBroker(fills),
    )


def _fake_account(user_id, broker_account_id):
    return SimpleNamespace(
        id=broker_account_id,
        user_id=user_id,
        broker="tradier",
        is_demo=True,
        sandbox_mode=True,
        encrypted_credentials="",
    )


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────

def test_inserts_missing_closes(monkeypatch):
    """3 broker fills; 1 already exists → exactly 2 INSERTs, both tagged."""
    from app.engines.live_trading.reconcile import reconcile_trades_from_broker

    user_id = str(uuid.uuid4())
    acct_id = str(uuid.uuid4())
    existing_ts = _ts("2026-04-15 14:30:00")

    db = FakeDB(existing_rows=[{
        "id":              str(uuid.uuid4()),
        "user_id":         user_id,
        "instrument":      "AAPL",
        "contracts":       10,
        "entry_price":     200.0,
        "exit_price":      200.0,
        "entry_time":      existing_ts,
        "exit_time":       existing_ts,
        "broker_order_id": "AAPL-ORD-1",
    }])

    fills = [
        # Match by broker_order_id → already_tracked.
        {"type": "trade", "date": existing_ts.isoformat() + "Z",
         "symbol": "AAPL", "side": "sell", "quantity": 10, "price": 200.5,
         "amount": 2005.0, "commission": 1.0, "trade_type": "sell",
         "raw": {"type": "trade", "trade": {"orderid": "AAPL-ORD-1"}}},
        # New fill (EZGO) → INSERT.
        {"type": "trade", "date": "2026-05-20 15:00:00Z",
         "symbol": "EZGO", "side": "sell", "quantity": 100, "price": 2.5,
         "amount": 250.0, "commission": 0.5, "trade_type": "sell",
         "raw": {"type": "trade", "trade": {"orderid": "EZGO-ORD-1"}}},
        # New fill (PESI) → INSERT.
        {"type": "trade", "date": "2026-05-21 16:15:00Z",
         "symbol": "PESI", "side": "sell", "quantity": 50, "price": 4.2,
         "amount": 210.0, "commission": 0.5, "trade_type": "sell",
         "raw": {"type": "trade", "trade": {"orderid": "PESI-ORD-1"}}},
    ]
    _install_fake_broker(monkeypatch, fills)

    account = _fake_account(user_id, acct_id)
    result = _run(reconcile_trades_from_broker(db, account))

    assert result["fetched_from_broker"] == 3, result
    assert result["already_tracked"] == 1, result
    assert result["inserted"] == 2, result
    assert len(result["inserted_ids"]) == 2

    inserted_syms = [p.get("inst") for _, p in db.inserts()]
    assert sorted(inserted_syms) == ["EZGO", "PESI"], inserted_syms

    # Tagged as tradier_reconcile (via exit_reason column param)
    # The INSERT SQL literally hard-codes 'tradier_reconcile' as exit_reason,
    # so we don't need to inspect params for it — but we verify by inspecting
    # one of the new rows the FakeDB recorded.
    new_ezgo = next(r for r in db.existing_rows if r.get("instrument") == "EZGO")
    assert new_ezgo["exit_reason"] == "tradier_reconcile"
    assert "tradier_reconcile" in (new_ezgo["notes"] or "")
    assert new_ezgo["broker_order_id"] == "EZGO-ORD-1"


def test_idempotent_when_all_matched(monkeypatch):
    """Two consecutive runs with identical broker history → 2nd inserts 0."""
    from app.engines.live_trading.reconcile import reconcile_trades_from_broker

    user_id = str(uuid.uuid4())
    acct_id = str(uuid.uuid4())
    db = FakeDB()

    fills = [{
        "type": "trade", "date": "2026-05-20 15:00:00Z",
        "symbol": "EZGO", "side": "sell", "quantity": 100, "price": 2.5,
        "amount": 250.0, "commission": 0.5, "trade_type": "sell",
        "raw": {"type": "trade", "trade": {"orderid": "EZGO-ORD-1"}},
    }]
    _install_fake_broker(monkeypatch, fills)

    account = _fake_account(user_id, acct_id)
    r1 = _run(reconcile_trades_from_broker(db, account))
    assert r1["inserted"] == 1

    r2 = _run(reconcile_trades_from_broker(db, account))
    assert r2["inserted"] == 0, r2
    assert r2["already_tracked"] == 1, r2


def test_never_updates_existing(monkeypatch):
    """Pre-seeded row must NEVER appear in an UPDATE statement."""
    from app.engines.live_trading.reconcile import reconcile_trades_from_broker

    user_id = str(uuid.uuid4())
    acct_id = str(uuid.uuid4())
    existing_ts = _ts("2026-04-15 14:30:00")
    existing_row = {
        "id":              str(uuid.uuid4()),
        "user_id":         user_id,
        "instrument":      "AAPL",
        "contracts":       10,
        "entry_price":     200.0,
        "exit_price":      200.0,
        "entry_time":      existing_ts,
        "exit_time":       existing_ts,
        "broker_order_id": "AAPL-EXISTING",
        "pnl":             100.0,
        "commission":      1.0,
        "net_pnl":         99.0,
        "exit_reason":     "pre-existing",
        "notes":           '{"source": "engine"}',
    }
    db = FakeDB(existing_rows=[existing_row])
    before_snapshot = dict(existing_row)

    fills = [{
        "type": "trade", "date": existing_ts.isoformat() + "Z",
        "symbol": "AAPL", "side": "sell", "quantity": 10, "price": 200.0,
        "amount": 2000.0, "commission": 1.0, "trade_type": "sell",
        "raw": {"type": "trade", "trade": {"orderid": "DIFFERENT-ORDER-ID"}},
    }]
    _install_fake_broker(monkeypatch, fills)

    account = _fake_account(user_id, acct_id)
    r = _run(reconcile_trades_from_broker(db, account))
    assert r["inserted"] == 0, r

    # Hard contract: zero UPDATE statements executed.
    assert db.updates() == [], f"unexpected UPDATEs: {db.updates()}"

    # And the row dict we held a reference to is byte-identical
    # (FakeDB never touched it).
    assert existing_row == before_snapshot


def test_handles_single_event_dict():
    """history.event as a dict (not list) — normaliser must wrap it."""
    from app.engines.live_trading.tradier import TradierBroker

    broker = TradierBroker({"access_token": "x", "account_id": "X1"}, is_demo=True)
    broker._connected = True

    class _Resp:
        status = 200
        async def json(self):
            return {"history": {"event": {
                "type": "trade", "date": "2026-05-21 16:15:00Z",
                "amount": 250.0,
                "trade": {"symbol": "EZGO", "quantity": 100, "price": 2.5,
                          "commission": 0.5, "trade_type": "sell"},
            }}}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class _Sess:
        def get(self, url, params=None): return _Resp()

    broker._session = _Sess()

    out = asyncio.run(broker.get_account_history())
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["symbol"] == "EZGO"
    assert out[0]["quantity"] == 100
    assert out[0]["side"] == "sell"


def test_handles_empty_history():
    """Empty/missing history payloads → [] without raising."""
    from app.engines.live_trading.tradier import TradierBroker

    cases = [
        {},
        {"history": None},
        {"history": {}},
        {"history": {"event": None}},
        {"history": {"event": []}},
    ]
    for payload in cases:
        broker = TradierBroker({"access_token": "x", "account_id": "X1"}, is_demo=True)
        broker._connected = True

        class _Resp:
            status = 200
            def __init__(self, p): self._p = p
            async def json(self): return self._p
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class _Sess:
            def __init__(self, p): self._p = p
            def get(self, url, params=None): return _Resp(self._p)

        broker._session = _Sess(payload)
        out = asyncio.run(broker.get_account_history())
        assert out == [], f"expected [] for {payload!r}, got {out!r}"

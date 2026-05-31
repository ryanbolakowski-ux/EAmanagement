"""Bug: Email Signals page rendered the user's full account_signals table
including ~1,665 internal `suppressed` rows (dead-zone, session cap, duplicate
content-hash). The default list endpoint must filter to `status='sent'` so the
user only sees actually-delivered signals, with opt-in escape hatches for
debugging via `?status=...` or `?include_suppressed=true`."""
import os
import uuid as _uuid
import psycopg2
import pytest


def _conn():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url, connect_timeout=5)


def _ensure_strategy(uid, name):
    """Insert a minimal active strategy row that account_signals.strategy_id
    can FK to. Returns its id. Idempotent on (user_id, name)."""
    cn = _conn()
    try:
        with cn, cn.cursor() as cur:
            cur.execute(
                "SELECT id FROM strategies WHERE user_id=%s AND name=%s",
                (uid, name),
            )
            r = cur.fetchone()
            if r:
                return str(r[0])
            sid = str(_uuid.uuid4())
            cur.execute(
                "INSERT INTO strategies (id, user_id, name, status, instruments, risk_reward_ratio, created_at) "
                "VALUES (%s, %s, %s, %s, %s::json, %s, NOW())",
                (sid, uid, name, "ACTIVE", '["ES"]', 2.0),
            )
            return sid
    finally:
        cn.close()


def _ensure_watcher(uid, sid, label):
    """Insert a watcher row — account_signals.watcher_id is NOT NULL so every
    seeded signal must hang off a watcher. Idempotent on (user_id, account_label)."""
    cn = _conn()
    try:
        with cn, cn.cursor() as cur:
            cur.execute(
                "SELECT id FROM account_signal_watchers WHERE user_id=%s AND account_label=%s",
                (uid, label),
            )
            r = cur.fetchone()
            if r:
                return str(r[0])
            wid = str(_uuid.uuid4())
            cur.execute(
                "INSERT INTO account_signal_watchers "
                "(id, user_id, strategy_id, instruments, account_label, channels, is_active, created_at) "
                "VALUES (%s, %s, %s, %s::json, %s, %s::json, FALSE, NOW())",
                (wid, uid, sid, '["ES"]', label, '["email"]'),
            )
            return wid
    finally:
        cn.close()


def _seed_signals(uid, sid, wid, sent=5, suppressed=5, outcomes=None):
    """Seed `sent` rows with status='sent' and `suppressed` rows with status=
    'suppressed' for this user. Returns list of inserted ids. Optionally
    attaches outcomes by index across the sent rows."""
    outcomes = outcomes or {}
    ids = []
    cn = _conn()
    try:
        with cn, cn.cursor() as cur:
            for i in range(sent):
                sigid = str(_uuid.uuid4())
                o = outcomes.get(i)
                cur.execute(
                    "INSERT INTO account_signals "
                    "(id, watcher_id, user_id, strategy_id, instrument, direction, entry_price, stop_loss, take_profit, "
                    " bias, fired_at, status, outcome, outcome_price, outcome_r) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() - INTERVAL \'1 hour\' * %s, "
                    "        %s, %s, %s, %s)",
                    (sigid, wid, uid, sid, "ES", "long", 5000.0 + i, 4990.0 + i, 5020.0 + i,
                     "bullish", i + 1, "sent",
                     o["outcome"] if o else None,
                     o.get("price") if o else None,
                     o.get("r") if o else None),
                )
                ids.append(sigid)
            for i in range(suppressed):
                sigid = str(_uuid.uuid4())
                cur.execute(
                    "INSERT INTO account_signals "
                    "(id, watcher_id, user_id, strategy_id, instrument, direction, entry_price, stop_loss, take_profit, "
                    " bias, fired_at, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() - INTERVAL \'2 hours\' * %s, %s)",
                    (sigid, wid, uid, sid, "ES", "long", 5100.0 + i, 5090.0 + i, 5120.0 + i,
                     "bullish", i + 1, "suppressed"),
                )
                ids.append(sigid)
        return ids
    finally:
        cn.close()


def _cleanup(ids, sid=None, wid=None):
    """Remove signal rows we inserted, and (optionally) the watcher + strategy
    we created. Always called from the fixture teardown so prod state stays
    clean after every test run."""
    cn = _conn()
    try:
        with cn, cn.cursor() as cur:
            if ids:
                cur.execute("DELETE FROM account_signals WHERE id = ANY(%s::uuid[])", (ids,))
            if wid:
                cur.execute("DELETE FROM account_signal_watchers WHERE id = %s", (wid,))
            if sid:
                cur.execute("DELETE FROM strategies WHERE id = %s", (sid,))
    finally:
        cn.close()


@pytest.fixture
def seeded(auth):
    uid = auth["user_id"]
    sid = _ensure_strategy(uid, "pytest-signals-filter-strategy")
    wid = _ensure_watcher(uid, sid, "pytest-signals-filter-watcher")
    ids = _seed_signals(uid, sid, wid, sent=5, suppressed=5, outcomes={
        0: {"outcome": "win",  "price": 5020.0, "r": 2.3},
        1: {"outcome": "loss", "price": 4990.0, "r": -1.0},
    })
    yield {"uid": uid, "sid": sid, "wid": wid, "ids": ids}
    _cleanup(ids, sid=sid, wid=wid)


def test_default_returns_sent_only(client, seeded):
    """With no querystring, the list MUST only contain rows whose status='sent'."""
    r = client.get("/api/v1/account-signals/?limit=500")
    assert r.status_code == 200, r.text
    rows = r.json()
    my_ids = set(seeded["ids"])
    mine = [x for x in rows if x["id"] in my_ids]
    # We seeded 5 sent + 5 suppressed for this user — expect exactly the 5 sent ones back.
    assert len(mine) == 5, f"expected 5 sent rows, got {len(mine)}: {[r['status'] for r in mine]}"
    assert all(x["status"] == "sent" for x in mine)


def test_include_suppressed_returns_all(client, seeded):
    """`?include_suppressed=true` drops the status filter entirely."""
    r = client.get("/api/v1/account-signals/?include_suppressed=true&limit=500")
    assert r.status_code == 200, r.text
    rows = r.json()
    my_ids = set(seeded["ids"])
    mine = [x for x in rows if x["id"] in my_ids]
    assert len(mine) == 10, f"expected 10 rows (5 sent + 5 suppressed), got {len(mine)}"
    statuses = {x["status"] for x in mine}
    assert "sent" in statuses and "suppressed" in statuses


def test_status_filter_passes_through(client, seeded):
    """Explicit `?status=suppressed` returns suppressed rows only."""
    r = client.get("/api/v1/account-signals/?status=suppressed&limit=500")
    assert r.status_code == 200, r.text
    rows = r.json()
    my_ids = set(seeded["ids"])
    mine = [x for x in rows if x["id"] in my_ids]
    assert len(mine) == 5, f"expected 5 suppressed rows, got {len(mine)}"
    assert all(x["status"] == "suppressed" for x in mine)


def test_signal_response_includes_outcome(client, seeded):
    """The default response objects MUST carry the new outcome fields so the
    frontend dot indicator can render."""
    r = client.get("/api/v1/account-signals/?limit=500")
    assert r.status_code == 200, r.text
    rows = r.json()
    my_ids = set(seeded["ids"])
    mine = [x for x in rows if x["id"] in my_ids]
    # All sent rows must carry the outcome fields (even if NULL).
    for row in mine:
        assert "outcome" in row
        assert "outcome_price" in row
        assert "outcome_r" in row
        assert "resolved_at" in row
    wins = [x for x in mine if x.get("outcome") == "win"]
    losses = [x for x in mine if x.get("outcome") == "loss"]
    assert len(wins) == 1, f"expected one win, saw {len(wins)}"
    assert len(losses) == 1, f"expected one loss, saw {len(losses)}"
    assert abs(wins[0]["outcome_r"] - 2.3) < 1e-6
    assert abs(losses[0]["outcome_r"] - (-1.0)) < 1e-6

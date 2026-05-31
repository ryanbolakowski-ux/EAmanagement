"""Admin-only diagnostic endpoint that surfaces suppressed signal rows
(dead-zone, session cap, duplicate, etc.) so we can keep them audit-visible
while hiding them from the user-facing /api/v1/account-signals/ list."""
import os
import uuid as _uuid
import psycopg2
import pytest


def _conn():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url, connect_timeout=5)


def _set_admin(uid, value):
    cn = _conn()
    try:
        with cn, cn.cursor() as cur:
            cur.execute("UPDATE users SET is_admin=%s WHERE id=%s", (value, uid))
    finally:
        cn.close()


def _ensure_strategy(uid, name):
    cn = _conn()
    try:
        with cn, cn.cursor() as cur:
            cur.execute("SELECT id FROM strategies WHERE user_id=%s AND name=%s", (uid, name))
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


def _seed(uid, sid, wid, sent=3, suppressed=4):
    ids = []
    cn = _conn()
    try:
        with cn, cn.cursor() as cur:
            for i in range(sent):
                sigid = str(_uuid.uuid4())
                cur.execute(
                    "INSERT INTO account_signals "
                    "(id, watcher_id, user_id, strategy_id, instrument, direction, entry_price, stop_loss, take_profit, "
                    " bias, fired_at, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() - INTERVAL \'10 minutes\' * %s, %s)",
                    (sigid, wid, uid, sid, "ES", "long", 5200.0 + i, 5190.0 + i, 5220.0 + i,
                     "bullish", i + 1, "sent"),
                )
                ids.append(sigid)
            for i in range(suppressed):
                sigid = str(_uuid.uuid4())
                cur.execute(
                    "INSERT INTO account_signals "
                    "(id, watcher_id, user_id, strategy_id, instrument, direction, entry_price, stop_loss, take_profit, "
                    " bias, fired_at, status, error_message) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() - INTERVAL \'30 minutes\' * %s, %s, %s)",
                    (sigid, wid, uid, sid, "ES", "long", 5300.0 + i, 5290.0 + i, 5320.0 + i,
                     "bullish", i + 1, "suppressed", f"pytest-suppressed-{i}"),
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
    sid = _ensure_strategy(uid, "pytest-signals-suppressed-strategy")
    wid = _ensure_watcher(uid, sid, "pytest-signals-suppressed-watcher")
    ids = _seed(uid, sid, wid, sent=3, suppressed=4)
    yield {"uid": uid, "sid": sid, "wid": wid, "ids": ids}
    _cleanup(ids, sid=sid, wid=wid)
    # always restore non-admin for other tests
    _set_admin(uid, False)


def test_non_admin_403(client, seeded):
    """Default fixture user is not admin → endpoint MUST 403."""
    _set_admin(seeded["uid"], False)
    r = client.get("/api/v1/account-signals/suppressed")
    assert r.status_code == 403, f"non-admin should be 403, got {r.status_code}: {r.text}"


def test_admin_returns_only_suppressed(client, seeded):
    """Promote the fixture user to admin → endpoint returns suppressed rows only."""
    _set_admin(seeded["uid"], True)
    try:
        r = client.get("/api/v1/account-signals/suppressed?limit=500")
        assert r.status_code == 200, r.text
        rows = r.json()
        my_ids = set(seeded["ids"])
        mine = [x for x in rows if x["id"] in my_ids]
        # Endpoint is global (not user-scoped), but among our seeded rows we
        # must see exactly the 4 suppressed and zero of the 3 sent.
        assert all(x["status"] == "suppressed" for x in mine), \
            f"expected only suppressed, got statuses: {[x['status'] for x in mine]}"
        assert len(mine) == 4, f"expected 4 suppressed rows, got {len(mine)}"
        # error_message must round-trip
        assert any((x.get("error_message") or "").startswith("pytest-suppressed-") for x in mine)
    finally:
        _set_admin(seeded["uid"], False)

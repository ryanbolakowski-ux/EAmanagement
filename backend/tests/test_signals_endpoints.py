"""Bug #3: /api/v1/email-signals/* returned 404 while UI labels say Email
Signals. Proves both the canonical and alias prefixes resolve, and that the
watcher list/create/stop lifecycle works."""
import uuid
import pytest


def test_account_signals_watchers_ok(client):
    r = client.get("/api/v1/account-signals/watchers")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_email_signals_alias_ok(client):
    """The alias must resolve (was a hidden 404)."""
    r = client.get("/api/v1/email-signals/watchers")
    assert r.status_code == 200, f"alias should 200, got {r.status_code}: {r.text}"
    assert isinstance(r.json(), list)


def test_email_signals_stats_alias_ok(client):
    r = client.get("/api/v1/email-signals/stats")
    assert r.status_code == 200, r.text


def test_watcher_create_and_stop(client):
    # need an active strategy to attach the watcher to
    s = client.post("/api/v1/strategies/", json={
        "name": f"pytest-watch-{uuid.uuid4().hex[:6]}",
        "instruments": ["ES"], "risk_reward_ratio": 2.0, "status": "active",
    })
    assert s.status_code == 201, s.text
    sid = s.json()["id"]
    wid = None
    try:
        c = client.post("/api/v1/account-signals/watchers", json={
            "strategy_id": sid,
            "instruments": ["ES"],
            "account_label": "pytest watcher",
            "channels": ["email"],
            "session_filter": "all",
        })
        assert c.status_code == 201, c.text
        wid = c.json()["id"]
        # it should now appear in the list
        lst = client.get("/api/v1/account-signals/watchers").json()
        assert any(w["id"] == wid for w in lst), "created watcher not in list"
        # and via the alias too
        lst2 = client.get("/api/v1/email-signals/watchers").json()
        assert any(w["id"] == wid for w in lst2), "watcher not visible via alias"
    finally:
        if wid:
            d = client.delete(f"/api/v1/account-signals/watchers/{wid}")
            assert d.status_code in (200, 204), d.text
        client.delete(f"/api/v1/strategies/{sid}")

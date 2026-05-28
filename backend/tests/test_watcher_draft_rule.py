"""Bug 2 — watcher creation blocked on draft strategies, allowed on active."""
import uuid


def _mk(client, status):
    r = client.post("/api/v1/strategies/", json={
        "name": f"watch-{status}-{uuid.uuid4().hex[:6]}",
        "instruments": ["ES"], "risk_reward_ratio": 2.0, "status": status})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_watcher_blocked_on_draft(client):
    sid = _mk(client, "draft")
    try:
        w = client.post("/api/v1/account-signals/watchers", json={
            "strategy_id": sid, "instruments": ["ES"],
            "account_label": "draft-test", "channels": ["email"]})
        assert w.status_code == 409, f"expected 409 on draft, got {w.status_code}: {w.text}"
        assert "draft" in w.text.lower()
    finally:
        client.delete(f"/api/v1/strategies/{sid}")


def test_watcher_allowed_on_active(client):
    sid = _mk(client, "active")
    wid = None
    try:
        w = client.post("/api/v1/account-signals/watchers", json={
            "strategy_id": sid, "instruments": ["ES"],
            "account_label": "active-test", "channels": ["email"]})
        assert w.status_code == 201, w.text
        wid = w.json()["id"]
    finally:
        if wid:
            client.delete(f"/api/v1/account-signals/watchers/{wid}")
        client.delete(f"/api/v1/strategies/{sid}")


def test_watcher_allowed_after_activation(client):
    sid = _mk(client, "draft")
    wid = None
    try:
        # blocked while draft
        assert client.post("/api/v1/account-signals/watchers", json={
            "strategy_id": sid, "instruments": ["ES"],
            "account_label": "x", "channels": ["email"]}).status_code == 409
        # activate, then allowed
        client.post(f"/api/v1/strategies/{sid}/activate")
        w = client.post("/api/v1/account-signals/watchers", json={
            "strategy_id": sid, "instruments": ["ES"],
            "account_label": "x", "channels": ["email"]})
        assert w.status_code == 201, w.text
        wid = w.json()["id"]
    finally:
        if wid:
            client.delete(f"/api/v1/account-signals/watchers/{wid}")
        client.delete(f"/api/v1/strategies/{sid}")

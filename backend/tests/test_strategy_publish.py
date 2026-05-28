"""Bug 1 — strategy publish/activate flow (create draft/active, draft<->active)."""
import uuid


def _payload(name, **over):
    p = {"name": name, "instruments": ["ES"], "risk_reward_ratio": 2.0,
         "primary_timeframe": "15m", "execution_timeframe": "1m",
         "stop_loss_type": "structure"}
    p.update(over)
    return p


def test_create_draft(client):
    r = client.post("/api/v1/strategies/", json=_payload(f"pub-draft-{uuid.uuid4().hex[:6]}", status="draft"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    try:
        assert r.json()["status"] == "draft"
    finally:
        client.delete(f"/api/v1/strategies/{sid}")


def test_create_active(client):
    r = client.post("/api/v1/strategies/", json=_payload(f"pub-active-{uuid.uuid4().hex[:6]}", status="active"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    try:
        assert r.json()["status"] == "active"
    finally:
        client.delete(f"/api/v1/strategies/{sid}")


def test_activate_endpoint_draft_to_active(client):
    r = client.post("/api/v1/strategies/", json=_payload(f"pub-act-{uuid.uuid4().hex[:6]}", status="draft"))
    sid = r.json()["id"]
    try:
        a = client.post(f"/api/v1/strategies/{sid}/activate")
        assert a.status_code == 200, a.text
        assert a.json()["status"] == "active"
        # persisted
        assert client.get(f"/api/v1/strategies/{sid}").json()["status"] == "active"
    finally:
        client.delete(f"/api/v1/strategies/{sid}")


def test_deactivate_endpoint_active_to_draft(client):
    r = client.post("/api/v1/strategies/", json=_payload(f"pub-deact-{uuid.uuid4().hex[:6]}", status="active"))
    sid = r.json()["id"]
    try:
        d = client.post(f"/api/v1/strategies/{sid}/deactivate")
        assert d.status_code == 200, d.text
        assert d.json()["status"] == "draft"
        assert client.get(f"/api/v1/strategies/{sid}").json()["status"] == "draft"
    finally:
        client.delete(f"/api/v1/strategies/{sid}")


def test_activate_missing_strategy_404(client):
    a = client.post(f"/api/v1/strategies/{uuid.uuid4()}/activate")
    assert a.status_code == 404

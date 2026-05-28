"""Bug #1: custom strategies saved as draft even when status=active submitted.

Proves: create defaults to active, create respects submitted status,
update promotes draft->active, invalid status is rejected 422.
"""
import uuid
import pytest


def _payload(name, **over):
    p = {
        "name": name,
        "description": "pytest fixture strategy",
        "instruments": ["ES"],
        "primary_timeframe": "15m",
        "execution_timeframe": "1m",
        "risk_reward_ratio": 2.0,
        "stop_loss_type": "structure",
    }
    p.update(over)
    return p


def test_create_defaults_to_active(client):
    r = client.post("/api/v1/strategies/", json=_payload(f"pytest-default-{uuid.uuid4().hex[:6]}"))
    assert r.status_code == 201, r.text
    body = r.json()
    try:
        assert body["status"] == "active", f"expected active default, got {body['status']}"
    finally:
        client.delete(f"/api/v1/strategies/{body['id']}")


def test_create_respects_active(client):
    r = client.post("/api/v1/strategies/", json=_payload(f"pytest-active-{uuid.uuid4().hex[:6]}", status="active"))
    assert r.status_code == 201, r.text
    body = r.json()
    try:
        assert body["status"] == "active"
    finally:
        client.delete(f"/api/v1/strategies/{body['id']}")


def test_create_respects_draft(client):
    r = client.post("/api/v1/strategies/", json=_payload(f"pytest-draft-{uuid.uuid4().hex[:6]}", status="draft"))
    assert r.status_code == 201, r.text
    body = r.json()
    try:
        assert body["status"] == "draft", "explicit draft must be respected"
    finally:
        client.delete(f"/api/v1/strategies/{body['id']}")


def test_update_promotes_draft_to_active(client):
    # create as draft
    r = client.post("/api/v1/strategies/", json=_payload(f"pytest-promote-{uuid.uuid4().hex[:6]}", status="draft"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["status"] == "draft"
    try:
        # update to active
        up = client.put(f"/api/v1/strategies/{sid}", json=_payload("pytest-promote-updated", status="active"))
        assert up.status_code == 200, up.text
        assert up.json()["status"] == "active", "PUT draft->active must persist"
        # confirm via GET
        g = client.get(f"/api/v1/strategies/{sid}")
        assert g.status_code == 200
        assert g.json()["status"] == "active"
    finally:
        client.delete(f"/api/v1/strategies/{sid}")


def test_invalid_status_rejected(client):
    r = client.post("/api/v1/strategies/", json=_payload(f"pytest-bad-{uuid.uuid4().hex[:6]}", status="bogus"))
    assert r.status_code == 422, f"invalid status should 422, got {r.status_code}: {r.text}"
    # make sure nothing leaked into the list under that name is not required; 422 means not created

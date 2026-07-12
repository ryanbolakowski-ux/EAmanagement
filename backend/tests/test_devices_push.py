"""Unit tests: iOS device-token registry + APNs push service (2026-07-12).

Pure/unit level by design — no live DB, no network:
  * upsert logic via the pure apply_device_upsert helper
  * APNS_ENABLED=0 short-circuit (must return before any DB import/work)
  * dead-token detection on 410 / BadDeviceToken via httpx.MockTransport
  * APNs payload shape
  * ES256 JWT fallback signer (cryptography is in the image; pyjwt is not)
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.api.routes.devices import apply_device_upsert
from app.services import push as push_svc


NOW = datetime(2026, 7, 12, 14, 0, 0, tzinfo=timezone.utc)
USER_A = uuid.uuid4()
USER_B = uuid.uuid4()


# ── upsert logic (pure helper) ───────────────────────────────────────────

def test_upsert_inserts_when_token_unknown():
    plan = apply_device_upsert(None, user_id=USER_A, token="tok1",
                               platform="ios", now=NOW)
    assert plan["action"] == "insert"
    v = plan["values"]
    assert v["user_id"] == USER_A
    assert v["token"] == "tok1"
    assert v["platform"] == "ios"
    assert v["created_at"] == NOW and v["last_seen_at"] == NOW


def test_upsert_same_user_only_bumps_last_seen():
    existing = SimpleNamespace(user_id=USER_A, token="tok1", platform="ios")
    plan = apply_device_upsert(existing, user_id=USER_A, token="tok1",
                               platform="ios", now=NOW)
    assert plan["action"] == "update"
    assert plan["values"] == {"last_seen_at": NOW}


def test_upsert_reassigns_device_that_changed_hands():
    existing = SimpleNamespace(user_id=USER_A, token="tok1", platform="ios")
    plan = apply_device_upsert(existing, user_id=USER_B, token="tok1",
                               platform="ios", now=NOW)
    assert plan["action"] == "update"
    assert plan["values"]["user_id"] == USER_B
    assert plan["values"]["last_seen_at"] == NOW


def test_upsert_updates_platform_and_defaults_blank_to_ios():
    existing = SimpleNamespace(user_id=USER_A, token="tok1", platform="ios")
    plan = apply_device_upsert(existing, user_id=USER_A, token="tok1",
                               platform="ipados", now=NOW)
    assert plan["values"]["platform"] == "ipados"
    plan2 = apply_device_upsert(None, user_id=USER_A, token="tok2",
                                platform="", now=NOW)
    assert plan2["values"]["platform"] == "ios"


# ── payload shape ────────────────────────────────────────────────────────

def test_apns_payload_shape():
    p = push_svc.build_apns_payload(
        "Saro Pick: BBIO", "Entry $42.10",
        {"ticker": "BBIO", "entry": 42.10, "kind": "theta_pick"})
    assert p["aps"]["alert"] == {"title": "Saro Pick: BBIO",
                                 "body": "Entry $42.10"}
    assert p["aps"]["sound"] == "default"
    assert p["ticker"] == "BBIO" and p["kind"] == "theta_pick"
    json.dumps(p)  # must be JSON-serializable


def test_apns_payload_custom_aps_key_cannot_clobber_alert():
    p = push_svc.build_apns_payload("t", "b", {"aps": {"evil": 1}, "x": 2})
    assert p["aps"]["alert"]["title"] == "t"
    assert "evil" not in p["aps"]
    assert p["x"] == 2


def test_payload_none_is_fine():
    p = push_svc.build_apns_payload("t", "b", None)
    assert set(p.keys()) == {"aps"}


# ── APNS_ENABLED=0 short-circuit ─────────────────────────────────────────

def test_disabled_short_circuits_before_any_db_work(monkeypatch):
    monkeypatch.setenv("APNS_ENABLED", "0")
    # If the short-circuit is broken, this would try DB/session work; make
    # push_to_tokens explode so we notice.
    monkeypatch.setattr(push_svc, "push_to_tokens", None)
    res = asyncio.run(push_svc.send_pick_push([USER_A], "t", "b", {}))
    assert res == {"skipped": "disabled"}


def test_default_env_is_disabled(monkeypatch):
    monkeypatch.delenv("APNS_ENABLED", raising=False)
    assert push_svc.apns_enabled() is False
    res = asyncio.run(push_svc.send_pick_push([USER_A], "t", "b"))
    assert res == {"skipped": "disabled"}


def test_enabled_but_unconfigured_skips(monkeypatch):
    monkeypatch.setenv("APNS_ENABLED", "1")
    for k in ("APNS_KEY_P8", "APNS_KEY_P8_PATH", "APNS_KEY_ID", "APNS_TEAM_ID"):
        monkeypatch.delenv(k, raising=False)
    res = asyncio.run(push_svc.send_pick_push([USER_A], "t", "b"))
    assert res == {"skipped": "unconfigured"}


# ── dead-token deactivation signals (mock transport) ─────────────────────

def _mock_client(responses: dict):
    """httpx client whose transport answers per-token from `responses`
    ({token: (status, reason)})."""
    def handler(request: httpx.Request) -> httpx.Response:
        tok = request.url.path.rsplit("/", 1)[-1]
        status, reason = responses[tok]
        body = {"reason": reason} if reason else {}
        return httpx.Response(status, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_410_marks_token_dead_and_200_does_not():
    client = _mock_client({
        "dead410": (410, "Unregistered"),
        "alive": (200, ""),
        "bad400": (400, "BadDeviceToken"),
        "flaky500": (500, "InternalServerError"),
    })
    res = asyncio.run(push_svc.push_to_tokens(
        ["dead410", "alive", "bad400", "flaky500"], "t", "b",
        {"k": "v"}, client=client, auth_token="test-jwt",
        topic="com.thetaalgos.app", base_url="https://apns.test"))
    assert res["sent"] == 1
    assert res["failed"] == 3
    # 410 and BadDeviceToken deactivate; a 500 must NOT.
    assert sorted(res["dead_tokens"]) == ["bad400", "dead410"]


def test_request_shape_headers_and_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content.decode())
        seen["path"] = request.url.path
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = asyncio.run(push_svc.push_to_tokens(
        ["tokX"], "Title", "Body", {"ticker": "BBIO"},
        client=client, auth_token="jwt123", topic="com.thetaalgos.app",
        base_url="https://apns.test"))
    assert res["sent"] == 1
    assert seen["path"] == "/3/device/tokX"
    assert seen["headers"]["authorization"] == "bearer jwt123"
    assert seen["headers"]["apns-topic"] == "com.thetaalgos.app"
    assert seen["headers"]["apns-push-type"] == "alert"
    assert seen["body"]["aps"]["alert"]["title"] == "Title"
    assert seen["body"]["ticker"] == "BBIO"


def test_transport_exception_is_failed_not_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = asyncio.run(push_svc.push_to_tokens(
        ["tokX"], "t", "b", client=client, auth_token="j",
        base_url="https://apns.test"))
    assert res == {"sent": 0, "failed": 1, "dead_tokens": []}


def test_is_dead_token_response_matrix():
    assert push_svc.is_dead_token_response(410, "")
    assert push_svc.is_dead_token_response(410, "Unregistered")
    assert push_svc.is_dead_token_response(400, "BadDeviceToken")
    assert push_svc.is_dead_token_response(400, "ExpiredToken")
    assert not push_svc.is_dead_token_response(400, "BadTopic")
    assert not push_svc.is_dead_token_response(500, "InternalServerError")
    assert not push_svc.is_dead_token_response(200, "")


# ── ES256 JWT (cryptography fallback — the live path in this image) ─────

def _fresh_ec_key_pem() -> str:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


def test_es256_jwt_shape_and_claims():
    import base64
    pem = _fresh_ec_key_pem()
    tok = push_svc.make_apns_jwt(pem, "KEYID1234", "TEAMID9876", now=1780000000)
    parts = tok.split(".")
    assert len(parts) == 3

    def _dec(seg):
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))

    header = _dec(parts[0])
    claims = _dec(parts[1])
    assert header["alg"] == "ES256" and header["kid"] == "KEYID1234"
    assert claims["iss"] == "TEAMID9876" and claims["iat"] == 1780000000
    # raw JOSE signature is 64 bytes (r||s)
    sig = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    assert len(sig) == 64

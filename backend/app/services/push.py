"""APNs push for Saro picks — iOS companion app (2026-07-12).

TRANSPORT FINDINGS (edge_backend image, checked at build time):
  * httpx 0.27.0 IS installed, but the 'h2' package is NOT — so
    ``httpx.AsyncClient(http2=True)`` raises ImportError at construction.
  * aiohttp 3.13.5 is installed, but aiohttp has NO HTTP/2 client support
    at all, so it can never talk to APNs (which is HTTP/2-only).
  => This module implements the httpx[http2] pattern with a GUARDED import:
     until ``pip install 'httpx[http2]'`` (i.e. the 'h2' package) is added to
     the backend image, every real send logs a clear warning and no-ops.
     Nothing upstream breaks.
  * pyjwt is NOT installed either; cryptography 47.0.0 IS. The APNs auth JWT
    (ES256) therefore tries pyjwt first and falls back to a manual ES256
    signer built directly on cryptography (raw r||s JOSE signature per
    RFC 7515) — the fallback is the live path in this image.

CONFIG (env):
  APNS_ENABLED      "1" to enable. Default "0" — HARD OFF until the .p8 key
                    is uploaded.
  APNS_KEY_P8_PATH  path to the .p8 signing key file, or
  APNS_KEY_P8       the PEM key inline ("\\n" escapes tolerated)
  APNS_KEY_ID       APNs Auth Key ID
  APNS_TEAM_ID      Apple Developer Team ID
  APNS_TOPIC        bundle id (default com.thetaalgos.app)
  APNS_USE_SANDBOX  "1" -> api.sandbox.push.apple.com (default: production)

FAIL-OPEN BY DESIGN: send_pick_push never raises. A 410 response (or a
BadDeviceToken/Unregistered reason) deactivates the token row — the spec
table has no active flag, so "deactivate" means the row is deleted (APNs
guarantees that token will never work again).
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

APNS_HOST_PROD = "https://api.push.apple.com"
APNS_HOST_SANDBOX = "https://api.sandbox.push.apple.com"
DEFAULT_TOPIC = "com.thetaalgos.app"

# APNs reasons that mean "this token is permanently dead".
_DEAD_REASONS = {"BadDeviceToken", "Unregistered", "ExpiredToken",
                 "DeviceTokenNotForTopic"}

# APNs auth tokens are valid 20-60 min; refresh ours after 45.
_JWT_MAX_AGE_S = 45 * 60
_jwt_cache = {"token": None, "iat": 0.0}


# ── config helpers ───────────────────────────────────────────────────────

def apns_enabled() -> bool:
    """Hard gate — default OFF until Ryan uploads the .p8 key."""
    return os.environ.get("APNS_ENABLED", "0") == "1"


def _load_key_pem():
    path = os.environ.get("APNS_KEY_P8_PATH")
    if path:
        try:
            with open(path, "r") as f:
                return f.read()
        except OSError as e:
            logger.warning(f"[push] APNS_KEY_P8_PATH set but unreadable: {e}")
            return None
    inline = os.environ.get("APNS_KEY_P8")
    if inline:
        # Tolerate single-line env values with literal \n escapes.
        return inline.replace("\\n", "\n")
    return None


# ── APNs auth JWT (ES256) ────────────────────────────────────────────────

def _es256_jwt_via_cryptography(key_pem: str, key_id: str, team_id: str,
                                iat: int) -> str:
    """Manual ES256 JWT: cryptography is in the image, pyjwt is not.

    JOSE requires the raw 64-byte r||s signature, not the DER form that
    cryptography's sign() returns (RFC 7515 §A.3).
    """
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    def _b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    header = _b64u(json.dumps({"alg": "ES256", "kid": key_id},
                              separators=(",", ":")).encode())
    claims = _b64u(json.dumps({"iss": team_id, "iat": iat},
                              separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    der_sig = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{_b64u(raw)}"


def make_apns_jwt(key_pem: str, key_id: str, team_id: str, now=None) -> str:
    iat = int(now if now is not None else time.time())
    try:
        import jwt as _pyjwt  # pyjwt — NOT currently in the image (documented)
        return _pyjwt.encode({"iss": team_id, "iat": iat}, key_pem,
                             algorithm="ES256", headers={"kid": key_id})
    except ImportError:
        pass
    return _es256_jwt_via_cryptography(key_pem, key_id, team_id, iat)


def _get_cached_jwt():
    """Build (and cache ~45 min) the APNs bearer token from env config."""
    now = time.time()
    if _jwt_cache["token"] and now - _jwt_cache["iat"] < _JWT_MAX_AGE_S:
        return _jwt_cache["token"]
    key_pem = _load_key_pem()
    key_id = os.environ.get("APNS_KEY_ID")
    team_id = os.environ.get("APNS_TEAM_ID")
    if not (key_pem and key_id and team_id):
        logger.warning("[push] APNs signing config incomplete "
                       "(need APNS_KEY_P8[_PATH] + APNS_KEY_ID + APNS_TEAM_ID)")
        return None
    try:
        token = make_apns_jwt(key_pem, key_id, team_id, now=now)
    except Exception as e:
        logger.warning(f"[push] APNs JWT signing failed: {type(e).__name__}: {e}")
        return None
    _jwt_cache["token"] = token
    _jwt_cache["iat"] = now
    return token


# ── payload / response helpers (pure — unit-tested) ──────────────────────

def build_apns_payload(title: str, body: str, payload=None) -> dict:
    """APNs JSON body: aps.alert + default sound, custom keys at top level.

    A custom key named 'aps' is dropped rather than allowed to clobber the
    alert block.
    """
    out = {"aps": {"alert": {"title": title, "body": body},
                   "sound": "default"}}
    for k, v in (payload or {}).items():
        if k == "aps":
            continue
        out[k] = v
    return out


def is_dead_token_response(status: int, reason: str) -> bool:
    """410 always means gone; BadDeviceToken arrives as 400."""
    return status == 410 or (reason or "") in _DEAD_REASONS


# ── transport (guarded HTTP/2) ───────────────────────────────────────────

def _make_http2_client():
    """httpx AsyncClient with HTTP/2, or None (with a loud warning) if the
    'h2' dependency is missing from the image."""
    try:
        import httpx
    except ImportError as e:  # not the case today, but stay fail-open
        logger.warning(f"[push] httpx not importable ({e}) — iOS push disabled")
        return None
    try:
        return httpx.AsyncClient(http2=True, timeout=10.0)
    except ImportError as e:
        logger.warning(
            "[push] httpx is installed but HTTP/2 support is missing (%s). "
            "APNs requires HTTP/2 — add \"httpx[http2]\" (the 'h2' package) "
            "to the backend image to enable iOS push. Skipping send.", e)
        return None


async def _post_token(client, base_url: str, token: str, headers: dict,
                      body_bytes: bytes):
    resp = await client.post(f"{base_url}/3/device/{token}",
                             headers=headers, content=body_bytes)
    reason = ""
    if resp.status_code != 200:
        try:
            reason = (resp.json() or {}).get("reason", "")
        except Exception:
            reason = (resp.text or "")[:200]
    return resp.status_code, reason


async def push_to_tokens(tokens, title: str, body: str, payload=None, *,
                         client=None, auth_token=None, topic=None,
                         base_url=None) -> dict:
    """Send one alert to each token. Returns
    {"sent": n, "failed": n, "dead_tokens": [...]} and never raises.

    client/auth_token/base_url are injectable for tests (httpx.MockTransport).
    """
    result = {"sent": 0, "failed": 0, "dead_tokens": []}
    if not tokens:
        return result
    if topic is None:
        topic = os.environ.get("APNS_TOPIC", DEFAULT_TOPIC)
    if base_url is None:
        base_url = (APNS_HOST_SANDBOX
                    if os.environ.get("APNS_USE_SANDBOX", "0") == "1"
                    else APNS_HOST_PROD)
    if auth_token is None:
        auth_token = _get_cached_jwt()
        if auth_token is None:
            result["failed"] = len(tokens)
            return result
    headers = {
        "authorization": f"bearer {auth_token}",
        "apns-topic": topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "content-type": "application/json",
    }
    body_bytes = json.dumps(build_apns_payload(title, body, payload)).encode()

    own_client = False
    if client is None:
        client = _make_http2_client()
        if client is None:  # h2 missing — warning already logged
            result["failed"] = len(tokens)
            return result
        own_client = True
    try:
        for tok in tokens:
            try:
                status, reason = await _post_token(client, base_url, tok,
                                                   headers, body_bytes)
            except Exception as e:
                logger.warning(f"[push] APNs send errored for token "
                               f"…{tok[-8:]}: {type(e).__name__}: {e}")
                result["failed"] += 1
                continue
            if status == 200:
                result["sent"] += 1
            else:
                result["failed"] += 1
                if is_dead_token_response(status, reason):
                    result["dead_tokens"].append(tok)
                logger.info(f"[push] APNs {status} reason={reason!r} "
                            f"token=…{tok[-8:]}")
    finally:
        if own_client:
            try:
                await client.aclose()
            except Exception:
                pass
    return result


# ── public entry point ───────────────────────────────────────────────────

async def send_pick_push(user_ids, title: str, body: str, payload=None) -> dict:
    """Push a pick alert to every registered device of the given users.

    Fail-open: never raises; returns a small result dict for logging.
    Short-circuits BEFORE any DB/transport work when APNS_ENABLED != 1.
    """
    try:
        if not apns_enabled():
            logger.debug("[push] APNS_ENABLED != 1 — push skipped")
            return {"skipped": "disabled"}
        user_ids = list(user_ids)
        key_pem = _load_key_pem()
        if not (key_pem and os.environ.get("APNS_KEY_ID")
                and os.environ.get("APNS_TEAM_ID")):
            logger.warning("[push] APNS enabled but signing config incomplete "
                           "— push skipped")
            return {"skipped": "unconfigured"}

        # Lazy imports: keep this module importable in stripped-down test envs.
        from sqlalchemy import select
        from app.database import AsyncSessionLocal
        from app.models.device import DeviceToken

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(DeviceToken).where(
                    DeviceToken.user_id.in_(user_ids))
            )).scalars().all()
            if not rows:
                return {"skipped": "no_tokens"}
            result = await push_to_tokens([r.token for r in rows],
                                          title, body, payload)
            # Deactivate dead rows (410 / BadDeviceToken / Unregistered):
            # the spec table has no active flag, so the row is deleted.
            if result["dead_tokens"]:
                dead = set(result["dead_tokens"])
                for r in rows:
                    if r.token in dead:
                        await session.delete(r)
                await session.commit()
                logger.info(f"[push] removed {len(dead)} dead device token(s)")
            logger.info(f"[push] pick push: sent={result['sent']} "
                        f"failed={result['failed']} users={len(user_ids)}")
            return result
    except Exception as e:
        logger.warning(f"[push] send_pick_push failed open: "
                       f"{type(e).__name__}: {e}")
        return {"error": str(e)}

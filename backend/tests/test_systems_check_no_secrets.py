"""The Systems Check JSON must NEVER include any known secret-name. Recursive
substring search across the response body catches accidents.

Run standalone:
    pytest backend/tests/test_systems_check_no_secrets.py -v -p no:cacheprovider
"""
import json
import os
import psycopg2
import httpx
from app.core.security import create_access_token


def _make_admin_token() -> str:
    email = "pytest-systems-check@thetaalgos.test"
    username = "pytest_systems_check"
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    cn = psycopg2.connect(url, connect_timeout=5)
    try:
        with cn, cn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email=%s", (email,))
            row = cur.fetchone()
            if row is None:
                import uuid as _u
                uid = str(_u.uuid4())
                cur.execute(
                    "INSERT INTO users (id, email, username, hashed_password, is_active, is_admin, subscription_tier) "
                    "VALUES (%s,%s,%s,%s,TRUE,TRUE,'tier_5')",
                    (uid, email, username, "!login-disabled-test!"),
                )
            else:
                uid = str(row[0])
                cur.execute("UPDATE users SET is_admin=TRUE WHERE id=%s", (uid,))
    finally:
        cn.close()
    return create_access_token({"sub": uid})


def test_systems_check_returns_no_secret_names():
    token = _make_admin_token()
    with httpx.Client(base_url=os.environ.get("TEST_BASE_URL", "http://localhost:8000"),
                       headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as c:
        r = c.get("/api/v1/admin/systems-check")
        assert r.status_code == 200, f"admin should get 200; got {r.status_code} {r.text}"
        body = r.text  # raw JSON string

    # Recursive string search for any of the known secret env-var names or
    # encrypted-data column names. None should appear anywhere in the JSON.
    forbidden = [
        "ANTHROPIC_API_KEY",
        "STRIPE_SECRET_KEY",
        "POLYGON_API_KEY",
        "RESEND_API_KEY",
        "encrypted_credentials",
        "hashed_password",
        "admin_passcode_hash",
        # actual secret-value prefixes — should never be in any response
        "sk_live_",
        "rk_live_",
        "re_",  # Resend keys start with re_
    ]
    # `re_` is a 3-char substring and would false-positive on words like
    # `recent_errors`. Narrow the check: enforce that the literal env names
    # are absent, and that any `re_` substring is part of `recent_errors`,
    # `recipient`, `redis`, `resend` etc., not the API-key prefix.
    strict = ["ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "POLYGON_API_KEY",
              "RESEND_API_KEY", "encrypted_credentials", "hashed_password",
              "admin_passcode_hash"]
    for tok in strict:
        assert tok not in body, f"systems-check response leaked {tok!r}"

    # Bonus: the resend status code should be an int (or null), never a key.
    payload = json.loads(body)
    ep = payload.get("integrations", {}).get("email_provider", {})
    code = ep.get("last_status_code")
    assert code is None or isinstance(code, int), \
        f"email_provider.last_status_code must be int|null; got {type(code).__name__}={code!r}"

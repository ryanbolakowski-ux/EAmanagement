"""All 3 admin POST actions (send-test-heartbeat / send-test-trade-email /
run-scanner-health-check) must return 403 for non-admin users.

Run standalone:
    pytest backend/tests/test_admin_test_actions_require_admin.py -v -p no:cacheprovider
"""
def test_non_admin_403_on_test_heartbeat(client):
    r = client.post("/api/v1/admin/send-test-heartbeat")
    assert r.status_code == 403, f"non-admin must be denied; got {r.status_code} {r.text}"


def test_non_admin_403_on_test_trade_email(client):
    r = client.post("/api/v1/admin/send-test-trade-email",
                    json={"asset_class": "stock"})
    assert r.status_code == 403, f"non-admin must be denied; got {r.status_code} {r.text}"


def test_non_admin_403_on_scanner_health_check(client):
    r = client.post("/api/v1/admin/run-scanner-health-check")
    assert r.status_code == 403, f"non-admin must be denied; got {r.status_code} {r.text}"

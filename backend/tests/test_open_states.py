"""Bug 7 — empty/no-account states must return 200, never 500."""


def test_open_positions_empty_is_200(client):
    r = client.get("/api/v1/trades/open-positions")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_completed_backtest_trades_is_200(client):
    # The specific completed backtest from the report; if it is gone in this
    # environment, any 200/404 (not 500) proves the NameError regression is gone.
    r = client.get("/api/v1/backtests/0f48386b-bddc-4873-a44b-8e18687d9eca/trades")
    assert r.status_code in (200, 404), f"must not 500, got {r.status_code}: {r.text}"

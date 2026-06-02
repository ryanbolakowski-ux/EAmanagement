"""Issue 6 verification — GET /api/v1/scanner/criteria must:
  1. Return a non-empty `criteria` list with at least gap_pct, rel_vol,
     catalyst_weight, score formula
  2. Each criterion must have name, rule, rationale
  3. current_pick block must populate from Redis when a pick exists
  4. Endpoint must return 200 OK with no auth setup needed beyond the
     standard get_current_user dependency

Run: pytest backend/tests/test_scanner_criteria.py -v -p no:cacheprovider
"""
import asyncio
import json
import types
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_criteria_endpoint_returns_full_rubric():
    from app.api.routes import scanner as sm

    user = types.SimpleNamespace(id="u-1", email="t@x.com")

    # Stub Redis to return no pick
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    with patch.object(sm, "_r", types.SimpleNamespace(from_url=lambda *_a, **_k: fake_redis)):
        result = _run(sm.scanner_criteria(current_user=user, db=MagicMock()))

    assert "criteria" in result
    c = result["criteria"]
    assert isinstance(c, list) and len(c) >= 6, f"expected 6+ criteria, got {len(c)}"
    # Required fields on each row
    for row in c:
        assert "name" in row and "rule" in row and "rationale" in row
    # Core scoring concepts must be present
    names = {row["name"] for row in c}
    assert "Gap %" in names
    assert "Relative volume" in names
    assert "Score formula" in names
    # No pick today => current_pick is None
    assert result.get("current_pick") is None


def test_criteria_endpoint_includes_current_pick_when_redis_has_one():
    from app.api.routes import scanner as sm

    sample_pick = {
        "ticker": "HLIT", "score": 19.25,
        "gap_pct": 18.2, "rel_vol": 4.5, "today_vol": 5_400_000,
        "catalyst_weight": 1.5, "catalyst_reason": "8-K item 8.01",
        "entry": 12.30, "stop": 11.93, "target": 13.53,
        "picked_at": "2026-06-02T11:00:00Z",
        "alternatives": [{"ticker": "EEIQ", "score": 14.1, "gap_pct": 12.0}],
    }

    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=json.dumps(sample_pick))

    user = types.SimpleNamespace(id="u-1", email="t@x.com")
    with patch.object(sm, "_r",
                      types.SimpleNamespace(from_url=lambda *_a, **_k: fake_redis)):
        result = _run(sm.scanner_criteria(current_user=user, db=MagicMock()))

    cp = result.get("current_pick")
    assert cp is not None, "current_pick should be populated"
    assert cp["ticker"] == "HLIT"
    assert cp["score"] == 19.25
    assert "why_selected" in cp and cp["why_selected"], "missing rationale"
    # The rationale text should mention key numbers
    assert "18.2" in cp["why_selected"] or "18" in cp["why_selected"]
    assert "4.5" in str(cp["why_selected"])

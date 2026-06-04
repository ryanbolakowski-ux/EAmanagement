"""Smoke test: scripts/backfill_stuck_trades.py imports cleanly and
exposes the expected functions, so it won't blow up under
`docker exec ... python -m scripts.backfill_stuck_trades --dry-run`.

Run: pytest backend/tests/test_backfill_stuck_trades_smoke.py -v -p no:cacheprovider
"""
from __future__ import annotations


def test_backfill_script_imports_and_has_main():
    import importlib
    mod = importlib.import_module("scripts.backfill_stuck_trades")
    assert hasattr(mod, "main"), "expected scripts.backfill_stuck_trades.main()"
    assert hasattr(mod, "_apply_backfill"), "expected internal _apply_backfill()"
    assert hasattr(mod, "_fetch_state"), "expected internal _fetch_state()"

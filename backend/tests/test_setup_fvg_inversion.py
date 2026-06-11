"""FVG Inversion Tap — REVERTED TO V1 (user feedback 2026-06-11: the dedicated
port was too tight and dropped the win rate vs the original 85%-WR engine).

The dedicated `FVGInversionTap` is no longer registered, so `get_setup` falls
back to None and the original generic engine (V1) handles this strategy. The
setup class is kept (dormant) for future re-tuning behind a backtest that beats
V1. These tests lock in that revert.
"""
from app.engines.ict import registry as reg
from app.engines.ict.setups.fvg_inversion_tap import FVGInversionTap


def test_inversion_tap_falls_back_to_v1():
    # Registry must NOT return the dedicated setup -> None means "use V1 engine"
    assert reg.get_setup("FVG Inversion Tap") is None
    assert reg.get_setup("FVG Inversion Tap", {}) is None
    # the rule_tree alias must also fall back
    assert reg.get_setup("anything", {"ict_setup": "fvg_inversion_tap"}) is None


def test_other_strategies_unaffected_by_revert():
    # Silver Bullet stays active; PO3/Judas/London were ported in build step 5
    # (they now resolve to dedicated setups). The STILL-unported SMT + NY PM
    # remain on the generic fallback (None).
    assert reg.get_setup("ICT Silver Bullet") is not None
    for name in ("SMT Divergence Reversal", "NY PM Reversal"):
        assert reg.get_setup(name) is None


def test_dormant_class_kept_for_future_reenable():
    # The code is preserved (commented @register) so it can be retuned later.
    s = FVGInversionTap()
    assert hasattr(s, "evaluate")

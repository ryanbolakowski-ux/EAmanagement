"""Scanner V2 fire-window gates (SCANNER-V2, shadow-only). Pure functions.

Grounded in the measured fire-time buckets (docs/v2/01-scanner-forensics.md §5):

  * Pre-market (<9:30) was the BEST bucket (39% WR, +2.00 avg) — but ONLY
    under the old hard $1M premarket dollar-vol gate. CAST (-20%) fired 06:00
    on a thin microcap AFTER that gate was loosened. So a premarket fire
    requires BOTH the hard $1M premkt $-vol floor AND a real catalyst.
  * Open fires (9:30-10:00) measured 17% WR -0.41 avg — allowed only 9:35+
    (past the opening rotation) and only with intraday confirmation.
  * Late fires (>10:00) are 0-for-4, -4.05 avg — the "last-chance whatever's
    best" tier picked the worst names. HARD CLOSE at 10:00, no exceptions,
    no fallback tier.

No V1 import, no network, no clock reads — the caller supplies ET minutes so
every branch is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Window constants (minutes since midnight ET) ────────────────────────────
EARLIEST_FIRE_MIN = 6 * 60        # 06:00 ET — before this the premarket tape is too thin to trust
MARKET_OPEN_MIN = 9 * 60 + 30     # 09:30 ET
RTH_FIRE_OPEN_MIN = 9 * 60 + 35   # 09:35 ET — sit out the opening rotation
HARD_CLOSE_MIN = 10 * 60          # 10:00 ET — measured 0-for-4 -4.05% after this; NO fires at/after

# Hard premarket liquidity floor — the measured protector of the 39%-WR
# premarket bucket. Loosening it (6/26: $1M -> $250k soft) preceded CAST.
PREMARKET_MIN_DOLLAR_VOL = 1_000_000.0


@dataclass(frozen=True)
class FireDecision:
    """Verdict for 'would V2 fire this candidate right now?'."""
    allowed: bool
    window: str   # "premarket" | "rth" | "closed"
    reason: str


def _has_catalyst(candidate: dict) -> bool:
    """Real news catalyst only. The V1 funnel labels bare volume surges
    'high rel-vol gap' — that is rel_vol wearing a costume, not a catalyst
    (and rel_vol is already the dominant score component)."""
    reason = str(candidate.get("catalyst_reason") or "").strip()
    if reason and reason.lower() != "high rel-vol gap":
        return True
    try:
        return float(candidate.get("catalyst_weight") or 1.0) > 1.0
    except (TypeError, ValueError):
        return False


def decide_fire(now_et_minutes: int, candidate: dict) -> FireDecision:
    """Pure fire-window decision. `now_et_minutes` = minutes since midnight ET.

    Missing data fails CLOSED: a candidate with no premarket_dollar_vol
    measurement cannot clear the premarket liquidity floor, and a candidate
    without `confirmed=True` cannot fire in the RTH window. We never fire on
    fabricated liquidity or assumed confirmation.
    """
    t = int(now_et_minutes)

    # HARD CLOSE — no last-chance tier, no fires at/after 10:00 ET.
    if t >= HARD_CLOSE_MIN:
        return FireDecision(False, "closed",
                            "hard close: no fires at/after 10:00 ET "
                            "(late fires measured 0-for-4, -4.05% avg)")

    if t < EARLIEST_FIRE_MIN:
        return FireDecision(False, "closed", "before 06:00 ET: premarket tape too thin")

    # ── Pre-market window 06:00-09:29 — hard liquidity floor AND catalyst ──
    if t < MARKET_OPEN_MIN:
        try:
            pm_dv = float(candidate.get("premarket_dollar_vol") or 0.0)
        except (TypeError, ValueError):
            pm_dv = 0.0
        if pm_dv < PREMARKET_MIN_DOLLAR_VOL:
            return FireDecision(False, "premarket",
                                f"premkt $-vol ${pm_dv:,.0f} < ${PREMARKET_MIN_DOLLAR_VOL:,.0f} "
                                "hard floor (thin premarket microcap — the CAST failure mode)")
        if not _has_catalyst(candidate):
            return FireDecision(False, "premarket",
                                "no news catalyst: premarket fires require liquidity AND catalyst")
        return FireDecision(True, "premarket",
                            f"premkt $-vol ${pm_dv / 1e6:.2f}M >= $1M floor + catalyst present")

    # ── Opening rotation 09:30-09:34 — stand down ───────────────────────────
    if t < RTH_FIRE_OPEN_MIN:
        return FireDecision(False, "rth", "opening rotation 09:30-09:35: wait for confirmation window")

    # ── RTH window 09:35-09:59 — confirmation required ──────────────────────
    if not bool(candidate.get("confirmed")):
        return FireDecision(False, "rth",
                            "RTH window requires intraday confirmation (above VWAP + continuation)")
    return FireDecision(True, "rth", "09:35-10:00 window, intraday confirmation present")

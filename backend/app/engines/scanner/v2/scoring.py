"""Scanner V2 scoring (SCANNER-V2, shadow-only).

Rebuilt from MEASURED feature->outcome relationships (docs/v2/
01-scanner-forensics.md, n=41 resolved picks + 130 shadow rows) — the V1
composite is anti-predictive (score>=40 avg -1.20%/pick vs score<25 +1.91%),
so V2 weights only what the data supports:

  * rel_vol is THE positive signal (winners 57.5x vs losers 21.1x) — dominant
    weight, log-scaled so a 500x print doesn't drown everything else.
  * gap size is mildly NEGATIVE at the tail (winners 16.9% vs losers 19.5%;
    big gaps mean-revert) — a capped piecewise curve, never a linear bonus.
  * cheap high-RVOL names carry the edge (under-$10: 35% WR +1.76 avg;
    over-$30: 0W-4L -2.93) — price band curve, sub-$3 penalized, NO hard floor
    (tail risk like CAST is controlled by liquidity + the fire gates, not price).
  * the $1M premarket dollar-vol floor protected the 39%-WR premarket bucket;
    CAST (-20%) happened after it was loosened — liquidity_quality leans on it.

Every component contributes raw in [0,1]; weights sum to 100 so the total is
naturally 0-100. When a feature's data is missing we NEVER fabricate it — the
component sits at a neutral 0.5, is flagged `neutral`, and why() says so.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ── Feature weights ─────────────────────────────────────────────────────────
# Sum = 100.0 → total score is sum(raw01 * weight), i.e. naturally 0-100.
# TODO(recalibrate): these are the forensics-grounded PRIORS. Refit them from
# measured shadow-v2 outcomes once the v2:* forward-test cohort reaches n>=30
# resolved rows (same gate the templates use) — do not hand-tune before that.
WEIGHTS: dict = {
    "rel_vol": 40.0,            # winners 57.5x vs losers 21.1x — the real signal, dominant
    "gap_quality": 16.0,        # optimal 3-15%; decay >20%; strong penalty >30% (mean-reversion)
    "liquidity_quality": 14.0,  # premkt $-vol vs the $1M floor + session $-vol (CAST lesson)
    "price_band": 8.0,          # edge lives under $10; over-$30 measured 0W-4L; sub-$3 penalized
    "rs_vs_qqq": 8.0,           # relative strength vs index (best shadow template family)
    "catalyst": 8.0,            # 8-K catalyst weight passthrough (same feed V1 uses)
    "regime": 6.0,              # QQQ above prev close = tailwind for long momentum
}

_NEUTRAL = 0.5  # contribution when a feature's data is unavailable (never fabricated)


@dataclass
class ScoreBreakdownV2:
    """0-100 total + per-component breakdown. `neutral: True` on a component
    means its data was missing and it contributed the flat 0.5 — surfaced in
    why() so a human reading the row knows what was actually measured."""
    total: float
    components: dict  # name -> {"raw": float, "weighted": float, "note": str, "neutral": bool}

    def why(self, k: int = 4) -> str:
        ranked = sorted(self.components.items(), key=lambda kv: kv[1]["weighted"], reverse=True)
        parts = [f"{n}: {v['note']} (+{v['weighted']:.1f})" for n, v in ranked[:k]]
        neutrals = sorted(n for n, v in self.components.items() if v.get("neutral"))
        if neutrals:
            parts.append("neutral (no data): " + ", ".join(neutrals))
        return " · ".join(parts)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _piecewise(x: float, points: list) -> float:
    """Linear interpolation over sorted (x, y) breakpoints; clamps at the ends."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


# ── Component curves (pure, unit-testable) ──────────────────────────────────

def rel_vol_raw(rv: float) -> float:
    """Log-scaled: 1x→0.0, 10x→0.5, 100x+→1.0. Winners avg 57.5x lands ~0.88,
    losers avg 21.1x ~0.66 — separation without letting one 500x print saturate."""
    try:
        rv = float(rv)
    except (TypeError, ValueError):
        return 0.0
    if rv <= 1.0:
        return 0.0
    return _clip(math.log10(rv) / 2.0)


# Gap% curve: small green gaps ramp up, 3-15% is the measured sweet spot,
# decay above 20%, strong penalty >30% (big gaps mean-revert — forensics §4).
_GAP_CURVE = [(0.0, 0.35), (3.0, 1.0), (15.0, 1.0), (20.0, 0.55), (30.0, 0.15), (45.0, 0.05)]


def gap_quality_raw(gap_pct: float) -> float:
    try:
        g = float(gap_pct)
    except (TypeError, ValueError):
        return _NEUTRAL
    if g < 0.0:
        return 0.25  # red gap: not the long-momentum pattern (soft, not a reject)
    return _piecewise(g, _GAP_CURVE)


# Price band: NO hard floor — sub-$3 penalized (thin pump territory), $4-10 is
# the measured edge zone, decay through $30 where the measured record is 0W-4L.
_PRICE_CURVE = [(0.0, 0.05), (3.0, 0.5), (4.0, 1.0), (10.0, 1.0), (30.0, 0.4), (100.0, 0.2)]


def price_band_raw(price: float) -> float:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return _NEUTRAL
    if p <= 0:
        return 0.0
    return _piecewise(p, _PRICE_CURVE)


def liquidity_quality_raw(premarket_dollar_vol, session_dollar_vol):
    """Blend of premarket $-vol (measured protector — the old hard $1M floor)
    and session $-vol. Returns (raw01 or None, note); None means BOTH inputs
    were missing → caller uses the neutral contribution."""
    pm = None if premarket_dollar_vol is None else max(0.0, float(premarket_dollar_vol))
    sess = None if session_dollar_vol is None else max(0.0, float(session_dollar_vol))
    if pm is None and sess is None:
        return None, "no $-vol data"
    # $1M premkt = 0.5, $2M+ = 1.0 — the floor sits mid-scale so being AT it
    # is unremarkable and being under it visibly drags the score.
    pm_raw = None if pm is None else _clip(pm / 2_000_000.0)
    sess_raw = None if sess is None else _clip(sess / 50_000_000.0)
    if pm_raw is None:
        return sess_raw, f"session ${sess / 1e6:.1f}M (no premkt data)"
    if sess_raw is None:
        return pm_raw, f"premkt ${pm / 1e6:.2f}M"
    return (0.6 * pm_raw + 0.4 * sess_raw), f"premkt ${pm / 1e6:.2f}M · session ${sess / 1e6:.1f}M"


# ── The scorer ──────────────────────────────────────────────────────────────

def score_v2(candidate: dict, context: dict) -> ScoreBreakdownV2:
    """Rank a funnel candidate 0-100 under the V2 weights.

    `candidate` is a stage-1 coarse dict (ticker/price/gap_pct/rel_vol/
    dollar_vol), optionally enriched with premarket_dollar_vol, day_pct,
    catalyst_weight/catalyst_reason. `context` is market-wide state
    (qqq_day_pct, qqq_above_prev_close) — every context field is optional and
    a missing field is a NEUTRAL contribution, never a guess.
    """
    comp: dict = {}

    def add(name: str, raw01: float, note: str, neutral: bool = False):
        raw01 = _clip(float(raw01))
        comp[name] = {
            "raw": round(raw01, 3),
            "weighted": round(raw01 * WEIGHTS[name], 2),
            "note": note,
            "neutral": neutral,
        }

    # rel_vol — dominant, log-scaled
    rv = candidate.get("rel_vol")
    if rv is None:
        add("rel_vol", _NEUTRAL, "neutral (no rel-vol data)", neutral=True)
    else:
        add("rel_vol", rel_vol_raw(rv), f"{float(rv):.1f}x rel-vol")

    # gap_quality — capped piecewise curve, never a linear bonus
    gp = candidate.get("gap_pct")
    if gp is None:
        add("gap_quality", _NEUTRAL, "neutral (no gap data)", neutral=True)
    else:
        add("gap_quality", gap_quality_raw(gp), f"gap {float(gp):+.1f}%")

    # liquidity_quality — premkt $-vol vs the $1M floor + session $-vol
    liq_raw, liq_note = liquidity_quality_raw(
        candidate.get("premarket_dollar_vol"), candidate.get("dollar_vol"))
    if liq_raw is None:
        add("liquidity_quality", _NEUTRAL, "neutral (no $-vol data)", neutral=True)
    else:
        add("liquidity_quality", liq_raw, liq_note)

    # price_band — no hard floor, sub-$3 penalized
    pr = candidate.get("price")
    if pr is None:
        add("price_band", _NEUTRAL, "neutral (no price)", neutral=True)
    else:
        add("price_band", price_band_raw(pr), f"${float(pr):.2f}")

    # rs_vs_qqq — candidate day% minus QQQ day%; neutral when context lacks QQQ
    qqq_pct = context.get("qqq_day_pct")
    day_pct = candidate.get("day_pct", candidate.get("gap_pct"))
    if qqq_pct is None or day_pct is None:
        add("rs_vs_qqq", _NEUTRAL, "neutral (no QQQ context)", neutral=True)
    else:
        rs = float(day_pct) - float(qqq_pct)
        # +-10% relative move spans the scale; 0 relative strength = 0.5
        add("rs_vs_qqq", _clip(0.5 + rs / 20.0), f"{rs:+.1f}% vs QQQ")

    # catalyst — passthrough of the 8-K catalyst weight (1.0 == none)
    cw = candidate.get("catalyst_weight")
    if cw is None:
        add("catalyst", _NEUTRAL, "neutral (catalyst not checked)", neutral=True)
    else:
        cw = float(cw)
        if cw > 1.0:
            add("catalyst", _clip(0.35 + (cw - 1.0) * 1.3),
                candidate.get("catalyst_reason") or f"catalyst x{cw:.2f}")
        else:
            add("catalyst", 0.35, "no catalyst")

    # regime — QQQ above prev close = tailwind; neutral when context absent
    regime = context.get("qqq_above_prev_close")
    if regime is None:
        add("regime", _NEUTRAL, "neutral (no regime context)", neutral=True)
    else:
        add("regime", 1.0 if regime else 0.2,
            "QQQ above prev close" if regime else "QQQ below prev close")

    total = round(sum(v["weighted"] for v in comp.values()), 1)
    return ScoreBreakdownV2(total=_clip(total, 0.0, 100.0), components=comp)

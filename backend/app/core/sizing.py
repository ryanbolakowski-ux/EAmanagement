"""Unified position sizing — single source of truth for "min-of" sizing
(#136). Pure functions, no I/O.

`unified_size()` computes the position size as the MINIMUM across every
configured constraint (risk-per-trade $/% , max_position_usd, allocation_usd,
account cash / buying-power, and a contract/share cap), and reports WHICH
constraint bound the result so a pre-entry preview can show the user exactly
what will be placed and why.

This module is intentionally additive: it does not change any engine's existing
sizing behaviour. Engines can migrate to it incrementally; the read-only
pre-entry preview endpoint uses it immediately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Constraint:
    name: str
    limit_value: Optional[float]      # the cap's configured value (None = not set)
    would_size_to: Optional[float]    # units this constraint alone would allow
    applied: bool = False             # True if this constraint bound the final size


@dataclass
class SizingResult:
    final_size: int
    risk_per_unit: float              # $ risked per contract/share if stop hits
    final_notional_usd: float
    actual_risk_usd: float
    intended_risk_usd: float
    risk_model: str                   # which sizing intent won: allocation/risk_usd/risk_pct/default_pct
    binding_constraint: str           # name of the constraint that set final_size
    constraints: list[Constraint] = field(default_factory=list)
    summary: str = ""
    ok: bool = True
    reason: str = ""                  # populated when final_size == 0


def _floor_int(x: float) -> int:
    try:
        return int(x) if x and x > 0 else 0
    except Exception:
        return 0


def unified_size(
    *,
    entry_price: float,
    stop_loss: float,
    # sizing intent (priority: allocation_usd > risk_per_trade_usd > risk_per_trade_pct > default 1% of equity)
    allocation_usd: Optional[float] = None,
    risk_per_trade_usd: Optional[float] = None,
    risk_per_trade_pct: Optional[float] = None,
    account_equity: Optional[float] = None,
    # caps (min-of)
    max_position_usd: Optional[float] = None,
    max_units: Optional[int] = None,          # contracts (futures/options) or shares
    cached_cash: Optional[float] = None,
    cached_buying_power: Optional[float] = None,
    account_type: str = "cash",               # "cash" | "margin"
    # instrument economics
    point_value: float = 1.0,                 # $ per 1.0 of price move per unit (stock=1, NQ=20, ES=50, option=100)
    commission_per_unit: float = 0.0,         # one-way; round-trip = 2x
    default_pct: float = 1.0,                 # fallback % of equity when no intent given
    symbol: str = "",
) -> SizingResult:
    """Return the min-of position size with full constraint transparency.

    `point_value` is $ P&L per 1.0 of price move for ONE unit (so risk per unit
    = |entry-stop| * point_value + round-trip commission). For stocks
    point_value=1 (1 share moves $ per $1). For NQ point_value=20, ES=50, an
    option point_value=100 (premium points × 100 multiplier)."""
    entry = float(entry_price or 0.0)
    stop = float(stop_loss or 0.0)
    if entry <= 0 or stop <= 0 or entry == stop:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, "none", "invalid_inputs",
                            summary="Invalid entry/stop.", ok=False, reason="invalid_inputs")

    risk_per_unit = abs(entry - stop) * float(point_value) + 2.0 * float(commission_per_unit or 0.0)
    if risk_per_unit <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, "none", "invalid_risk",
                            summary="Risk per unit is zero.", ok=False, reason="invalid_risk")
    notional_per_unit = entry * float(point_value)

    # ── Sizing intent (the $ budget we try to size to) ──
    eq = float(account_equity) if account_equity is not None else None
    if allocation_usd is not None and allocation_usd > 0:
        intended_risk = float(allocation_usd); risk_model = "allocation_usd"
        # allocation sizes by NOTIONAL, not risk — handle separately below
    elif risk_per_trade_usd is not None and risk_per_trade_usd > 0:
        intended_risk = float(risk_per_trade_usd); risk_model = "risk_per_trade_usd"
    elif risk_per_trade_pct is not None and risk_per_trade_pct > 0 and eq:
        intended_risk = eq * float(risk_per_trade_pct) / 100.0; risk_model = "risk_per_trade_pct"
    elif eq:
        intended_risk = eq * float(default_pct) / 100.0; risk_model = f"default_{default_pct}pct"
    else:
        return SizingResult(0, risk_per_unit, 0.0, 0.0, 0.0, "none", "no_sizing_basis",
                            summary="No allocation, risk, or equity provided.", ok=False, reason="no_sizing_basis")

    cons: list[Constraint] = []

    # The base intent → units.
    if risk_model == "allocation_usd":
        base_units = allocation_usd / notional_per_unit if notional_per_unit > 0 else 0
        cons.append(Constraint("allocation_usd", allocation_usd, base_units))
    else:
        base_units = intended_risk / risk_per_unit
        cons.append(Constraint(risk_model, intended_risk, base_units))

    # Caps (each expressed as a unit ceiling).
    if max_position_usd is not None and max_position_usd > 0 and notional_per_unit > 0:
        cons.append(Constraint("max_position_usd", max_position_usd, max_position_usd / notional_per_unit))
    if max_units is not None and max_units > 0:
        cons.append(Constraint("max_units", float(max_units), float(max_units)))
    # Capital: cash account is bounded by cash; margin by buying power.
    if account_type == "cash" and cached_cash is not None and cached_cash > 0 and notional_per_unit > 0:
        cons.append(Constraint("cash", cached_cash, cached_cash / notional_per_unit))
    elif cached_buying_power is not None and cached_buying_power > 0 and notional_per_unit > 0:
        cons.append(Constraint("buying_power", cached_buying_power, cached_buying_power / notional_per_unit))

    # Min-of: the smallest unit-ceiling wins.
    binding = min(cons, key=lambda c: (c.would_size_to if c.would_size_to is not None else float("inf")))
    final_units = _floor_int(binding.would_size_to or 0)
    for c in cons:
        c.applied = (c is binding)

    if final_units <= 0:
        return SizingResult(
            0, risk_per_unit, 0.0, 0.0, intended_risk, risk_model, binding.name, cons,
            summary=f"Cannot size {symbol or 'position'}: {binding.name} allows <1 unit.",
            ok=False, reason=f"{binding.name}_below_one_unit")

    actual_risk = final_units * risk_per_unit
    notional = final_units * notional_per_unit
    summary = (f"Size {final_units} {symbol or 'unit(s)'} @ {entry:g} "
               f"(~${notional:,.0f} notional, ${actual_risk:,.0f} risk if stop hits). "
               f"Bound by {binding.name}.")
    return SizingResult(final_units, risk_per_unit, notional, actual_risk, intended_risk,
                        risk_model, binding.name, cons, summary=summary, ok=True)


def rr_ratio(entry: float, stop: float, target: Optional[float]) -> Optional[float]:
    """Reward:risk based on price distances. None if target/stop invalid."""
    try:
        if not target or entry == stop:
            return None
        risk = abs(entry - stop)
        reward = abs(target - entry)
        return round(reward / risk, 2) if risk > 0 else None
    except Exception:
        return None

"""Strike & expiration selector.

Given:
  • current spot price for the underlying
  • a target delta band (e.g. 0.30 - 0.50)
  • a DTE band (e.g. 30 - 60)
  • side (call / put)

…returns the single best `OptionContract` to trade. Selection rules:
  1. Filter the chain to contracts whose expiration is inside [min_dte, max_dte].
  2. For each candidate, compute the model delta using Black-Scholes against
     an estimated IV (either supplied or assumed from `default_iv`).
  3. Among contracts whose model delta lands inside [delta_min, delta_max],
     pick the one closest to the midpoint of the band.
  4. If no contract fits the band, return the contract whose delta is closest
     to the midpoint anyway — but tag the return as `band_missed=True` so
     the caller can decide whether to skip the trade.

For verticals (debit spreads), pass `width` and we'll also return the short-leg
contract `width` strikes away in the same expiration."""
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.engines.options.pricing import greeks, OptionType
from app.engines.options.polygon_options import OptionContract


@dataclass
class StrikePick:
    long: OptionContract
    short: Optional[OptionContract]   # set for vertical spreads
    target_delta: float
    actual_delta: float
    days_to_expiration: int
    band_missed: bool                 # True if no contract fit the delta band
    estimated_iv: float
    reason: str                        # plain-english explanation


def _days_to(d: date, today: date) -> int:
    return (d - today).days


def pick_strike(chain: list[OptionContract], spot: float, today: date,
                 side: OptionType,
                 delta_min: float = 0.30, delta_max: float = 0.50,
                 dte_min: int = 30, dte_max: int = 60,
                 default_iv: float = 0.30,
                 risk_free_rate: float = 0.045,
                 dividend_yield: float = 0.0,
                 prefer_itm: bool = False,
                 spread_width: Optional[int] = None) -> Optional[StrikePick]:
    """Pick the best contract from the chain. Returns None when the filtered
    chain is empty (e.g. nothing in the DTE band at all)."""
    # Restrict to the right side
    chain = [c for c in chain if c.right == side]
    # Restrict to the DTE band
    chain = [c for c in chain if dte_min <= _days_to(c.expiration, today) <= dte_max]
    if not chain:
        return None

    # When `prefer_itm` is on, shift the target band higher (more in-the-money
    # = higher delta). This is the user-provided framework's "safer leverage"
    # mode where you trade 0.55-0.65 deltas instead of 0.30-0.50.
    if prefer_itm:
        delta_min = max(delta_min, 0.55)
        delta_max = min(0.85, delta_max + 0.20)

    target = (delta_min + delta_max) / 2

    # Compute model delta for each candidate. We use `default_iv` because we
    # don't have live IV from the free Polygon tier — for backtest we'd
    # solve IV from the historical bar; for paper we assume a sensible IV.
    scored: list[tuple[float, OptionContract, float, int]] = []
    for c in chain:
        dte = _days_to(c.expiration, today)
        t   = max(1, dte) / 365.0
        g = greeks(s=spot, k=c.strike, t=t, sigma=default_iv,
                    r=risk_free_rate, q=dividend_yield, opt_type=side)
        d = abs(g.delta) if side == "put" else g.delta
        scored.append((d, c, g.delta, dte))

    # Within band first
    in_band = [s for s in scored if delta_min <= s[0] <= delta_max]
    if in_band:
        # Pick the one closest to target midpoint
        best = min(in_band, key=lambda s: abs(s[0] - target))
        band_missed = False
        reason = f"Picked {best[1].right.upper()} {best[1].strike:.0f}EXP{best[1].expiration} at delta {best[2]:+.2f} ({best[3]}DTE) — inside target band {delta_min:.2f}-{delta_max:.2f}"
    else:
        # Fall back to nearest to target
        best = min(scored, key=lambda s: abs(s[0] - target))
        band_missed = True
        reason = f"No contract inside delta band {delta_min:.2f}-{delta_max:.2f}; nearest is {best[1].right.upper()} {best[1].strike:.0f}EXP{best[1].expiration} at delta {best[2]:+.2f}"

    long_contract = best[1]
    short_contract = None

    # Optional vertical-spread short leg
    if spread_width and spread_width > 0:
        target_short_strike = (long_contract.strike + spread_width) if side == "call" else (long_contract.strike - spread_width)
        same_exp = [c for c in chain if c.expiration == long_contract.expiration]
        if same_exp:
            short_contract = min(same_exp, key=lambda c: abs(c.strike - target_short_strike))
            if short_contract.ticker == long_contract.ticker:
                short_contract = None  # didn't find a distinct strike

    return StrikePick(
        long=long_contract,
        short=short_contract,
        target_delta=target,
        actual_delta=best[2],
        days_to_expiration=best[3],
        band_missed=band_missed,
        estimated_iv=default_iv,
        reason=reason,
    )

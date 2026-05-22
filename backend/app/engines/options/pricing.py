"""Black-Scholes pricing for European-style equity options.

For US listed options (which are technically American), early-exercise premium
is small for non-dividend names and negligible far from expiry. Black-Scholes
is accurate enough for strategy backtesting and paper trading — when we go
live with a real broker, we use the broker's quote, not our model.

All math operates on `t = days_to_expiration / 365`. Volatilities are
expressed as decimal fractions (0.20 = 20% IV). Risk-free rate `r` defaults
to 4.5% — pulled from the FRED 3-month T-bill yield. Dividend yield `q`
defaults to zero (it's small enough to ignore for short-dated options on
non-divvy names; pass it explicitly for SPY/QQQ etc).
"""
import math
from dataclasses import dataclass
from typing import Literal

OptionType = Literal["call", "put"]

# Cached so we don't allocate erf/ndtr lookups per call
SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the math.erf-based identity."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


@dataclass
class GreeksResult:
    price: float          # mid theoretical price per share (×100 = per contract)
    delta: float
    gamma: float
    theta: float          # per CALENDAR day (already divided by 365)
    vega: float           # per 1% absolute IV move
    rho: float            # per 1% absolute rate move


def _d1_d2(s: float, k: float, t: float, r: float, q: float, sigma: float):
    if sigma <= 0 or t <= 0:
        # Degenerate — option is at intrinsic
        return None, None
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / vol_t
    d2 = d1 - vol_t
    return d1, d2


def price(s: float, k: float, t: float, sigma: float,
          r: float = 0.045, q: float = 0.0,
          opt_type: OptionType = "call") -> float:
    """Black-Scholes-Merton theoretical price.

    s     spot
    k     strike
    t     time to expiry in years (days/365)
    sigma annualised implied volatility (0.20 = 20%)
    r     risk-free rate (annualised, decimal)
    q     continuous dividend yield (annualised, decimal)
    opt_type "call" or "put"
    """
    if t <= 0:
        intrinsic = max(0.0, s - k) if opt_type == "call" else max(0.0, k - s)
        return intrinsic
    d1, d2 = _d1_d2(s, k, t, r, q, sigma)
    if d1 is None:
        return max(0.0, (s - k) if opt_type == "call" else (k - s))
    if opt_type == "call":
        return s * math.exp(-q * t) * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    else:
        return k * math.exp(-r * t) * _norm_cdf(-d2) - s * math.exp(-q * t) * _norm_cdf(-d1)


def greeks(s: float, k: float, t: float, sigma: float,
           r: float = 0.045, q: float = 0.0,
           opt_type: OptionType = "call") -> GreeksResult:
    """Compute price + delta/gamma/theta/vega/rho. Theta is per-day; vega
    and rho are per-1%-absolute (so vega = 0.10 means the option price moves
    $0.10 per share when IV moves from 20% to 21%)."""
    if t <= 0 or sigma <= 0:
        # Degenerate — return intrinsic price and zero greeks (except delta which is binary)
        intrinsic = max(0.0, s - k) if opt_type == "call" else max(0.0, k - s)
        delta = 1.0 if (opt_type == "call" and s > k) else (
                -1.0 if (opt_type == "put" and s < k) else 0.0)
        return GreeksResult(price=intrinsic, delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    d1, d2 = _d1_d2(s, k, t, r, q, sigma)
    pdf_d1 = _norm_pdf(d1)

    if opt_type == "call":
        p     = s * math.exp(-q * t) * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
        delta = math.exp(-q * t) * _norm_cdf(d1)
        theta_ann = (- (s * math.exp(-q * t) * pdf_d1 * sigma) / (2 * math.sqrt(t))
                     - r * k * math.exp(-r * t) * _norm_cdf(d2)
                     + q * s * math.exp(-q * t) * _norm_cdf(d1))
        rho_ann = k * t * math.exp(-r * t) * _norm_cdf(d2)
    else:
        p     = k * math.exp(-r * t) * _norm_cdf(-d2) - s * math.exp(-q * t) * _norm_cdf(-d1)
        delta = -math.exp(-q * t) * _norm_cdf(-d1)
        theta_ann = (- (s * math.exp(-q * t) * pdf_d1 * sigma) / (2 * math.sqrt(t))
                     + r * k * math.exp(-r * t) * _norm_cdf(-d2)
                     - q * s * math.exp(-q * t) * _norm_cdf(-d1))
        rho_ann = -k * t * math.exp(-r * t) * _norm_cdf(-d2)

    gamma = math.exp(-q * t) * pdf_d1 / (s * sigma * math.sqrt(t))
    vega  = s * math.exp(-q * t) * pdf_d1 * math.sqrt(t) / 100.0   # per 1%
    theta_day = theta_ann / 365.0                                   # per day
    rho       = rho_ann   / 100.0                                   # per 1%

    return GreeksResult(price=p, delta=delta, gamma=gamma, theta=theta_day,
                        vega=vega, rho=rho)


def implied_vol(market_price: float, s: float, k: float, t: float,
                r: float = 0.045, q: float = 0.0,
                opt_type: OptionType = "call",
                iv_floor: float = 0.01, iv_ceiling: float = 5.0,
                max_iter: int = 50, tol: float = 1e-5) -> float:
    """Solve for IV given the option's market price. Newton-Raphson with a
    bisection fallback when Newton steps fly out of the [floor, ceiling]
    band — which happens for very deep ITM / OTM contracts where vega is
    near zero. Returns the IV that reproduces the market price under BS."""
    if t <= 0 or market_price <= 0:
        return iv_floor

    # Sanity check: price must be at least intrinsic (no-arb)
    intrinsic = max(0.0, (s - k) if opt_type == "call" else (k - s))
    if market_price < intrinsic - 1e-6:
        return iv_floor

    sigma = 0.30  # initial guess — reasonable for most equities
    lo, hi = iv_floor, iv_ceiling

    for _ in range(max_iter):
        g = greeks(s, k, t, sigma, r, q, opt_type)
        diff = g.price - market_price
        if abs(diff) < tol:
            return max(iv_floor, min(iv_ceiling, sigma))
        # vega is per 1% — convert to per-unit (×100) for Newton step
        v = g.vega * 100.0
        if v < 1e-8:
            # Vega too small; switch to bisection
            if diff > 0:
                hi = sigma
            else:
                lo = sigma
            sigma = (lo + hi) / 2
            continue
        new_sigma = sigma - diff / v
        if new_sigma <= iv_floor or new_sigma >= iv_ceiling or not math.isfinite(new_sigma):
            # Out of band — bisect instead
            if diff > 0:
                hi = sigma
            else:
                lo = sigma
            sigma = (lo + hi) / 2
        else:
            sigma = new_sigma

    return max(iv_floor, min(iv_ceiling, sigma))

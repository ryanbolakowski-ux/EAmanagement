"""Classify a strategy as futures / options / stock by looking at its
``instruments`` list, and decide whether a given broker can execute that
asset class.

The DB has no ``asset_class`` column on ``strategies`` — instead the asset
class is implicit in the symbols a user picked when building the strategy.
The live-deploy UI and the live-session validation both need a single source
of truth for that derivation, so the rules live here.

Design notes:
    * 'futures' wins over 'stock' if any instrument is a known futures
      root (ES/NQ/MES/CL/…), because dropping a futures-rooted strategy
      onto a stock account is the dangerous miscategorization we are
      explicitly trying to prevent.
    * 'options' is detected via OCC option-symbol formatting (root +
      YYMMDD + C/P + 8-digit strike). Today the DB has no OCC symbols,
      but the rule is here so options-strategy flows already work the
      moment someone authors one.
    * 'unknown' is reserved for an empty ``instruments`` list — those are
      template strategies and are not deployable to a live broker.
"""

import re

# Major US futures roots (CME group + a few common others) plus their micros.
# Kept as a frozenset so look-ups are O(1).
FUTURES_SYMBOLS = frozenset({
    # Equity index
    "ES", "NQ", "RTY", "YM",
    "MES", "MNQ", "M2K", "MYM",
    # Energy
    "CL", "NG", "RB", "HO", "MCL",
    # Metals
    "GC", "SI", "HG", "PL", "MGC", "SIL",
    # Treasuries
    "ZB", "ZN", "ZF", "ZT", "UB",
    # FX (alphabetic CME roots — not the ISO pairs)
    "6E", "6J", "6B", "6A", "6C", "6S", "6N",
    # Ag
    "ZC", "ZS", "ZW", "ZL", "ZM",
    # Crypto
    "BTC", "MBT", "ETH", "MET",
})

# OCC 21-character option symbol: 1-6 char root + YYMMDD + C/P + 8-digit strike.
# Example: SPY240517C00500000 → SPY, expires 2024-05-17, call, strike $500.00.
OCC_OPTION_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def classify_asset_class(instruments) -> str:
    """Return ``'futures'`` / ``'options'`` / ``'stock'`` / ``'unknown'``.

    ``'unknown'`` indicates an empty or all-blank ``instruments`` list — those
    are template strategies and the caller should treat them as undeployable.

    The check is intentionally order-sensitive: options win over futures win
    over stock. If a strategy mixes ES with SPY in one list, the stricter
    futures rule is the safer default — that strategy should not deploy to a
    stock-only broker.
    """
    if not instruments:
        return "unknown"
    # Normalize first, then drop any that became empty after stripping.
    # This lets us treat ['', '  ', None] the same as [].
    syms = [str(s).upper().strip() for s in instruments if s]
    syms = [s for s in syms if s]
    if not syms:
        return "unknown"
    if any(OCC_OPTION_RE.match(s) for s in syms):
        return "options"
    if any(s in FUTURES_SYMBOLS for s in syms):
        return "futures"
    return "stock"


# Which asset classes each broker can route. The list is conservative — we
# only enable a class once we have a verified path through to the broker.
# Schwab and IBKR are listed for forward compatibility; today only Tradier
# and Tradovate are wired end-to-end on the prod stack.
BROKER_ASSET_CLASSES = {
    "tradier":   ["stock", "options"],
    "alpaca":    ["stock", "options"],
    "tradovate": ["futures"],
    "schwab":    ["stock", "options", "futures"],
    "webull":    ["stock", "options"],
    "ibkr":      ["stock", "options", "futures"],
}


def broker_supports(broker: str, asset_class: str) -> bool:
    """Return True if the given broker can execute the given asset class."""
    return asset_class in BROKER_ASSET_CLASSES.get((broker or "").lower(), [])


def supported_classes(broker: str) -> list:
    """Convenience for error messages: the list of asset classes this broker
    supports, or an empty list for unknown brokers."""
    return list(BROKER_ASSET_CLASSES.get((broker or "").lower(), []))

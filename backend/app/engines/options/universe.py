"""Default universe of liquid optionable names for the scanner.

Selection criteria (the same any options-scanner service would use):
  • Average daily volume > 5M shares
  • Options daily volume > 5K contracts
  • Tight bid-ask spreads in front-month chain
  • Listed on a US exchange (NYSE / NASDAQ)

Three tiers so the user can dial in how aggressive the scan is:
  TIER_CORE     — the 12 most-traded names in options markets. Use this for
                  conservative scans where you want only the deepest liquidity.
  TIER_EXPANDED — 50 names. The "all in one" scanner universe.
  TIER_FULL     — 100 names. Adds high-beta tech, biotechs, recent IPOs.
"""

# Mega-caps + index ETFs — the "always works" core
TIER_CORE = [
    "SPY", "QQQ", "IWM", "DIA",                  # broad-market ETFs
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",     # mag 7
    "META", "TSLA", "AMD",
]

# Top 50 most-traded optionable names — the scanner default.
TIER_EXPANDED = TIER_CORE + [
    # ETFs
    "XLF", "XLK", "XLE", "XLY", "XLI", "XLV", "XLP", "XBI", "XOP", "GDX",
    "TLT", "HYG", "USO", "GLD", "SLV", "ARKK",
    # Mega/large-cap stocks
    "NFLX", "PYPL", "AVGO", "ADBE", "CRM", "INTC", "ORCL", "QCOM", "TXN",
    "JPM", "BAC", "GS", "WFC", "C",
    "DIS", "MCD", "NKE", "SBUX", "WMT", "HD", "COST",
    "BA", "CAT", "DE", "GE",
    "PFE", "JNJ", "ABBV", "UNH", "LLY",
]

# Expanded universe — high-beta, recent IPOs, popular meme/momentum names
TIER_FULL = TIER_EXPANDED + [
    "PLTR", "SOFI", "AFRM", "COIN", "HOOD", "RIVN", "LCID", "F", "GM",
    "ROKU", "SHOP", "SQ", "U", "DASH", "ABNB", "UBER", "LYFT",
    "MARA", "RIOT", "MSTR", "GBTC",
    "MRNA", "BIIB", "GILD",
    "X", "CLF", "FCX", "AA", "NUE",
    "T", "VZ", "CMCSA", "TMUS",
    "DKNG", "MGM", "CCL", "NCLH",
    "TGT", "LOW", "BBY", "DG", "DLTR",
    "GME", "AMC",  # meme staples (volatile, options-heavy)
]


def get_universe(tier: str = "expanded") -> list[str]:
    t = (tier or "").lower()
    if t in ("core", "small", "conservative"):
        return TIER_CORE
    if t in ("full", "wide", "aggressive"):
        return TIER_FULL
    return TIER_EXPANDED

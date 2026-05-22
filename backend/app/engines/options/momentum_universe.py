"""Universe of high-momentum names — the 'stocks that move 10%+ regularly' list.

Compiled from:
  • Russell 2000 small-cap momentum names with options
  • The Tim Sykes / StocksToTrade typical universe ($1-$20 small floats)
  • Recent IPOs known for volatility
  • Sympathy plays around news catalysts

Roughly 400 tickers — big enough to catch any meaningful gapper, small
enough that yfinance bulk download stays under 60 seconds.
"""

MOMENTUM_UNIVERSE = [
    # ── Mega-cap ETFs + leaders (always-on liquidity)
    "SPY", "QQQ", "IWM", "DIA", "ARKK", "XLF", "XLE", "XLK", "XLY", "XLV", "XBI", "XOP", "GDX",
    "TLT", "HYG", "USO", "GLD", "SLV", "VIX",

    # ── Mag 7 + close
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX",

    # ── Large-cap, options-heavy
    "AVGO", "ADBE", "CRM", "INTC", "ORCL", "QCOM", "TXN", "MU", "AMAT", "LRCX",
    "JPM", "BAC", "GS", "WFC", "C", "MS", "SCHW",
    "DIS", "MCD", "NKE", "SBUX", "WMT", "HD", "COST", "TGT", "LOW",
    "BA", "CAT", "DE", "GE", "LMT", "RTX",
    "PFE", "JNJ", "ABBV", "UNH", "LLY", "MRK", "AMGN",
    "T", "VZ", "TMUS", "CMCSA",
    "XOM", "CVX", "COP",

    # ── High-beta / momentum favorites
    "PLTR", "SOFI", "AFRM", "COIN", "HOOD", "RIVN", "LCID", "F", "GM", "FORD",
    "ROKU", "SHOP", "SQ", "U", "DASH", "ABNB", "UBER", "LYFT", "PINS", "SNAP", "Z",
    "MARA", "RIOT", "MSTR", "GBTC", "CLSK", "HUT", "BITF",
    "MRNA", "BIIB", "GILD", "NVAX", "VKTX", "RIGL", "SAVA", "DNA", "BEAM", "CRSP",
    "X", "CLF", "FCX", "AA", "NUE", "STLD", "MT",
    "DKNG", "MGM", "CCL", "NCLH", "WYNN", "LVS",
    "GME", "AMC", "BBBY", "BBBYQ",

    # ── Small-mid cap momentum names that frequently 10%+ on news/earnings
    "SOFI", "TLRY", "CGC", "ACB", "SNDL", "SUNDL",
    "NIO", "XPEV", "LI", "BABA", "JD", "PDD", "BIDU", "TCEHY", "BIDU",
    "DIDI", "GRAB", "SE",
    "BBAI", "AI", "C3.AI", "AILE", "PATH", "GTLB", "SMCI", "ANET", "DELL",
    "PYPL", "BABA",
    "RKLB", "ASTR", "JOBY", "ACHR",
    "BB", "SBLK", "ZIM", "GNK", "DAC", "ESEA",
    "PSNY", "FSR", "MULN", "WKHS", "RIDE",
    "OPEN", "Z", "RDFN", "EXPI",
    "VKTX", "RVNC", "REGN", "ALNY", "VRTX", "IONS", "EDIT", "NTLA",
    "CVAC", "VBLT", "TGTX", "ARWR", "SRPT", "BLUE",
    "IBRX", "ANIK", "MNKD",
    "AMRX", "TEVA", "MYL", "BMY", "PFE", "AZN",
    "ENPH", "SEDG", "RUN", "PLUG", "FCEL", "BLDP", "BE", "BLNK", "CHPT", "EVGO",
    "TLRY", "CGC", "CRON", "ACB", "OGI",
    "SBSW", "PAAS", "CDE", "AG",
    "CHWY", "CVNA", "W", "ETSY", "PTON", "FIGS",
    "SE", "MELI", "NU",
    "DOCN", "DOCS", "ZS", "OKTA", "NET", "DDOG", "MDB", "CRWD", "PANW",
    "TWLO", "ZM",
    "QS", "FCEL", "BLDP", "PLUG",

    # ── Mid-cap industrials/cyclicals — earnings movers
    "EAT", "DRI", "TXRH", "BLMN", "RUTH", "BJRI", "CHEF",
    "DAL", "AAL", "UAL", "LUV", "JBLU", "ALK", "HA",
    "JBHT", "ODFL", "XPO", "KNX", "WERN", "LSTR",
    "CVS", "WBA", "RAD",
    "CAG", "GIS", "K", "POST", "MDLZ", "KHC", "HSY",
    "MO", "PM", "BTI",
    "WBD", "PARA", "FOX", "FOXA", "NWSA",
    "ROKU", "FUBO", "IQ",

    # ── Crypto-proxy names (vol follows BTC closely)
    "MARA", "RIOT", "CLSK", "HUT", "BITF", "MSTR", "COIN", "HOOD", "SI",
    "GBTC", "ETHE", "BITO",

    # ── Recent IPOs / SPACs (high vol)
    "RDDT", "ARM", "INSTACART", "CART", "KLAR", "STMP", "AS",
    "BIRK", "WBA", "BMBL", "DASH", "RBLX", "PATH",

    # ── Common low-float runners that print 10-50% moves on no news
    "GNUS", "SOS", "EBET", "MULN", "NILE", "ICCM", "HKIT", "SNTI",
    "WISA", "RGC", "SCOR",

    # ── Healthcare/biotech momentum
    "BNTX", "PFE", "JNJ",
    "GH", "TWST", "PACB", "ILMN",

    # ── Bank earnings movers
    "JPM", "BAC", "GS", "MS", "C", "WFC", "USB", "PNC", "TFC", "FITB",
    "RF", "HBAN", "KEY", "CFG", "ZION", "MTB", "CMA",

    # ── Defense
    "LMT", "NOC", "RTX", "GD", "LDOS", "HII", "TXT", "KTOS",

    # ── Energy / oil
    "OXY", "MRO", "DVN", "EOG", "PXD", "FANG", "APA", "MUR", "CHK",
    "HAL", "SLB", "BKR", "NOV", "FTI",

    # ── REITs (rate-sensitive)
    "VNQ", "O", "PLD", "AMT", "CCI", "EQIX", "DLR",
]

# Dedupe + sort
MOMENTUM_UNIVERSE = sorted(set(MOMENTUM_UNIVERSE))



# Small-cap / low-float names that move 10%+ regularly — these are the
# StocksToTrade-style targets. We scan these FIRST every cycle so the
# 50-candidate per-scan cap doesn't waste slots on AAPL/AMZN.
SMALL_CAP_PRIORITY = [
    # Penny / low-float runners
    "MULN", "GNUS", "SOS", "EBET", "NILE", "WISA", "RGC", "SCOR",
    "BBBYQ", "BBAI", "ANIK", "MNKD", "IBRX",
    # Cannabis (volatile, options-thin, often gaps on regulation news)
    "TLRY", "CGC", "ACB", "OGI", "CRON", "SNDL",
    # EVs / SPACs (high beta)
    "RIVN", "LCID", "NIO", "XPEV", "LI", "QS", "FFIE", "MULN", "WKHS",
    "PSNY", "POLES",
    # Crypto-proxy small/mid
    "MARA", "RIOT", "CLSK", "HUT", "BITF", "SI", "GBTC",
    # Biotech runners (FDA / topline data movers)
    "VKTX", "BIIB", "RVNC", "REGN", "ALNY", "IONS", "EDIT", "NTLA",
    "VBLT", "TGTX", "ARWR", "SRPT", "BLUE", "NVAX", "VRTX", "MRNA",
    "CVAC", "RIGL", "SAVA", "DNA", "BEAM", "CRSP",
    # Meme staples
    "GME", "AMC", "BBBY",
    # Real-estate small-caps that move on rates
    "OPEN", "Z", "RDFN", "EXPI",
    # Small-cap shippers (vol on tariff/freight news)
    "SBLK", "ZIM", "GNK", "DAC", "ESEA",
    # Solar / clean energy (rate-sensitive)
    "ENPH", "SEDG", "RUN", "PLUG", "FCEL", "BLDP", "BE", "BLNK", "CHPT", "EVGO",
    # Recent IPOs / SPACs known for moving
    "RDDT", "ARM", "BIRK", "BMBL", "RBLX", "PATH",
    # Misc small-mid momentum
    "FFIE", "PRPL", "PARA", "FUBO",
]

# Build the prioritized list: small-caps first, then the rest dedupe
def get_prioritized_universe():
    seen = set()
    out = []
    for t in SMALL_CAP_PRIORITY:
        if t not in seen:
            seen.add(t); out.append(t)
    for t in MOMENTUM_UNIVERSE:
        if t not in seen:
            seen.add(t); out.append(t)
    return out

MOMENTUM_UNIVERSE_PRIORITIZED = get_prioritized_universe()

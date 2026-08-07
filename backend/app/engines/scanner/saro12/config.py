"""Saro 1.2 Configuration — every threshold from the owner spec, verbatim.

ONE namespace for the whole scanner. Change numbers here, nowhere else.
Pure constants: this module imports nothing but the stdlib, so it is safe
to import from tests, the scheduler hook, and every saro12 submodule.

Spec sections implemented:
  Section 1 (screen)   -> screen.py
  Section 2 (trigger)  -> indicators.py (TTM-squeeze + alert_state)
  Section 3 (free OI)  -> oi_snapshot.py (paid sweeps feed = commented hook only)
  Sections 4-5         -> OUT OF SCOPE (shadow signal quality first)
"""
from __future__ import annotations

# ── SECTION 1: SCREEN ──────────────────────────────────────────────────────
FLOAT_MAX_SHARES = 20_000_000          # float < 20,000,000 shares
DAILY_VOLUME_MIN = 500_000             # daily volume > 500,000
PRICE_MIN = 1.00                       # price $1.00 ..
PRICE_MAX = 15.00                      # .. $15.00
RVOL_MIN = 4.0                         # RVOL >= 4.0x vs 3-month avg volume
RSI_PERIOD = 14                        # RSI(14) ..
RSI_CROSS_LEVEL = 65.0                 # .. crossing above 65
RSI_CROSS_LOOKBACK_BARS = 15           # "crossing" = crossed within last N bars
                                       # (concrete + testable reading of the spec)
MACD_FAST = 12                         # WITH positive MACD histogram
MACD_SLOW = 26
MACD_SIGNAL = 9

# ANTI-JUNK
MARKET_CAP_MIN = 30_000_000            # market cap >= $30M
ALLOWED_EXCHANGES = ("NASDAQ", "NYSE")  # NASDAQ/NYSE only (no OTC/pink)
OWNERSHIP_MIN_PCT = 10.0               # institutional OR insider ownership > 10%
# NOTE: FMP institutional-ownership/* is RESTRICTED on this plan. The
# ownership filter is a PROXY (presence of 13D/G beneficial owners OR insider
# activity) and FAILS OPEN with a logged note when unverifiable — never
# fail-closed the whole screen on this one filter (owner instruction).
# "shortable OR active options chain": options chain checked best-effort
# (zero-FMP yfinance expirations); shortability has no data source on this
# plan -> the pair fails OPEN with a logged note when both are unverifiable.

# ── SECTION 2: TRIGGER (TTM-squeeze style) ─────────────────────────────────
BB_PERIOD = 20                         # Bollinger(20, ..)
BB_STD = 2.0                           # .. 2 std
KC_PERIOD = 20                         # Keltner(20, ..)
KC_ATR_MULT = 1.5                      # .. 1.5 x ATR
KC_ATR_PERIOD = 20                     # ATR period matches the KC period (TTM convention)
# Squeeze ON  = BB fully INSIDE Keltner (upper below KC upper AND lower above KC lower)
# ALERT       = squeeze active (prior bar) AND RVOL >= RVOL_MIN
#               AND price breaks above the upper Keltner line
#               (prev close <= prev KC-upper, current close > current KC-upper)
MOMENTUM_TIMEFRAME = "1min"            # all indicator math on FMP /stable 1-min bars
                                       # (real-time; prod Polygon key is 15-min delayed
                                       #  — NEVER fall back to Polygon bars here)
MIN_BARS_FOR_SIGNAL = 40               # need MACD(26)+signal(9) + BB/KC(20) warm-up

# ── SECTION 3: FREE OI SNAPSHOT ────────────────────────────────────────────
OI_SURGE_PCT = 50.0                    # flag > 50% 24h OI surge on OTM calls
OI_EXPIRATIONS_PER_TICKER = 2          # nearest N expirations snapped nightly
OI_SNAPSHOT_TTL_S = 8 * 24 * 3600      # keep snapshots ~8 days in redis
OI_SURGE_MAX_LOOKBACK_DAYS = 4         # latest two snapshots within this window

# ── RVOL DEFINITION (pinned — one deterministic definition for both uses) ──
# day_rvol = today's cumulative volume / 3-month average daily volume.
# Section 1 screens day_rvol >= RVOL_MIN; the Section 2 alert reuses the SAME
# day_rvol (spec wording gates the alert on "RVOL >= 4.0").
# 3-month average source order: yfinance avg_volume_3m (zero FMP budget),
# then FMP profile volAvg/averageVolume from the capped enrichment.

# ── BUDGET / CAPS (FMP request discipline — morning funnel already ~79) ────
ENRICH_TOP_N = 15                      # per-candidate FMP enrichment cap (by RVOL)
PRE_ENRICH_CAP = 40                    # zero-budget yfinance pre-rank cap (by volume)
FMP_CALLS_PER_ENRICH = 4               # shares-float + profile + insider-stats + 13D/G
# Worst case/day: 1 screener + ENRICH_TOP_N*FMP_CALLS_PER_ENRICH enrichment
# + ENRICH_TOP_N intraday 1-min = 1 + 60 + 15 = 76 calls, all redis-cached
# per-ET-day (a same-day re-run after restart costs ~0-15).

# ── FMP /stable endpoints (probed against the live key; /api/v3 is DEAD) ──
SCREENER_URL = "https://financialmodelingprep.com/stable/company-screener"
SHARES_FLOAT_URL = "https://financialmodelingprep.com/stable/shares-float"
PROFILE_URL = "https://financialmodelingprep.com/stable/profile"
INSIDER_STATS_URL = "https://financialmodelingprep.com/stable/insider-trading/statistics"
BENEFICIAL_URL = "https://financialmodelingprep.com/stable/acquisition-of-beneficial-ownership"
SCREENER_LIMIT = 10000                 # whole sweep in one page (probed 3962 rows)

# ── SCHEDULING (all seconds-since-midnight ET) ─────────────────────────────
ENV_GATE = "SARO12_SHADOW_ENABLED"     # default "1"
SCAN_SPAWN_START_S = 9 * 3600 + 33 * 60    # 09:33:00 ET (9:33 fire doctrine)
SCAN_SPAWN_END_S = 11 * 3600               # 11:00:00 ET (NY-lunch no-trade doctrine)
OI_SPAWN_START_S = 18 * 3600 + 30 * 60     # 18:30:00 ET nightly OI snapshot window
OI_SPAWN_END_S = 21 * 3600                 # 21:00:00 ET
# Optional squeeze re-check loop inside the daily task (OFF by default so the
# shipped budget stays 76/day; each pass past the bar-cache TTL costs up to
# RECHECK_TOP_N intraday calls).
RECHECK_ENV_GATE = "SARO12_RECHECK_ENABLED"  # default "0"
RECHECK_SECONDS = 600.0
RECHECK_TOP_N = 5

# ── IDENTITY / PERSISTENCE ─────────────────────────────────────────────────
MATCHED_STRATEGY_PREFIX = "saro12:"    # cohort identity (mirrors the 'v2:' convention)
MATCHED_STRATEGY = "saro12:squeeze_breakout"
SOURCE_TAG = "saro12_shadow"           # email_signals_history.source (col added
                                       # via the idempotent _ensure_columns pattern)
FALLBACK_STOP_PCT = 3.0                # last-resort stop when no structure/KC stop
TARGET_RR = 2.0                        # measured-move target = entry + 2R

# ── REDIS KEYS (own latches — never share another cohort's latch) ──────────
LATCH_SCAN_FMT = "theta:saro12:{date}"          # daily shadow-scan latch (SETNX)
LATCH_OI_FMT = "theta:saro12:oi_done:{date}"    # nightly OI-snapshot latch (SETNX)
LATCH_TTL_S = 36 * 3600
SCREEN_CACHE_FMT = "saro12:screen:{date}"       # raw bulk-screener rows
ENRICH_CACHE_FMT = "saro12:enrich:{date}:{ticker}"  # per-ticker FMP enrichment
CANDIDATES_KEY_FMT = "saro12:candidates:{date}"     # screened list for nightly OI
OI_KEY_FMT = "saro12:oi:{date}:{ticker}"            # {occ_symbol: open_interest}
DAY_CACHE_TTL_S = 3 * 24 * 3600
REDIS_URL_ENV = "REDIS_URL"
REDIS_URL_DEFAULT = "redis://redis:6379/0"

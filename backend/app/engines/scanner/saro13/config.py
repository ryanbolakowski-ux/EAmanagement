"""Saro 1.3 Configuration — every threshold from the owner spec, verbatim.

"Predictive consolidation": find the coil BEFORE the move — the opposite of
Saro 1.2's momentum chase. ONE namespace for the whole scanner. Change
numbers here, nowhere else. Pure constants: this module imports nothing but
the stdlib, so it is safe to import from tests, the scheduler hook, and
every saro13 submodule.

Spec sections implemented:
  S1 COMPRESSION      -> indicators.py (daily BB width in lowest 15% of its
                         60-day range, ATR(14) declining 5 consecutive
                         sessions, daily-change > +3% reject)
  S2 GUARDRAILS       -> indicators.py (daily RSI(14) in [45,55] with hard
                         reject > 65; daily volume below its 20-day MA)
  S3 THETA VIABILITY  -> iv_snapshot.py (IV Rank > 50 vs accumulated ATM-IV
                         history) + screen.py (M&A hard exclude)
  S4 DELIVERY         -> shadow.py (detection_ts ms-precision stamp +
                         flag-gated instant-alert webhook stub, OFF by
                         default; APNS hook commented, APNS_ENABLED still 0)
"""
from __future__ import annotations

# ── S1: COMPRESSION (daily bars only — completed sessions, never today) ────
DAILY_CHANGE_MAX_PCT = 3.0          # reject daily change > +3% vs prior close
                                    # (applied to the LIVE session move AND the
                                    #  last completed session — the coil must
                                    #  not already be breaking)
BB_PERIOD = 20                      # Bollinger(20, ..) on DAILY closes
BB_STD = 2.0                        # .. 2 std
# BB WIDTH is pinned as (upperBB - lowerBB) / middleBB on daily(20,2).
BBWIDTH_LOWEST_PCT = 15.0           # width in the LOWEST 15% of ..
BBWIDTH_RANGE_SESSIONS = 60         # .. its trailing 60-session range:
                                    # current width <= 15th percentile of the
                                    # trailing 60 width values (incl. current)
ATR_PERIOD = 14                     # ATR(14), Wilder smoothing, daily bars
ATR_DECLINE_SESSIONS = 5            # declining 5 consecutive sessions =
                                    # STRICTLY decreasing last 5 ATR values

# ── S2: GUARDRAILS ─────────────────────────────────────────────────────────
RSI_PERIOD = 14                     # daily RSI(14) ..
RSI_BAND_LO = 45.0                  # .. between 45 ..
RSI_BAND_HI = 55.0                  # .. and 55
RSI_HARD_REJECT = 65.0              # hard reject > 65 (implied by the band;
                                    # tracked separately so the log says so)
VOL_SMA_PERIOD = 20                 # daily volume BELOW SMA20(volume),
                                    # inclusive window ending at the last
                                    # completed session (charting convention)

# ── S3: THETA VIABILITY ────────────────────────────────────────────────────
IV_RANK_MIN = 50.0                  # IV Rank > 50% vs 52-week history
IV_HISTORY_MIN_SESSIONS = 20        # rank applies only with >= 20 sessions of
                                    # accumulated ATM-IV history; below that
                                    # the gate FAILS OPEN with a LOUD
                                    # 'iv-history-warming' warning (documented
                                    # ramp — true 52-week rank at ~252)
# M&A hard exclude: mergers-acquisitions-latest targetedSymbol match, plus a
# headline keyword fallback on news/stock (spec keywords, plural-tolerant).
MA_KEYWORD_PATTERN = r"\b(?:buyouts?|acquisitions?|mergers?|take[-\s]?private)\b"
NEWS_LIMIT = 20                     # headlines per news/stock check

# ── S4: DELIVERY (shadow-honest instant alert) ─────────────────────────────
ALERT_WEBHOOK_ENV = "SARO13_ALERT_WEBHOOK"   # default empty = OFF (stub)
APNS_ENABLED_ENV = "APNS_ENABLED"            # still 0 — commented hook only

# ── UNIVERSE PRE-FILTER (engineering choice, NOT owner-spec numbers) ───────
# Saro 1.3 sells theta on the coil, so the universe skews to liquid,
# optionable mid/large caps (tight chains, real IV) — unlike saro12's
# low-float rockets. Reuses saro12's bulk company-screener approach with
# client-side NASDAQ/NYSE filtering.
PRICE_MIN = 10.00                   # below ~$10 option strikes are too coarse
PRICE_MAX = 500.00
DAILY_VOLUME_MIN = 1_000_000        # liquidity floor (contraction is judged
                                    # vs the ticker's OWN 20-day MA, not this)
MARKET_CAP_MIN = 1_000_000_000      # $1B+ — junk never has a theta-worthy chain
ALLOWED_EXCHANGES = ("NASDAQ", "NYSE")
SCREENER_LIMIT = 10_000             # whole sweep in one page

# ── DAILY HISTORY (FMP /stable EOD — bare-list response, newest-first) ─────
EOD_FROM_DAYS = 150                 # calendar days back -> ~100 trading days
MIN_DAILY_SESSIONS = 80             # BB(20) width needs 60 widths -> 79 bars;
                                    # 80 gives one spare
SWING_LOW_LOOKBACK = 10             # last-swing-low stop = min low, 10 sessions

# ── BUDGET / CAPS (FMP request discipline — hard ceiling ~45/day) ──────────
DAILY_HISTORY_CAP = 30              # max historical-price-eod pulls per ET day
NEWS_CHECK_CAP = 10                 # max news/stock headline checks per ET day
# Worst case/day: 1 company-screener + 30 daily-history + 1 M&A list
# + 10 news checks = 42 <= 45 ceiling. All redis-cached per ET day under
# saro13:* keys, so a same-day re-run after a restart costs ~0.
# (Morning funnel ~79 + saro12 ~76 + saro13 42 = ~197 combined.)

# ── FMP /stable endpoints (probed on the live key; /api/v3 is DEAD) ────────
SCREENER_URL = "https://financialmodelingprep.com/stable/company-screener"
EOD_HIST_URL = "https://financialmodelingprep.com/stable/historical-price-eod/full"
MA_LATEST_URL = "https://financialmodelingprep.com/stable/mergers-acquisitions-latest"
NEWS_STOCK_URL = "https://financialmodelingprep.com/stable/news/stock"

# ── IV SNAPSHOT (Tradier SANDBOX reads only — 2026-08-06 incident rule) ────
IV_TENOR_MIN_DTE = 7                # tenor rule: nearest MONTHLY (3rd-Friday)
                                    # expiration >= 7 DTE, so the accumulated
                                    # series is comparable day over day
                                    # (expirations list starts with 0DTE)
IV_STRIKE_WALK = 6                  # nearest strikes tried until a populated
                                    # IV is found (illiquid rows can have
                                    # mid_iv==0 while smv_vol stays populated)
IV_SNAPSHOT_CAP = 30                # tickers snapped per night (== history cap)
IV_TABLE = "saro_iv_history"        # durable Postgres accumulation table
                                    # (NOT redis — appendonly=off durability rule)

# ── SCHEDULING (all seconds-since-midnight ET) ─────────────────────────────
ENV_GATE = "SARO13_SHADOW_ENABLED"          # default "1"
SCAN_SPAWN_START_S = 9 * 3600 + 33 * 60     # 09:33:00 ET (symmetric with
SCAN_SPAWN_END_S = 11 * 3600                # saro12 for the 3-way readout —
                                            # a compression scan COULD run
                                            # pre-market; kept symmetric)
IV_SPAWN_START_S = 18 * 3600 + 30 * 60      # 18:30:00 ET nightly IV snapshot
IV_SPAWN_END_S = 21 * 3600                  # 21:00:00 ET (same window/account
                                            # policy as saro12's OI job)

# ── IDENTITY / PERSISTENCE ─────────────────────────────────────────────────
MATCHED_STRATEGY_PREFIX = "saro13:"
MATCHED_STRATEGY = "saro13:compression_coil"
SOURCE_TAG = "saro13_shadow"        # email_signals_history.source (column added
                                    # via the idempotent _ensure pattern — it
                                    # does NOT pre-exist in prod)
FALLBACK_STOP_PCT = 3.0             # last-resort stop when no BB/swing stop
TARGET_RANGE_MULT = 2.0             # target = entry + 2x compression range
                                    # (range = upperBB - lowerBB, the coil
                                    # width — measured-move on expansion)

# ── REDIS KEYS (own latches — never share another cohort's latch) ──────────
LATCH_SCAN_FMT = "theta:saro13:{date}"           # daily shadow-scan latch (SETNX)
LATCH_IV_FMT = "theta:saro13:iv_done:{date}"     # nightly IV-snapshot latch (SETNX)
LATCH_TTL_S = 36 * 3600
SCREEN_CACHE_FMT = "saro13:screen:{date}"        # raw bulk-screener rows
DAILY_CACHE_FMT = "saro13:daily:{date}:{ticker}" # per-ticker EOD history
MA_CACHE_FMT = "saro13:ma_list:{date}"           # M&A latest list (1 call/day)
NEWS_CACHE_FMT = "saro13:news:{date}:{ticker}"   # per-ticker headline check
CANDIDATES_KEY_FMT = "saro13:candidates:{date}"  # screened list for nightly IV
DAY_CACHE_TTL_S = 3 * 24 * 3600
REDIS_URL_ENV = "REDIS_URL"
REDIS_URL_DEFAULT = "redis://redis:6379/0"

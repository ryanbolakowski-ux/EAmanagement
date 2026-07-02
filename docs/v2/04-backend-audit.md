# Backend Architecture Audit (2026-07-01) — read-only

149 py files · 166 routes / 18 routers · 27 tables · 82 test files · 185 raw text() vs ~40 ORM queries (82% raw).

## Process architecture — SINGLE PROCESS, EXTREME RISK
11 lifespan asyncio tasks + dynamic (paper sessions, signal watchers, live sessions) all inside the ONE FastAPI process. No supervisor: tasks are fire-and-forget create_task(); a crashed watcher stays dead until restart (no add_done_callback, no auto-restart). Event-loop block = every user gets 503. Zombie-recovery + resume-on-restart exist (good), no graceful shedding.

## Database
candle_cache 4,357,644 rows (hottest) · account_signals 13,141 · trades 1,228 · email_signals_history 277 · users 10. 78 indexes; hot tables properly indexed; no obvious missing-index smells. Pool 10+20 overflow (~30 cap) — prior watcher connection leak (~70 sockets, exhausted PG max 100) FIXED in code. candle_cache append-only, no visible VACUUM strategy.

## Caching
Redis: 2FA tokens 5m · pwreset 1h · admin passcode session 8h · pipeline-alert dedup · login rate-limit (fail-open). In-process: SIX unbounded module-level dict caches (_YF_CACHE, _BAR_CACHE, _chain_cache, _preview_chain_cache, proxy_scale _cache, _fundamentals_cache) — time-based staleness only, no LRU/maxsize → memory growth at scale.

## Auth/Security
JWT HS256, 60m access / 30d refresh, no key rotation (SECRET_KEY compromise = total takeover). Admin = JWT + separate bcrypt-hashed passcode (Redis session 8h) — strong, but SOME admin endpoints only require_admin without passcode (e.g. tier change — see verification appendix). 2FA TOTP via pyotp, no backup codes (lockout risk). Broker creds Fernet-encrypted in DB. No hardcoded secrets found in code.

## Reliability
1,324 try/except; 553 broad except Exception; 26 bare except (worst: optimization.py:684 silent swallow). pipeline_alerts w/ Redis dedup fail-open. No retry on broker API calls (single attempt). Watcher crash = dead until restart.

## Performance bottlenecks
Blocking-in-async hot paths: yfinance Ticker().history() 2–10s under a GLOBAL LOCK (serializes all symbols); Polygon requests.get (~1–5s) and Alpaca fetch not executor-wrapped; psycopg2 candle reads properly to_thread'd. staleTime/polling thundering herd from frontend compounds. _BAR_CACHE dedups watcher reads to ~2 queries/min (good).

## Scalability verdict
10x users: yfinance global lock serializes 20+ watchers (minutes of queue), pool exhaustion risk, 100–200MB cache growth. 100x: unusable without re-architecture. Fixes: bounded TTL caches, task supervisor, executor-wrap all sync HTTP, watcher pooling (one watcher per instrument shared across users), worker pool (Celery) for backtests, candle read replica.

## Test coverage gaps
Good: backtests, paper, strategies, options, auth. ZERO: live_trading, email pipeline, broker adapters (Tradovate/Tradier), migrations, load. (Cross-agent contradiction on this — resolved in verification appendix.)

## Top issues (ranked)
1 CRIT unbounded in-process caches → OOM at scale (runner.py:398–410 +5 more)
2 CRIT no task supervisor; crashed background task stays dead silently (main.py lifespan)
3 CRIT yfinance global lock serializes event-loop-blocking calls (runner.py:405–430)
4 HIGH bare except optimization.py:684 swallows failures
5 HIGH some admin endpoints lack passcode gate (admin.py ~150–200) [verify]
6 HIGH 185 raw text() queries — parameterized today, high audit surface
7 HIGH sync network calls not executor-wrapped (runner.py:100–180)
8 MED no broker API retry → single blip drops a live order
9 MED live trading zero tests · 10 MED email zero tests · 11 MED pool sizing · 12 MED admin session 8h TTL · 13 MED no TOTP backup codes · 14 LOW candle_cache VACUUM · 15 LOW email sync run_until_complete latency

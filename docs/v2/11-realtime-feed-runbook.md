# 11 — Real-Time Feed Go-Live Runbook (REALTIME-FEED-V1)

**Status:** built, tested, flag-OFF in prod. Waiting on the vendor real-time
entitlement (most likely a Polygon plan upgrade — same API key, real-time
websocket access added).

**What it is:** a supervised background task (`app/engines/data_feeds/
realtime_feed.py`) that streams Polygon `AM.*` minute aggregates over a
websocket into a bounded in-process store. Three delayed consumers prefer the
store whenever it is fresh (newest bar ≤ 120s old) and fall back to their
existing REST paths otherwise:

| Consumer | Today (flag off) | With the feed live |
|---|---|---|
| `GET /api/v1/public/tape` | yfinance daily closes, 60s cache | stock/ETF rows overlaid with live store prices per request — LIVE really means live. Futures rows (ES/NQ/YM/RTY) intentionally stay on the real (delayed) yfinance quote — we never display a proxy-scaled price as a real futures price. |
| Futures signal proxy (`account_signals/runner.py`) | Alpaca IEX → delayed Polygon REST → candle_cache → yfinance | in-process store becomes source #0 (seconds-fresh, zero REST). QQQ/SPY are subscribed at boot; IWM/DIA (RTY/YM proxies) auto-subscribe on the first RTY/YM poll. The SIGNAL-PRICE-ALIGN drift guard still validates every bar series against the real candle_cache close. |
| Theta-scanner confirmation (`theta_scanner._apply_quality_filters`) | 15-min-delayed REST 1-min bars — the 09:35 opening candle is unreadable until ~09:50, quiet tickers hit "no Polygon intraday bars → watch-only" | live store bars are merged over the REST bars (live wins per minute); candidate tickers auto-subscribe on first scan tick. The 09:30–09:35 candles are readable AT 09:35. |

Everything is gated on one env var. **Flag off = byte-identical to current
behavior** (verified by `tests/test_realtime_feed.py`, import-time and
call-time).

---

## Go-live checklist (one env flip)

Do this **after market close** on the day the entitlement lands.

### 1. Confirm the key is entitled
```bash
# From the server — should return "connected" then "auth_success":
python3 - <<'EOF'
import asyncio, aiohttp, json, os
async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("wss://socket.polygon.io/stocks") as ws:
            print(await ws.receive())     # connected
            await ws.send_json({"action": "auth", "params": os.environ["POLYGON_API_KEY"]})
            print(await ws.receive())     # auth_success = entitled; auth_failed = not yet
asyncio.run(main())
EOF
```
(If it says `auth_failed` / not authorized, the plan upgrade hasn't propagated
— you can still flip the flag safely, see "Flipping early" below.)

### 2. Flip the flag
Add to `/root/edge-asset-management/backend/.env` (or the compose env of
whatever stack is serving the backend at that point):
```
REALTIME_FEED=polygon
```
Optional knobs (defaults are fine):
```
REALTIME_SYMBOLS=QQQ,SPY            # boot-time subscriptions (the default).
                                    # Everything else self-subscribes at runtime:
                                    # IWM/DIA on the first RTY/YM futures poll,
                                    # scanner candidate tickers on the first scan
                                    # tick, tape stocks on the first /tape request
                                    # — no need to list any of them.
REALTIME_AUTH_RETRY_S=900           # retry cadence while NOT entitled
REALTIME_BACKOFF_BASE_S=2           # reconnect backoff base (exponential)
REALTIME_BACKOFF_CAP_S=60           # reconnect backoff hard cap
FUTURES_PROXY_MAX_DRIFT_PCT=0.4     # existing drift guard — still applies to store bars
```

### 3. Restart the backend (recreate, not just restart)
```bash
cd /root/edge-asset-management
docker compose up -d backend
```
> `docker restart edge_backend` is NOT enough — env_file values are baked in
> at container **creation**, so a plain restart won't pick up the new var.

### 4. Verify (during RTH the next morning)
1. **ws connected + authenticated** — within ~30s of boot:
   ```bash
   docker logs edge_backend 2>&1 | grep realtime-feed | tail -20
   ```
   Expect, in order:
   - `[realtime-feed] polygon feed configured (symbols=['QQQ', 'SPY'])`
   - `[realtime-feed] ws connected: wss://socket.polygon.io/stocks`
   - `[realtime-feed] polygon ws AUTHENTICATED — real-time stream is LIVE`
   - `[realtime-feed] subscribed: AM.QQQ,AM.SPY`
   - then, as consumers wake up, incremental `[realtime-feed] subscribed: ...`
     lines for IWM/DIA (first RTY/YM poll), the tape stocks (first /tape hit)
     and scanner candidates (first scan tick).
   - during RTH: `[realtime-feed] 1000 bars ingested ...` every so often.
2. **Store ages < 90s during RTH** — the futures watchers log it every poll:
   ```bash
   docker logs edge_backend 2>&1 | grep "source=realtime-store" | tail -5
   # [Signals] futures NQ source=realtime-store proxy=QQQ scale=41.xx latest_bar=... age=NNs
   ```
   `age` should be < 90s. If you see no `source=realtime-store` lines during
   RTH, the store is stale/empty and the watchers silently fell back to
   Alpaca/REST (which is safe, but means the ws is not delivering).
3. **Tape is genuinely live** — two curls a few seconds apart during RTH:
   ```bash
   curl -s localhost:8000/api/v1/public/tape | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['live'], [q for q in d['quotes'] if q['symbol']=='SPY'])"
   ```
   `live: true` and the SPY/QQQ/NVDA/... prices should move BETWEEN calls even
   inside the 60s payload cache window (the overlay is applied per-request).
4. **Scanner confirmation at 09:35, not ~09:50** — on the first scan tick:
   ```bash
   docker logs edge_backend 2>&1 | grep "merged .* realtime store bars"
   ```
   and the 09:35 tick should NOT log
   `no Polygon intraday bars — UNCONFIRMED, downgraded to watch-only` for
   tickers that are actively trading.
5. **Systems Check green** — admin → Systems Check: no new red rows; the
   supervised task shows up as `realtime_feed` (a crash would produce
   `[supervisor] task 'realtime_feed' crashed` log lines + a pipeline alert —
   there should be none).

### Rollback (instant, zero-risk)
```bash
# remove/comment REALTIME_FEED in backend/.env, then:
cd /root/edge-asset-management && docker compose up -d backend
```
Flag off returns every consumer to today's REST behavior byte-for-byte. No
data migration, no state — the store only lives in process memory.

---

## Flipping early (key NOT entitled yet)

Safe. The feed connects, Polygon answers `auth_failed` / "not authorized",
and the feed logs ONE clear warning then sleeps `REALTIME_AUTH_RETRY_S`
(15 min) before retrying:

```
[realtime-feed] polygon ws NOT AUTHORIZED — the API key has no real-time ws
entitlement yet. Consumers stay on their REST paths; retrying in 900s. ...
```

No crash-loop, no supervisor restart-budget burn, no alert spam; every
consumer keeps its current REST path because the store simply stays empty.
When the entitlement propagates, one of the 15-min retries succeeds and the
platform goes live on its own — no restart needed.

## Switching vendors later

The consumers only ever touch `LatestBarStore` via `get_fresh_bars()` /
`get_fresh_price()` — they don't know Polygon exists. A different vendor is a
new `RealtimeFeed` subclass + one branch in `create_feed_from_env()` + a new
`REALTIME_FEED=<provider>` value. Nothing else changes.

## Failure modes and what they look like

| Failure | Behavior | Log signature |
|---|---|---|
| Key not entitled | slow retry every 15 min, consumers on REST | `NOT AUTHORIZED ... retrying in 900s` |
| Network blip / Polygon restart | reconnect w/ exponential backoff (2s→60s cap), resubscribes everything | `ws session died ... reconnecting in Ns` |
| Feed coroutine bug (should not happen) | task_supervisor restarts it w/ backoff + pipeline alert; gives up after 5 crashes/hour | `[supervisor] task 'realtime_feed' crashed` |
| Stream silently stops delivering | store goes stale (>120s) → every consumer auto-falls back to REST within one poll | `source=alpaca`/`source=polygon` lines reappear |
| Bad/junk ws frames | skipped per-event, never raises | `bad ws event skipped` / `unparseable ws frame` |

## Tests

`backend/tests/test_realtime_feed.py` (15 tests, no network, no DB):
store bounded/replace/ordering, age + staleness gate, flag-off byte-identical
consumers (tape overlay identity, runner path empty, scanner watch-only
preserved), flag-on integration (tape overlay math, runner scaled bars
preferred over Alpaca/Polygon, scanner confirms from store bars alone — the
09:35 case), default symbols QQQ,SPY + runner auto-subscribe of IWM/DIA,
reconnect backoff caps, not-authorized slow-retry.

```bash
IMG=$(docker inspect edge_backend --format '{{.Image}}')
docker run --rm --cpus=2 -v /root/worktrees/v2-redesign/backend:/app -w /app $IMG \
  sh -c 'pip install -q pytest && python -m pytest tests/test_realtime_feed.py -q'
```

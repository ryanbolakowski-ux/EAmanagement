"""Financial Modeling Prep (FMP) realtime feed — the second RealtimeFeed provider.

WHY: the prod Polygon key is the 15-min-DELAYED tier and its real-time ws
entitlement hasn't landed. The owner's FMP plan IS live-entitled, so
REALTIME_FEED=fmp + FMP_API_KEY lights up the SAME LatestBarStore + consumer
plumbing (public tape, futures signal proxy, scanner store-merge) that
REALTIME-FEED-V1 built — no consumer changes, one env flip.

TRANSPORT (two layers, each independently graceful):
  1. REST polling (default — works on every FMP paid tier): ONE batch
     /api/v3/quote-short/{SYM1,SYM2,...} request every FMP_POLL_SECONDS
     (default 5) REGARDLESS of symbol count. Successive quotes are folded into
     in-process minute buckets (o/h/l/c from the price path; v from the
     positive delta of the endpoint's CUMULATIVE day volume — v stays 0 when
     the payload carries no volume field) and pushed into the shared
     LatestBarStore in the Polygon-REST-aggs dict shape, so every existing
     consumer works unchanged.
  2. Websocket (FMP_WEBSOCKET=1, default 0): wss://websockets.financialmodelingprep.com
     — login event with the api key, then subscribe. Trade ticks feed the same
     minute aggregation. If the plan lacks ws entitlement, the login rejection
     is logged ONCE and ws is disabled for the process lifetime — REST polling
     continues untouched, no crash-loop, no supervisor budget burn. While the
     ws is authed AND delivering, the poll loop skips its HTTP request (rate
     discipline + no double-counted volume); the moment the ws goes quiet the
     next poll cycle resumes automatically.

ON-DEMAND REAL-TIME BARS (the scanner's killer feature): fetch_intraday_bars()
pulls /api/v3/historical-chart/1min/{SYM} — REAL-TIME on live-entitled plans —
for ARBITRARY tickers (no subscription needed), behind a 15s TTL cache
(≤ 1 request/symbol/15s, failures cached as a cooldown) and a hard per-request
timeout. theta_scanner._apply_quality_filters prefers these bars over the
15-min-delayed Polygon REST aggs when REALTIME_FEED=fmp, falling back to
Polygon on ANY failure — that is what makes 09:35 confirmation real-time for
every candidate, not just pre-subscribed symbols.

RATE DISCIPLINE: 1 poll request per FMP_POLL_SECONDS total; on-demand bars
capped by the TTL cache; ONE shared aiohttp session for everything; 429/5xx →
exponential backoff (base·2^n, hard cap — same env knobs as the Polygon feed),
never a tight loop.

No new dependencies: aiohttp==3.13.5 (already pinned) for both transports.
Go-live checklist: docs/v2/11-realtime-feed-runbook.md (FMP variant section).
"""
import asyncio
import json
import os
import threading
import time
from datetime import datetime
from typing import Iterable, Optional

from loguru import logger

from app.engines.data_feeds.realtime_feed import LatestBarStore, RealtimeFeed

# ── Endpoints (overridable for staging/tests) ───────────────────────────────
# FMP retired /api/v3 for accounts created after 2025-08-31 ("Legacy Endpoint").
# This account uses the stable API (verified live 2026-07-05). The stable
# quote endpoint takes ONE symbol per request on this plan (batch endpoint is
# plan-restricted; a comma list returns []), so the poller iterates symbols.
QUOTE_URL = "https://financialmodelingprep.com/stable/quote-short"
INTRADAY_URL = "https://financialmodelingprep.com/stable/historical-chart/1min"
DEFAULT_WS_URL = "wss://websockets.financialmodelingprep.com"

# On-demand 1-min bars: TTL cache window (≤ 1 request/symbol/TTL) and the hard
# per-request timeout. 15s TTL ≪ one minute bar, so the scanner always sees
# the current partial candle, while N quality-filter passes in one scan tick
# cost exactly one request per ticker.
ONDEMAND_TTL_S = 15.0
ONDEMAND_TIMEOUT_S = 6.0

_ET_TZ_NAME = "America/New_York"  # FMP intraday timestamps are US/Eastern


class FMPHTTPError(Exception):
    """Non-200 from an FMP REST endpoint (status kept for backoff decisions)."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = int(status)


def _env_api_key() -> str:
    return (os.environ.get("FMP_API_KEY", "") or "").strip()


def _poll_seconds_from_env() -> float:
    """FMP_POLL_SECONDS (default 5), floored at 1s — the batch quote poll is
    one request per cycle no matter how many symbols ride in it."""
    try:
        v = float(os.environ.get("FMP_POLL_SECONDS", "5") or 5)
    except (TypeError, ValueError):
        v = 5.0
    return max(1.0, v)


# ── Shared aiohttp session (polling + ws + on-demand bars) ──────────────────
_session_lock = threading.Lock()
_session_ref: tuple = (None, None)  # (event loop, aiohttp.ClientSession)


def _get_session():
    """One shared aiohttp.ClientSession per event loop. Recreated when the
    loop changed (sequential asyncio.run() calls in tests) or the session was
    closed — prod has one loop, so in practice this is ONE session reused by
    the quote poll, the ws connect and every on-demand bar fetch."""
    import aiohttp  # lazy: pinned dep, but only paid for when the feed is on

    global _session_ref
    loop = asyncio.get_running_loop()
    with _session_lock:
        ref_loop, sess = _session_ref
        if sess is not None and not sess.closed and ref_loop is loop:
            return sess
        sess = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        _session_ref = (loop, sess)
        return sess


async def close_shared_session() -> None:
    """Best-effort close (feed stop / tests)."""
    global _session_ref
    with _session_lock:
        _, sess = _session_ref
        _session_ref = (None, None)
    if sess is not None and not getattr(sess, "closed", True):
        try:
            await sess.close()
        except Exception:
            pass


# ── On-demand real-time 1-min bars (scanner confirmation) ───────────────────
_ondemand_lock = threading.Lock()
# sym -> (monotonic fetch time, parsed bars | None). A None payload is a
# FAILURE COOLDOWN: within the TTL we answer [] instead of re-hitting the API,
# so a broken endpoint / 429 storm can never be tight-looped by callers.
_ondemand_cache: dict[str, tuple[float, Optional[list]]] = {}


def clear_ondemand_cache() -> None:
    """Drop the on-demand bar cache (tests / ops)."""
    with _ondemand_lock:
        _ondemand_cache.clear()


def _et_datestr_to_ms(date_str: str) -> int:
    """'2026-07-02 09:35:00' (FMP intraday format, US/Eastern) -> epoch ms.
    Returns 0 on anything unparseable."""
    try:
        import zoneinfo

        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=zoneinfo.ZoneInfo(_ET_TZ_NAME))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _parse_intraday_rows(raw) -> list:
    """FMP /historical-chart rows (newest first, ET timestamps) -> Polygon-
    REST-aggs-shaped dicts sorted oldest→newest. The extra 'date_et' key
    ('YYYY-MM-DD') supports per-session filtering; consumers only read the
    aggs keys so it rides along harmlessly. Unusable rows are skipped."""
    if not isinstance(raw, list):
        return []
    out: list = []
    for row in raw:
        try:
            if not isinstance(row, dict):
                continue
            date_str = str(row.get("date") or "")
            t = _et_datestr_to_ms(date_str)
            c = float(row.get("close") or 0.0)
            if t <= 0 or c <= 0:
                continue
            out.append({
                "t": t,
                "e": t + 60_000,
                "o": float(row.get("open") or 0.0),
                "h": float(row.get("high") or 0.0),
                "l": float(row.get("low") or 0.0),
                "c": c,
                "v": float(row.get("volume") or 0.0),
                "vw": None,
                "date_et": date_str[:10],
            })
        except Exception:
            continue
    out.sort(key=lambda b: b["t"])
    return out


def _slice_bars(bars: list, n: Optional[int], date_et: Optional[str]) -> list:
    """Filter one ET session + keep the newest n, as dict copies."""
    out = bars or []
    if date_et:
        out = [b for b in out if b.get("date_et") == date_et]
    if n is not None and n > 0:
        out = out[-int(n):]
    return [dict(b) for b in out]


async def fetch_intraday_bars(
    symbol: str,
    n: Optional[int] = None,
    date_et: Optional[str] = None,
    ttl_s: float = ONDEMAND_TTL_S,
    timeout_s: float = ONDEMAND_TIMEOUT_S,
) -> list:
    """Real-time 1-min bars for an ARBITRARY ticker via FMP /historical-chart.

    Returns Polygon-REST-aggs-shaped dicts ({'t','e','o','h','l','c','v','vw'},
    oldest→newest) so every scanner helper parses them verbatim. `date_et`
    ('YYYY-MM-DD') filters to one ET session (FMP returns several days of
    bars); `n` keeps only the newest n AFTER the filter.

    Rate discipline: ≤ 1 HTTP request/symbol/`ttl_s` via the module cache —
    failures are cached too (cooldown, not retry-storm). Hard per-request
    timeout. NEVER raises: any failure returns [] so callers fall back to
    their delayed-REST path, exactly like the store helpers."""
    sym = (symbol or "").strip().upper()
    key = _env_api_key()
    if not sym or not key:
        return []
    now = time.monotonic()
    with _ondemand_lock:
        hit = _ondemand_cache.get(sym)
    if hit is not None and (now - hit[0]) < ttl_s:
        return _slice_bars(hit[1], n, date_et) if hit[1] else []

    bars: Optional[list] = None
    try:
        import aiohttp

        session = _get_session()
        url = INTRADAY_URL
        async with session.get(
            url,
            params={"symbol": sym, "apikey": key},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[fmp-feed] 1min bars {sym}: HTTP {resp.status} — cooling down {ttl_s:.0f}s")
            else:
                bars = _parse_intraday_rows(await resp.json(content_type=None))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[fmp-feed] 1min bars {sym} fetch failed ({type(e).__name__}: {e}) — cooling down {ttl_s:.0f}s")
    with _ondemand_lock:
        _ondemand_cache[sym] = (time.monotonic(), bars)
    return _slice_bars(bars, n, date_et) if bars else []


# ── POLYGON-EXIT: quote + settled-close helpers for the migrated call sites ─
# Every runtime Polygon dependency (P&L marks, trailing-stop watcher, pre-mkt
# confirmation, chart bars, systems-check) is FMP-primary when
# REALTIME_FEED=fmp, keeping its original Polygon code as the fallback. These
# helpers are that FMP layer. They NEVER raise — any failure returns
# None/{} so the caller falls through to Polygon exactly as before.

EOD_URL = "https://financialmodelingprep.com/stable/historical-price-eod/full"

# [fmp-primary] first-use log dedupe — exactly one line per call site per
# process, so prod logs show which migrated sites actually serve from FMP.
_fmp_primary_logged: set = set()
_fmp_primary_log_lock = threading.Lock()


def log_fmp_primary_once(site: str) -> None:
    """Emit the [fmp-primary] tag ONCE per call site (per process)."""
    with _fmp_primary_log_lock:
        if site in _fmp_primary_logged:
            return
        _fmp_primary_logged.add(site)
    logger.info(
        f"[fmp-primary] {site}: serving from FMP (REALTIME_FEED=fmp) — Polygon is fallback only"
    )


async def fetch_quote_short_price(symbol: str, timeout_s: float = 4.0) -> Optional[float]:
    """Async /stable/quote-short last price for ONE symbol (the stable batch
    endpoint is plan-restricted). None on any failure — callers keep their
    existing Polygon-snapshot fallback. Never raises."""
    sym = (symbol or "").strip().upper()
    key = _env_api_key()
    if not sym or not key:
        return None
    try:
        import aiohttp

        session = _get_session()
        async with session.get(
            QUOTE_URL,
            params={"symbol": sym, "apikey": key},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[fmp-feed] quote-short {sym}: HTTP {resp.status}")
                return None
            rows = await resp.json(content_type=None)
        if isinstance(rows, list) and rows:
            px = float(rows[0].get("price") or 0)
            return px if px > 0 else None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[fmp-feed] quote-short {sym} failed ({type(e).__name__}: {e})")
    return None


def fetch_quote_short_price_sync(symbol: str, timeout_s: float = 3.0) -> Optional[float]:
    """Sync twin of fetch_quote_short_price for the request/response P&L-mark
    paths that already make blocking `requests` calls today. Never raises."""
    sym = (symbol or "").strip().upper()
    key = _env_api_key()
    if not sym or not key:
        return None
    try:
        import requests as _rq

        r = _rq.get(QUOTE_URL, params={"symbol": sym, "apikey": key}, timeout=timeout_s)
        if r.status_code != 200:
            return None
        rows = r.json() or []
        if isinstance(rows, list) and rows:
            px = float(rows[0].get("price") or 0)
            return px if px > 0 else None
    except Exception as e:
        logger.warning(f"[fmp-feed] quote-short(sync) {sym} failed ({type(e).__name__}: {e})")
    return None


# Settled-close cache: symbol -> (monotonic_ts, close). A settled close only
# changes once per session, so a 10-min TTL keeps the outside-RTH P&L loops
# at ≤ 1 request/symbol/10min.
SETTLED_CLOSE_TTL_S = 600.0
_settled_close_cache: dict = {}
_settled_close_lock = threading.Lock()


def fetch_last_settled_close_sync(symbol: str, timeout_s: float = 4.0,
                                  ttl_s: float = SETTLED_CLOSE_TTL_S) -> Optional[float]:
    """Most recent SETTLED session close via /stable/historical-price-eod/full.
    This is the FMP equivalent of Polygon's PNL-MARK-FREEZE-V1 freeze source
    (day.c once today has settled, else prevDay.c): before today's settle the
    newest EOD row is yesterday's close; after settle it becomes today's.
    Never raises; None on any failure."""
    sym = (symbol or "").strip().upper()
    key = _env_api_key()
    if not sym or not key:
        return None
    now = time.monotonic()
    with _settled_close_lock:
        hit = _settled_close_cache.get(sym)
    if hit is not None and (now - hit[0]) < ttl_s:
        return hit[1]
    close: Optional[float] = None
    try:
        import requests as _rq

        r = _rq.get(EOD_URL, params={"symbol": sym, "apikey": key}, timeout=timeout_s)
        if r.status_code == 200:
            rows = r.json() or []
            if isinstance(rows, dict):  # tolerate a {"historical": [...]} wrapper
                rows = rows.get("historical") or []
            best_date = ""
            for row in rows or []:
                d = str(row.get("date") or "")
                c = row.get("close")
                if not d or c is None or d <= best_date:
                    continue
                try:
                    cf = float(c)
                except (TypeError, ValueError):
                    continue
                if cf > 0:
                    best_date, close = d, cf
    except Exception as e:
        logger.warning(f"[fmp-feed] settled-close {sym} failed ({type(e).__name__}: {e})")
    if close is not None:
        with _settled_close_lock:
            _settled_close_cache[sym] = (time.monotonic(), close)
    return close


def fmp_equity_snapshot_sync(symbol: str, session: str) -> dict:
    """Polygon-stocks-snapshot-shaped 'ticker' dict built from FMP, honoring
    the PNL-MARK-FREEZE-V1 session rule BY CONSTRUCTION:
      'regular' session -> {'lastTrade': {'p': quote-short price}}  (live mark)
      anything else     -> {'day': {'c': last settled EOD close}}   (frozen)
    Feeding the result to pnl_marks.pick_equity_mark() keeps the RTH-freeze
    semantics byte-identical: outside RTH the ONLY price present is the
    settled close, so an after-hours print can never move open P&L. Returns
    {} on any failure so callers fall through to the Polygon snapshot path."""
    if (session or "") == "regular":
        px = fetch_quote_short_price_sync(symbol)
        return {"lastTrade": {"p": px}} if px else {}
    close = fetch_last_settled_close_sync(symbol)
    return {"day": {"c": close}} if close else {}


# ── The feed ────────────────────────────────────────────────────────────────
class FMPRealtimeFeed(RealtimeFeed):
    """FMP batch-quote poller (+ optional ws) → LatestBarStore.

    Poll protocol: GET quote-short/{SYM1,SYM2,...} returns
    [{"symbol","price","volume"}, ...] where volume is the CUMULATIVE day
    volume. Each quote is folded into its wall-clock minute bucket; the bucket
    is pushed to the store on every update (LatestBarStore.add_bar REPLACES
    same-minute bars, so consumers always see the freshest partial candle).

    Ws protocol (only when FMP_WEBSOCKET=1):
      ← {"event":"login","data":{"apiKey": key}}
      → {"event":"login","status":200,...}   (401/unauthorized = not entitled)
      ← {"event":"subscribe","data":{"ticker":["aapl",...]}}
      → trade ticks {"s":"aapl","type":"T","lp":<price>,"ls":<size>,...}
    Entitlement rejection logs ONCE and permanently disables ws for this run;
    polling continues either way.
    """

    def __init__(
        self,
        store: LatestBarStore,
        api_key: Optional[str] = None,
        symbols: Optional[Iterable[str]] = None,
        poll_seconds: Optional[float] = None,
    ):
        self.store = store
        self._api_key = api_key if api_key is not None else _env_api_key()
        self._poll_seconds = (
            max(1.0, float(poll_seconds)) if poll_seconds is not None else _poll_seconds_from_env()
        )
        self._ws_enabled = (os.environ.get("FMP_WEBSOCKET", "0") or "0").strip() == "1"
        self._ws_url = os.environ.get("FMP_WS_URL", "") or DEFAULT_WS_URL
        # Backoff knobs — same envs (and same formula) as the Polygon feed.
        self._backoff_base_s = float(os.environ.get("REALTIME_BACKOFF_BASE_S", "2"))
        self._backoff_cap_s = float(os.environ.get("REALTIME_BACKOFF_CAP_S", "60"))
        # Subscription set: rebuilt into the batch URL every poll, so
        # subscribe() is a pure set-add — no wakeup signalling needed.
        self._sub_lock = threading.Lock()
        self._desired: set[str] = {
            str(s).strip().upper() for s in (symbols or []) if s and str(s).strip()
        }
        # Minute aggregation state.
        self._agg_lock = threading.Lock()
        self._agg: dict[str, dict] = {}            # sym -> in-progress minute bar
        self._last_cum_vol: dict[str, float] = {}  # sym -> last cumulative day volume
        self._last_static: dict = {}  # sym -> (price, cum_vol) of last observation (freshness guard)
        # Run state.
        self._stopping = False
        self._consec_errors = 0
        self._last_poll_ok_mono = 0.0
        self._polls_total = 0
        # Ws state.
        self._ws_task: Optional[asyncio.Task] = None
        self._ws = None
        self._ws_authed = False
        self._ws_auth_rejected = False
        self._ws_last_data_mono = 0.0
        self._ws_subscribed: set[str] = set()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self._api_key:
            # Clean return = "don't restart" to the supervisor.
            logger.warning("[fmp-feed] REALTIME_FEED=fmp but FMP_API_KEY is empty — feed disabled")
            return
        self._stopping = False
        logger.info(
            f"[fmp-feed] FMP feed starting: poll every {self._poll_seconds:g}s, "
            f"ws={'on' if self._ws_enabled else 'off'}, symbols={sorted(self._desired)}"
        )
        if self._ws_enabled:
            self._ws_task = asyncio.create_task(self._ws_loop())
        try:
            await self._poll_loop()
        finally:
            task, self._ws_task = self._ws_task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        logger.info("[fmp-feed] FMP feed stopped")

    async def stop(self) -> None:
        self._stopping = True
        task = self._ws_task
        if task is not None:
            task.cancel()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        await close_shared_session()

    def subscribe(self, symbols: Iterable[str]) -> None:
        cleaned = {str(s).strip().upper() for s in (symbols or []) if s and str(s).strip()}
        if not cleaned:
            return
        with self._sub_lock:
            self._desired |= cleaned
        # Takes effect on the NEXT poll cycle (≤ poll_seconds away): the batch
        # URL is rebuilt from _desired every time. The ws flush reads it too.

    def healthy(self) -> bool:
        """A recent successful poll (or a delivering ws) = healthy. Quiet tape
        with the poll succeeding is HEALTHY — closed markets aren't an outage."""
        if self._stopping:
            return False
        horizon = max(30.0, self._poll_seconds * 3)
        if self._last_poll_ok_mono and (time.monotonic() - self._last_poll_ok_mono) <= horizon:
            return True
        return self._ws_delivering()

    # ── polling ──────────────────────────────────────────────────────────
    def _error_backoff_delay(self, attempt: int) -> float:
        """Exponential backoff, hard-capped: base · 2^attempt, ≤ cap."""
        try:
            return min(self._backoff_base_s * (2 ** max(0, int(attempt))), self._backoff_cap_s)
        except OverflowError:
            return self._backoff_cap_s

    async def _poll_loop(self) -> None:
        while not self._stopping:
            delay = self._poll_seconds
            try:
                if self._ws_delivering():
                    # Ws stream is live for the subscribed set — skip this
                    # cycle's REST request entirely (rate discipline; also
                    # avoids double-counting volume into the same buckets).
                    pass
                else:
                    await self._poll_once()
                    self._consec_errors = 0
                    self._last_poll_ok_mono = time.monotonic()
            except asyncio.CancelledError:
                raise  # lifespan shutdown — propagate
            except FMPHTTPError as e:
                self._consec_errors += 1
                delay = max(delay, self._error_backoff_delay(self._consec_errors - 1))
                logger.warning(
                    f"[fmp-feed] quote poll HTTP {e.status} — backing off {delay:.0f}s "
                    f"(consecutive errors: {self._consec_errors})"
                )
            except Exception as e:
                self._consec_errors += 1
                delay = max(delay, self._error_backoff_delay(self._consec_errors - 1))
                logger.warning(
                    f"[fmp-feed] quote poll failed ({type(e).__name__}: {e}) — retrying in {delay:.0f}s"
                )
            if self._stopping:
                break
            await asyncio.sleep(delay)

    async def _poll_once(self) -> int:
        """ONE batch quote-short request for the whole desired set (1 request
        per cycle regardless of symbol count). Returns quotes ingested. Raises
        FMPHTTPError on non-200 so the loop can back off."""
        with self._sub_lock:
            syms = sorted(self._desired)
        if not syms:
            return 0
        import aiohttp

        session = _get_session()
        # Stable API: one symbol per request (plan has no batch endpoint).
        # N small requests per cycle; ~2-10 symbols keeps us far under FMP
        # per-minute limits. First failure raises so the loop backs off.
        ingested = 0
        for sym in syms:
            async with session.get(
                QUOTE_URL,
                params={"symbol": sym, "apikey": self._api_key},
                timeout=aiohttp.ClientTimeout(total=max(8.0, self._poll_seconds)),
            ) as resp:
                if resp.status != 200:
                    raise FMPHTTPError(resp.status)
                raw = await resp.json(content_type=None)
            ingested += self._ingest_quote_payload(raw)
        self._polls_total += 1
        if self._polls_total == 1 or self._polls_total % 120 == 0:
            logger.info(
                f"[fmp-feed] quote poll #{self._polls_total}: {ingested}/{len(syms)} symbols "
                f"ingested (store symbols: {len(self.store.symbols())})"
            )
        return ingested

    def _ingest_quote_payload(self, raw, ts_s: Optional[float] = None) -> int:
        """Parse one quote-short JSON payload into minute buckets. Returns the
        number of quotes ingested (test hook). NEVER raises — one malformed
        entry must not kill the poll loop."""
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            logger.warning(f"[fmp-feed] unexpected quote payload shape: {str(raw)[:200]!r}")
            return 0
        now_s = float(ts_s) if ts_s is not None else time.time()
        ingested = 0
        for q in raw:
            try:
                if not isinstance(q, dict):
                    continue
                sym = str(q.get("symbol") or "").upper()
                price = float(q.get("price") or 0.0)
                if not sym or price <= 0:
                    continue  # unusable quote — skip, don't poison the store
                vol = q.get("volume")
                cum_vol = float(vol) if vol is not None else None
                self._ingest_tick(sym, price, cum_vol=cum_vol, ts_s=now_s)
                ingested += 1
            except Exception as e:
                logger.warning(f"[fmp-feed] bad quote entry skipped: {type(e).__name__}: {e}")
                continue
        return ingested

    def _ingest_tick(
        self,
        sym: str,
        price: float,
        cum_vol: Optional[float] = None,
        trade_size: Optional[float] = None,
        ts_s: Optional[float] = None,
    ) -> dict:
        """Fold one observation (poll quote or ws trade) into its wall-clock
        minute bucket and push the bucket to the store (same-minute pushes
        REPLACE — LatestBarStore.add_bar). Volume semantics:
          * cum_vol (quote-short 'volume' = CUMULATIVE day volume): the bucket
            accumulates the positive delta vs the previous observation. The
            first-ever observation contributes 0 (no baseline yet); a negative
            delta (day roll / vendor reset) also contributes 0.
          * trade_size (ws 'ls'): added directly.
          * neither present -> v stays 0 (endpoint without volume: documented
            in the runbook — bar-shape consumers treat v=0 as "no volume info").
        Returns a snapshot of the bucket (test hook)."""
        now_s = float(ts_s) if ts_s is not None else time.time()
        minute_ms = int(now_s // 60) * 60_000
        with self._agg_lock:
            # CLOSED-MARKET FRESHNESS GUARD (review 2026-07-05 issue #2): a
            # static quote (same price AND same cumulative volume as the last
            # observation, and no ws trade tick) means the market is not
            # printing — do NOT mint a new bucket, or weekends/overnight would
            # read as fresh 24/7 and defeat the staleness gate. Real sessions
            # always move one of the two within a poll interval.
            if not trade_size:
                last = self._last_static.get(sym)
                if last is not None and last == (price, cum_vol):
                    return dict(self._agg.get(sym) or {})
                self._last_static[sym] = (price, cum_vol)
            bar = self._agg.get(sym)
            if bar is None or int(bar["t"]) != minute_ms:
                bar = {
                    "t": minute_ms,
                    "e": minute_ms + 60_000,
                    "o": price, "h": price, "l": price, "c": price,
                    "v": 0.0,
                    "vw": None,
                }
                self._agg[sym] = bar
            else:
                if price > bar["h"]:
                    bar["h"] = price
                if price < bar["l"]:
                    bar["l"] = price
                bar["c"] = price
            if cum_vol is not None:
                prev = self._last_cum_vol.get(sym)
                if prev is not None and cum_vol >= prev:
                    bar["v"] = float(bar["v"]) + (cum_vol - prev)
                self._last_cum_vol[sym] = cum_vol
            if trade_size:
                try:
                    bar["v"] = float(bar["v"]) + max(0.0, float(trade_size))
                except (TypeError, ValueError):
                    pass
            snapshot = dict(bar)
        self.store.add_bar(sym, snapshot)
        return snapshot

    # ── websocket (optional layer) ───────────────────────────────────────
    def _ws_delivering(self) -> bool:
        """Authed ws that produced a tick recently — used to idle the poller."""
        if not (self._ws_enabled and self._ws_authed and not self._ws_auth_rejected):
            return False
        horizon = max(30.0, self._poll_seconds * 3)
        return bool(
            self._ws_last_data_mono
            and (time.monotonic() - self._ws_last_data_mono) <= horizon
        )

    async def _ws_loop(self) -> None:
        """Best-effort ws layer. Entitlement rejection is logged ONCE and
        disables ws for the process lifetime; network blips reconnect with the
        capped exponential backoff. Either way the poll loop is unaffected."""
        attempt = 0
        while not self._stopping and not self._ws_auth_rejected:
            try:
                await self._ws_session_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                attempt += 1
                logger.warning(f"[fmp-feed] ws session died: {type(e).__name__}: {e}")
            finally:
                self._ws = None
                self._ws_authed = False
                self._ws_subscribed.clear()
            if self._stopping or self._ws_auth_rejected:
                break
            await asyncio.sleep(self._error_backoff_delay(attempt))
        if self._ws_auth_rejected:
            logger.info("[fmp-feed] ws layer OFF for this run — REST polling carries the feed")

    async def _ws_session_once(self) -> None:
        import aiohttp

        session = _get_session()
        async with session.ws_connect(self._ws_url, heartbeat=30) as ws:
            self._ws = ws
            logger.info(f"[fmp-feed] ws connected: {self._ws_url}")
            await ws.send_json({"event": "login", "data": {"apiKey": self._api_key}})
            while not self._stopping:
                try:
                    msg = await ws.receive(timeout=10.0)
                except asyncio.TimeoutError:
                    await self._ws_flush_subscriptions(ws)
                    continue
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_ws_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.warning(f"[fmp-feed] ws error frame: {ws.exception()}")
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    logger.info("[fmp-feed] ws closed by server")
                    break
                if self._ws_auth_rejected:
                    break  # permanent — the outer loop exits
                if self._ws_authed:
                    await self._ws_flush_subscriptions(ws)
        logger.info("[fmp-feed] ws disconnected")

    async def _ws_flush_subscriptions(self, ws) -> None:
        """Send one subscribe frame for anything desired-but-not-subscribed."""
        if not self._ws_authed:
            return
        with self._sub_lock:
            pending = sorted(self._desired - self._ws_subscribed)
            if not pending:
                return
            self._ws_subscribed |= set(pending)
        try:
            await ws.send_json({"event": "subscribe", "data": {"ticker": [s.lower() for s in pending]}})
            logger.info(f"[fmp-feed] ws subscribed: {','.join(pending)}")
        except Exception as e:
            with self._sub_lock:
                self._ws_subscribed -= set(pending)  # retry on the next flush
            logger.warning(f"[fmp-feed] ws subscribe send failed: {e}")

    def _handle_ws_message(self, raw: str) -> int:
        """Parse one ws TEXT frame (login/acks/trade ticks) — returns ticks
        ingested (test hook). NEVER raises."""
        try:
            data = json.loads(raw)
        except Exception:
            logger.warning(f"[fmp-feed] unparseable ws frame: {str(raw)[:200]!r}")
            return 0
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return 0
        ingested = 0
        for ev in data:
            try:
                if not isinstance(ev, dict):
                    continue
                if ev.get("event") == "login":
                    self._handle_ws_login(ev)
                    continue
                if ev.get("event"):
                    # subscribe acks / heartbeats / server errors.
                    status = ev.get("status")
                    message = str(ev.get("message") or "")
                    if "unauthorized" in message.lower() or status in (401, "401"):
                        self._ws_reject(message or f"status {status}")
                    else:
                        logger.info(f"[fmp-feed] ws event {ev.get('event')}: {message or status}")
                    continue
                sym = str(ev.get("s") or "").upper()
                lp = ev.get("lp")
                if not sym or lp is None:
                    continue
                price = float(lp)
                if price <= 0:
                    continue
                self._ingest_tick(sym, price, trade_size=ev.get("ls"))
                ingested += 1
            except Exception as ev_exc:
                logger.warning(f"[fmp-feed] bad ws event skipped: {type(ev_exc).__name__}: {ev_exc}")
                continue
        if ingested:
            self._ws_last_data_mono = time.monotonic()
        return ingested

    def _handle_ws_login(self, ev: dict) -> None:
        status = ev.get("status")
        message = str(ev.get("message") or "")
        ok = status in (200, "200") or "connected" in message.lower() or "success" in message.lower()
        if ok:
            self._ws_authed = True
            logger.info("[fmp-feed] ws AUTHENTICATED — trade stream live (poller idles while it delivers)")
        else:
            self._ws_reject(message or f"status {status}")

    def _ws_reject(self, why: str) -> None:
        """Entitlement rejection: log ONCE, disable ws for this run. The poll
        loop never notices — REST polling is the guaranteed transport."""
        if not self._ws_auth_rejected:
            logger.warning(
                f"[fmp-feed] ws login rejected ({why}) — the FMP plan has no ws "
                "entitlement. ws disabled for this run; REST polling continues unchanged."
            )
        self._ws_auth_rejected = True
        self._ws_authed = False


# Daily-bars cache: (symbol,start,end) -> (monotonic_ts, rows). Daily history
# only changes once per session, so a 10-min TTL matches the settled-close cache.
DAILY_BARS_TTL_S = 600.0
_daily_bars_cache: dict = {}
_daily_bars_lock = threading.Lock()


def fetch_daily_bars_sync(symbol: str, start_iso: str, end_iso: str,
                          timeout_s: float = 6.0) -> list:
    """Daily OHLCV via /stable/historical-price-eod/full, mapped to the
    Polygon aggs row shape ({t,o,h,l,c,v}, ascending) so Polygon call sites
    can fall back without changes. [] on any failure. Never raises."""
    import time as _time

    sym = (symbol or "").strip().upper()
    key = _env_api_key()
    if not sym or not key or not start_iso or not end_iso:
        return []
    ck = (sym, start_iso, end_iso)
    now = _time.monotonic()
    with _daily_bars_lock:
        hit = _daily_bars_cache.get(ck)
        if hit and (now - hit[0]) < DAILY_BARS_TTL_S:
            return hit[1]
    try:
        import requests as _rq

        r = _rq.get(
            "https://financialmodelingprep.com/stable/historical-price-eod/full",
            params={"symbol": sym, "from": start_iso, "to": end_iso, "apikey": key},
            timeout=timeout_s,
        )
        if r.status_code != 200:
            logger.warning(f"[fmp-feed] daily-bars {sym}: HTTP {r.status_code}")
            return []
        rows = r.json() or []
        out = []
        for row in rows if isinstance(rows, list) else []:
            d, c = row.get("date"), row.get("close")
            if not d or c is None:
                continue
            out.append({"t": _et_datestr_to_ms(str(d)[:10]), "o": row.get("open"),
                        "h": row.get("high"), "l": row.get("low"),
                        "c": float(c), "v": float(row.get("volume") or 0)})
        out.sort(key=lambda b: b["t"])  # FMP returns newest-first
        with _daily_bars_lock:
            _daily_bars_cache[ck] = (now, out)
        return out
    except Exception as e:
        logger.warning(f"[fmp-feed] daily-bars {sym} failed ({type(e).__name__}: {e})")
        return []

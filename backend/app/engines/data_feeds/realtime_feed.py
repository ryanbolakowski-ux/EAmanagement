"""Real-time minute-bar feed + bounded in-process bar store (REALTIME-FEED-V1).

WHY THIS EXISTS: every "live" surface in the platform is actually delayed REST
polling — the prod Polygon key is the 15-min-DELAYED tier, so the futures
signal proxy, the Theta-scanner confirmation bars and the landing-page tape
all lag the market by up to ~15 minutes (root cause of the ~09:50
confirmations and the morning-pick "no intraday bars" dark days).

This module is the ONE-ENV-FLIP fix: the moment the vendor key is entitled
for real-time data, setting REALTIME_FEED=polygon streams AM.* (minute
aggregate) websocket events into an in-process LatestBarStore that the
delayed consumers (public tape, futures signal proxy, scanner confirmation)
prefer over their REST paths whenever the store is FRESH (newest bar end
<= 120s old). Go-live checklist: docs/v2/11-realtime-feed-runbook.md.

Design rules (same philosophy as app.core.task_supervisor):
  * DEFAULT OFF. With REALTIME_FEED unset, every helper here returns []/None
    immediately and no socket is ever opened — consumers behave
    byte-identically to today.
  * Provider-agnostic: consumers only ever touch the LatestBarStore + the
    module-level get_fresh_bars()/get_fresh_price() helpers. Polygon is just
    the first RealtimeFeed subclass; a different vendor is a new subclass +
    one branch in create_feed_from_env().
  * Graceful entitlement failure: the key NOT being ws-entitled yet is the
    EXPECTED state today. auth_failed / "not authorized" logs one clear
    warning and retries on a slow cadence (default 15 min) — no crash-loop,
    no supervisor restart-budget burn, consumers keep their REST paths.
  * Stale beats wrong: a store older than STALE_AFTER_S is treated as EMPTY.
    Worst case is always today's behavior (delayed REST), never worse.
  * No new dependencies: the ws client is aiohttp.ClientSession.ws_connect
    (aiohttp==3.13.5 is already pinned and used by scanner/live_trading).
"""
import asyncio
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Iterable, Optional

from loguru import logger

# ── Flag / env plumbing ─────────────────────────────────────────────────────
# REALTIME_FEED: "" (off — the default) | "polygon" | "fmp". Read at CALL
# time, not import time, so a compose env flip + restart (and monkeypatch in
# tests) is all it takes. The FMP provider (REALTIME-FEED-FMP: batch-quote
# REST polling -> minute buckets -> this same store, optional ws, plus
# on-demand REAL-TIME 1-min bars for arbitrary scanner candidates) lives in
# app/engines/data_feeds/fmp_feed.py and is lazy-imported only when selected.
_FLAG_ENV = "REALTIME_FEED"
_SYMBOLS_ENV = "REALTIME_SYMBOLS"
# Default subscription set: the two headline futures ETF proxies (QQQ→NQ,
# SPY→ES). Everything else is added dynamically at runtime via
# request_symbols(): the futures runner subscribes IWM/DIA (RTY/YM proxies)
# on its first poll, the scanner subscribes its candidate tickers, the tape
# subscribes its stock rows — no need to enumerate any of them here.
_DEFAULT_SYMBOLS = "QQQ,SPY"

# A store entry whose newest bar ended more than this many seconds ago is
# STALE: consumers must fall back to their existing REST path (at worst
# ~15 min delayed — i.e. never worse than today). 120s = two missed minute
# bars, which on the ws feed means the stream is broken, not just quiet.
STALE_AFTER_S = 120.0

# Bars kept per symbol. 500 minutes covers 04:00 ET premarket open through
# ~12:20 ET in one unbroken run — plenty for session VWAP and any resample
# the consumers do, while keeping the store's memory strictly bounded.
MAX_BARS_PER_SYMBOL = 500


def realtime_provider() -> str:
    """The configured provider name, lowercased ('' = feed off)."""
    return (os.environ.get(_FLAG_ENV, "") or "").strip().lower()


def realtime_enabled() -> bool:
    """True only when a known provider is configured. Checked at CALL time by
    every consumer helper so flag-off behavior is byte-identical to today."""
    return realtime_provider() in ("polygon", "fmp")


def _symbols_from_env() -> list[str]:
    raw = os.environ.get(_SYMBOLS_ENV, "") or _DEFAULT_SYMBOLS
    return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})


# ── LatestBarStore ──────────────────────────────────────────────────────────
class LatestBarStore:
    """Bounded, thread/async-safe in-process store of the freshest minute bars.

    Per symbol: a deque of the last `max_bars` minute bars + the last quote
    (close of the newest bar). Bars are stored in the SAME dict shape as
    Polygon's REST v2 aggs results ({'t','o','h','l','c','v','vw'} + 'e' for
    the bar END ms) so consumers that already parse REST bars (scanner VWAP
    helpers, the runner's proxy resample) can use them verbatim.

    All mutation happens under one threading.RLock: the event-loop feed task
    writes while the sync watcher threads (asyncio.to_thread) read. Every
    operation is a few dict/deque ops — no I/O — so holding the lock from the
    event loop is safe.
    """

    def __init__(self, max_bars: int = MAX_BARS_PER_SYMBOL):
        if max_bars <= 0:
            raise ValueError("max_bars must be positive")
        self._max_bars = int(max_bars)
        self._lock = threading.RLock()
        self._bars: dict[str, deque] = {}
        # sym -> (last_price, bar_end_epoch_s) — the "last quote".
        self._quotes: dict[str, tuple[float, float]] = {}
        # sym -> time.monotonic() of last ingest (freshness fallback when a
        # bar arrives without usable timestamps).
        self._recv_mono: dict[str, float] = {}

    def add_bar(self, symbol: str, bar: dict) -> None:
        """Ingest one minute bar (REST-aggs dict shape). Same-minute updates
        REPLACE the existing bar; out-of-order older bars are dropped so the
        deque stays chronologically sorted."""
        sym = (symbol or "").upper()
        if not sym or not isinstance(bar, dict):
            return
        with self._lock:
            dq = self._bars.get(sym)
            if dq is None:
                dq = deque(maxlen=self._max_bars)
                self._bars[sym] = dq
            t = int(bar.get("t") or 0)
            if dq:
                last_t = int(dq[-1].get("t") or 0)
                if t and t == last_t:
                    dq[-1] = bar  # same-minute refresh (vendor re-send)
                elif t and t < last_t:
                    return  # out-of-order — keep the deque monotonic
                else:
                    dq.append(bar)
            else:
                dq.append(bar)
            try:
                close = float(bar.get("c") or 0.0)
            except (TypeError, ValueError):
                close = 0.0
            end_ms = int(bar.get("e") or 0) or (t + 60_000 if t else 0)
            if close > 0:
                self._quotes[sym] = (close, end_ms / 1000.0)
            self._recv_mono[sym] = time.monotonic()

    def get_recent_bars(self, symbol: str, n: Optional[int] = None) -> list:
        """Last `n` bars (all if n is None), oldest→newest, as dict COPIES so
        callers can't mutate the store."""
        sym = (symbol or "").upper()
        with self._lock:
            dq = self._bars.get(sym)
            if not dq:
                return []
            bars = list(dq)
        if n is not None and n > 0:
            bars = bars[-int(n):]
        return [dict(b) for b in bars]

    def get_last_price(self, symbol: str) -> Optional[float]:
        sym = (symbol or "").upper()
        with self._lock:
            q = self._quotes.get(sym)
        return q[0] if q else None

    def age_seconds(self, symbol: str) -> Optional[float]:
        """Seconds since the END of the newest bar (wall clock). None if the
        store has nothing for the symbol. Falls back to receipt time when the
        bar carried no usable timestamp."""
        sym = (symbol or "").upper()
        with self._lock:
            q = self._quotes.get(sym)
            recv = self._recv_mono.get(sym)
            has_bars = bool(self._bars.get(sym))
        if not has_bars:
            return None
        if q and q[1] > 0:
            return max(0.0, time.time() - q[1])
        if recv is not None:
            return max(0.0, time.monotonic() - recv)
        return None

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._bars.keys())

    def clear(self) -> None:
        """Drop everything (tests / ops)."""
        with self._lock:
            self._bars.clear()
            self._quotes.clear()
            self._recv_mono.clear()


# ── Provider-agnostic feed contract ─────────────────────────────────────────
class RealtimeFeed(ABC):
    """Contract every realtime vendor implementation must satisfy. Consumers
    never touch a concrete feed directly — they read the LatestBarStore via
    the module helpers below — so swapping vendors is invisible to them."""

    store: LatestBarStore

    @abstractmethod
    async def start(self) -> None:
        """Connect + stream until stop(). This is the coroutine handed to
        task_supervisor.supervise() — it must swallow routine disconnects
        itself (reconnect w/ backoff) and only raise on truly fatal bugs."""

    @abstractmethod
    async def stop(self) -> None:
        """Signal the run loop to exit and close the socket (best-effort)."""

    @abstractmethod
    def subscribe(self, symbols: Iterable[str]) -> None:
        """Add symbols to the subscription set. Sync + thread-safe so both
        async routes and to_thread workers can call it; takes effect on the
        next flush (a few seconds) or on reconnect."""

    @abstractmethod
    def healthy(self) -> bool:
        """Socket up + authenticated. Quiet tape is NOT unhealthy."""


# ── Polygon implementation ──────────────────────────────────────────────────
class PolygonRealtimeFeed(RealtimeFeed):
    """Polygon stocks-cluster websocket → LatestBarStore.

    Protocol (wss://socket.polygon.io/stocks):
      → connect: server pushes {"ev":"status","status":"connected"}
      ← {"action":"auth","params":"<API key>"}
      → {"ev":"status","status":"auth_success"} (or "auth_failed")
      ← {"action":"subscribe","params":"AM.QQQ,AM.SPY"}
      → a stream of {"ev":"AM","sym":...,"o","h","l","c","v","vw",
                     "s":<start ms>,"e":<end ms>} once per symbol per minute.

    ENTITLEMENT: on the current (delayed REST) plan the socket answers
    auth/subscribe with auth_failed / "not authorized". That is handled as a
    SLOW-RETRY state (default every 15 min), logged clearly, never raised —
    so the flag can even be flipped a day early without harm.
    """

    DEFAULT_URL = "wss://socket.polygon.io/stocks"

    def __init__(
        self,
        store: LatestBarStore,
        api_key: Optional[str] = None,
        symbols: Optional[Iterable[str]] = None,
        url: Optional[str] = None,
    ):
        self.store = store
        self._api_key = api_key if api_key is not None else os.environ.get("POLYGON_API_KEY", "")
        self._url = url or os.environ.get("POLYGON_WS_URL", "") or self.DEFAULT_URL
        self._sub_lock = threading.Lock()
        self._desired: set[str] = {s.strip().upper() for s in (symbols or []) if s and s.strip()}
        self._subscribed: set[str] = set()
        # Reconnect/backoff knobs (env-tunable, sane defaults).
        self._backoff_base_s = float(os.environ.get("REALTIME_BACKOFF_BASE_S", "2"))
        self._backoff_cap_s = float(os.environ.get("REALTIME_BACKOFF_CAP_S", "60"))
        self._auth_retry_s = float(os.environ.get("REALTIME_AUTH_RETRY_S", "900"))
        self._recv_timeout_s = float(os.environ.get("REALTIME_RECV_TIMEOUT_S", "10"))
        # Session state.
        self._stopping = False
        self._connected = False
        self._authed = False
        self._auth_failed = False
        self._attempt = 0            # consecutive failed sessions (backoff exponent)
        self._bars_total = 0
        self._last_data_mono = 0.0
        self._ws = None

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self._api_key:
            # Nothing to connect with — log once and exit cleanly (the
            # supervisor treats a clean return as "don't restart").
            logger.warning("[realtime-feed] REALTIME_FEED=polygon but POLYGON_API_KEY is empty — feed disabled")
            return
        self._stopping = False
        logger.info(f"[realtime-feed] polygon ws feed starting (symbols={sorted(self._desired)})")
        while not self._stopping:
            try:
                await self._run_session()
            except asyncio.CancelledError:
                raise  # lifespan shutdown — propagate
            except Exception as e:
                # DNS/TLS/network blips, protocol junk — all retryable.
                logger.warning(f"[realtime-feed] ws session died: {type(e).__name__}: {e}")
            finally:
                self._connected = False
                self._authed = False
                self._ws = None
                with self._sub_lock:
                    self._subscribed.clear()  # reconnect resubscribes everything
            if self._stopping:
                break
            delay = self._next_delay()
            if self._auth_failed:
                logger.warning(
                    "[realtime-feed] polygon ws NOT AUTHORIZED — the API key has no "
                    f"real-time ws entitlement yet. Consumers stay on their REST paths; "
                    f"retrying in {delay:.0f}s. (Expected until the plan upgrade — see "
                    "docs/v2/11-realtime-feed-runbook.md.)"
                )
            else:
                logger.info(f"[realtime-feed] disconnected — reconnecting in {delay:.0f}s (attempt {self._attempt + 1})")
            await asyncio.sleep(delay)
            self._attempt += 1
        logger.info("[realtime-feed] polygon ws feed stopped")

    async def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def subscribe(self, symbols: Iterable[str]) -> None:
        cleaned = {str(s).strip().upper() for s in (symbols or []) if s and str(s).strip()}
        if not cleaned:
            return
        with self._sub_lock:
            self._desired |= cleaned
        # The run loop flushes (desired - subscribed) after every message and
        # on every receive timeout (<= _recv_timeout_s later), so no wakeup
        # signalling is needed here — keeps this callable from sync threads.

    def healthy(self) -> bool:
        return bool(self._connected and self._authed and not self._auth_failed)

    # ── internals ────────────────────────────────────────────────────────
    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff, hard-capped: base * 2^attempt, <= cap."""
        try:
            return min(self._backoff_base_s * (2 ** max(0, int(attempt))), self._backoff_cap_s)
        except OverflowError:
            return self._backoff_cap_s

    def _next_delay(self) -> float:
        if self._auth_failed:
            # Not-entitled is a CONFIG state, not an outage: poll slowly so we
            # pick the entitlement up when it lands without hammering the
            # socket or burning supervisor restarts in the meantime.
            return self._auth_retry_s
        return self._backoff_delay(self._attempt)

    async def _run_session(self) -> None:
        # Lazy import: aiohttp is a pinned requirement (used by scanner /
        # live_trading), but the feed is off by default — don't pay for it
        # (or fail on it) unless the flag is actually flipped.
        import aiohttp

        self._auth_failed = False
        timeout = aiohttp.ClientTimeout(total=None, connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(self._url, heartbeat=30) as ws:
                self._ws = ws
                self._connected = True
                logger.info(f"[realtime-feed] ws connected: {self._url}")
                await ws.send_json({"action": "auth", "params": self._api_key})
                while not self._stopping:
                    try:
                        msg = await ws.receive(timeout=self._recv_timeout_s)
                    except asyncio.TimeoutError:
                        # Quiet tape (overnight/premarket lulls) — use the gap
                        # to flush subscribe() calls from other tasks/threads.
                        await self._flush_subscriptions(ws)
                        continue
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning(f"[realtime-feed] ws error frame: {ws.exception()}")
                        break
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        logger.info("[realtime-feed] ws closed by server")
                        break
                    if self._auth_failed:
                        break  # slow-retry handled by the outer loop
                    if self._authed:
                        await self._flush_subscriptions(ws)
        logger.info("[realtime-feed] ws disconnected")

    async def _flush_subscriptions(self, ws) -> None:
        """Send one subscribe frame for anything desired-but-not-subscribed."""
        if not self._authed:
            return
        with self._sub_lock:
            pending = sorted(self._desired - self._subscribed)
            if not pending:
                return
            self._subscribed |= set(pending)
        params = ",".join(f"AM.{s}" for s in pending)
        try:
            await ws.send_json({"action": "subscribe", "params": params})
            logger.info(f"[realtime-feed] subscribed: {params}")
        except Exception as e:
            with self._sub_lock:
                self._subscribed -= set(pending)  # retry on the next flush
            logger.warning(f"[realtime-feed] subscribe send failed: {e}")

    def _handle_message(self, raw: str) -> int:
        """Parse one ws TEXT frame (a JSON array of events) into the store.
        Returns the number of bars ingested (test hook). NEVER raises — one
        malformed event must not kill the stream."""
        try:
            events = json.loads(raw)
        except Exception:
            logger.warning(f"[realtime-feed] unparseable ws frame: {str(raw)[:200]!r}")
            return 0
        if isinstance(events, dict):
            events = [events]
        if not isinstance(events, list):
            return 0
        ingested = 0
        for ev in events:
            try:
                if not isinstance(ev, dict):
                    continue
                kind = ev.get("ev")
                if kind == "AM":
                    sym = str(ev.get("sym") or "").upper()
                    if not sym:
                        continue
                    # Map the ws AM event to the REST v2-aggs dict shape the
                    # consumers already parse ('t' = bar START ms), keeping
                    # 'e' (bar END ms) as the freshness anchor.
                    bar = {
                        "t": int(ev.get("s") or 0),
                        "e": int(ev.get("e") or 0),
                        "o": float(ev.get("o") or 0.0),
                        "h": float(ev.get("h") or 0.0),
                        "l": float(ev.get("l") or 0.0),
                        "c": float(ev.get("c") or 0.0),
                        "v": float(ev.get("v") or 0.0),
                        "vw": float(ev["vw"]) if ev.get("vw") is not None else None,
                    }
                    if bar["t"] <= 0 or bar["c"] <= 0:
                        continue  # unusable bar — skip, don't poison the store
                    self.store.add_bar(sym, bar)
                    ingested += 1
                    self._bars_total += 1
                    if self._bars_total % 1000 == 0:
                        logger.info(
                            f"[realtime-feed] {self._bars_total} bars ingested "
                            f"(symbols in store: {len(self.store.symbols())})"
                        )
                elif kind == "status":
                    self._handle_status(ev)
            except Exception as ev_exc:
                logger.warning(f"[realtime-feed] bad ws event skipped: {type(ev_exc).__name__}: {ev_exc}")
                continue
        if ingested:
            self._last_data_mono = time.monotonic()
        return ingested

    def _handle_status(self, ev: dict) -> None:
        status = str(ev.get("status") or "").lower()
        message = str(ev.get("message") or "")
        if status == "connected":
            logger.info(f"[realtime-feed] polygon ws handshake: {message or 'connected'}")
        elif status == "auth_success":
            self._authed = True
            self._auth_failed = False
            self._attempt = 0  # healthy session — reset the backoff ladder
            logger.info("[realtime-feed] polygon ws AUTHENTICATED — real-time stream is LIVE")
        elif status == "auth_failed" or "not authorized" in message.lower():
            # The expected state until the plan upgrade lands. Flag it; the
            # run loop exits the session and retries on the slow cadence.
            self._auth_failed = True
            self._authed = False
            logger.warning(f"[realtime-feed] polygon ws auth/entitlement rejected: {message or status}")
        elif status == "success":
            logger.info(f"[realtime-feed] polygon ws ack: {message}")
        else:
            logger.info(f"[realtime-feed] polygon ws status '{status}': {message}")


# ── Module singletons + consumer helpers ────────────────────────────────────
# One store per process. The feed task (started from the lifespan) writes into
# it; consumers read through the flag-gated helpers below, so with the flag
# off (or the feed dead/stale) every helper degrades to []/None and callers
# keep their existing REST behavior untouched.
_default_store = LatestBarStore()
_feed: Optional[RealtimeFeed] = None


def get_default_store() -> LatestBarStore:
    return _default_store


def get_feed() -> Optional[RealtimeFeed]:
    return _feed


def create_feed_from_env() -> Optional[RealtimeFeed]:
    """Build (and remember) the feed configured by REALTIME_FEED, or None when
    the flag is off/unknown. Called once from the app lifespan; the returned
    feed's .start is handed to task_supervisor.supervise()."""
    global _feed
    provider = realtime_provider()
    if provider in ("", "0", "off", "false", "none"):
        return None  # default: feed off, zero behavior change
    if provider == "polygon":
        key = os.environ.get("POLYGON_API_KEY", "")
        if not key:
            logger.warning("[realtime-feed] REALTIME_FEED=polygon but POLYGON_API_KEY is empty — feed disabled")
            return None
        _feed = PolygonRealtimeFeed(store=_default_store, api_key=key, symbols=_symbols_from_env())
        logger.info(f"[realtime-feed] polygon feed configured (symbols={_symbols_from_env()})")
        return _feed
    if provider == "fmp":
        key = (os.environ.get("FMP_API_KEY", "") or "").strip()
        if not key:
            logger.warning("[realtime-feed] REALTIME_FEED=fmp but FMP_API_KEY is empty — feed disabled")
            return None
        # Lazy import: the FMP module is only paid for when actually selected.
        from app.engines.data_feeds.fmp_feed import FMPRealtimeFeed
        _feed = FMPRealtimeFeed(store=_default_store, api_key=key, symbols=_symbols_from_env())
        logger.info(
            f"[realtime-feed] fmp feed configured (symbols={_symbols_from_env()}, "
            f"poll={_feed._poll_seconds:g}s, ws={'on' if _feed._ws_enabled else 'off'})"
        )
        return _feed
    logger.warning(f"[realtime-feed] unknown REALTIME_FEED provider '{provider}' — feed disabled")
    return None


def get_fresh_bars(symbol: str, n: Optional[int] = None, max_age_s: float = STALE_AFTER_S) -> list:
    """Consumer entry point: recent minute bars for `symbol` from the live
    store, or [] when the flag is off / the store is empty / the newest bar is
    older than `max_age_s`. Callers fall through to their existing REST path
    on [], which makes flag-off behavior byte-identical to today. Never raises."""
    try:
        if not realtime_enabled():
            return []
        age = _default_store.age_seconds(symbol)
        if age is None or age > max_age_s:
            return []
        return _default_store.get_recent_bars(symbol, n)
    except Exception as e:
        logger.warning(f"[realtime-feed] get_fresh_bars({symbol}) failed: {e}")
        return []


def get_fresh_price(symbol: str, max_age_s: float = STALE_AFTER_S) -> Optional[float]:
    """Latest live price for `symbol`, or None when off/empty/stale. Never raises."""
    try:
        if not realtime_enabled():
            return None
        age = _default_store.age_seconds(symbol)
        if age is None or age > max_age_s:
            return None
        return _default_store.get_last_price(symbol)
    except Exception as e:
        logger.warning(f"[realtime-feed] get_fresh_price({symbol}) failed: {e}")
        return None


async def get_ondemand_intraday_bars(
    symbol: str,
    n: Optional[int] = None,
    date_et: Optional[str] = None,
) -> list:
    """On-demand REAL-TIME 1-min bars for an ARBITRARY ticker — no
    subscription required. FMP-only: its /historical-chart/1min endpoint is
    real-time on the live-entitled plan, TTL-cached in fmp_feed
    (≤ 1 request/symbol/15s) with a hard timeout. This is what makes the
    scanner's 09:35 confirmation real-time for EVERY candidate, not just the
    symbols already streaming into the store.

    Returns [] (never raises) when the provider isn't 'fmp', the key is
    missing, or the fetch fails/times out — callers keep their delayed-REST
    fallback, the exact same discipline as get_fresh_bars(). `date_et`
    ('YYYY-MM-DD') filters to one ET session; bars come back in the REST-aggs
    dict shape, oldest→newest."""
    try:
        if realtime_provider() != "fmp":
            return []
        from app.engines.data_feeds.fmp_feed import fetch_intraday_bars
        return await fetch_intraday_bars(symbol, n=n, date_et=date_et)
    except Exception as e:
        logger.warning(f"[realtime-feed] get_ondemand_intraday_bars({symbol}) failed: {e}")
        return []


def request_symbols(symbols: Iterable[str]) -> None:
    """Best-effort dynamic subscribe (scanner candidates, tape symbols, ...).
    No-op when the flag is off or no feed is running. Never raises."""
    try:
        if not realtime_enabled():
            return
        feed = _feed
        if feed is not None:
            feed.subscribe(symbols)
    except Exception as e:
        logger.warning(f"[realtime-feed] request_symbols failed: {e}")

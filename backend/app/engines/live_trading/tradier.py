import json
"""Tradier broker adapter.

Tradier covers what Tradovate doesn't: US equities and listed options. The
two adapters split cleanly along asset-class lines — Tradovate routes
futures (ES, NQ, MES, MNQ, …); Tradier routes equities & options (SPY,
QQQ, AAPL, …, plus OCC option tickers like O:SPY260618C00500000).

Authentication: a single bearer access token (sandbox or live). No OAuth
dance required for our use — the user generates the token in Tradier's
dashboard and pastes it into the Connect Account modal.

Docs: https://documentation.tradier.com/
"""
import asyncio
from typing import Optional, Callable
from datetime import datetime, timezone
import aiohttp
from loguru import logger

from app.engines.live_trading.broker_base import (
    BrokerBase, OrderRequest, OrderResponse, AccountInfo,
    OrderStatus, OrderType, OrderSide,
)


TRADIER_LIVE_URL    = "https://api.tradier.com/v1"
TRADIER_SANDBOX_URL = "https://sandbox.tradier.com/v1"
TRADIER_WS_URL      = "wss://ws.tradier.com/v1/markets/events"


def _occ_to_tradier(ticker: str) -> str:
    """Tradier's order endpoint expects the OCC symbol *without* the leading
    `O:` prefix that Polygon uses. `O:SPY260618C00500000` → `SPY260618C00500000`."""
    return ticker[2:] if ticker.startswith("O:") else ticker


def _is_option_symbol(symbol: str) -> bool:
    """Detect whether a symbol is an option contract (OCC-style) vs an
    equity ticker. Options follow `ROOT YYMMDD [C|P] STRIKE*1000` with no
    spaces, length >= 15 after stripping the optional `O:` prefix."""
    body = symbol[2:] if symbol.startswith("O:") else symbol
    if len(body) < 15:
        return False
    # The OCC format ends in 8 digits + (C|P) at position -9
    side_char = body[-9].upper() if len(body) >= 9 else ""
    return side_char in ("C", "P") and body[-8:].isdigit()


class TradierBroker(BrokerBase):
    """Tradier REST + WebSocket adapter.

    Two environments:
      • sandbox.tradier.com — free, near-realtime simulated quotes, paper fills
      • api.tradier.com     — live trading; quotes via WebSocket on paid plan

    Quote streaming is a two-step dance: POST to /markets/events/session
    to get a session id, then connect to the WebSocket with that id.
    """

    def __init__(self, credentials: dict, is_demo: bool = True):
        super().__init__(credentials, is_demo)
        self.base_url      = TRADIER_SANDBOX_URL if is_demo else TRADIER_LIVE_URL
        self.access_token  = credentials.get("access_token", "").strip()
        self.account_id    = credentials.get("account_id", "").strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws_session_id: Optional[str] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._subscriptions: dict[str, list[Callable]] = {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept":        "application/json",
        }

    # ─────────────────────────────────────────────────────────────────────
    # Connection
    # ─────────────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Verify the bearer token + (optionally) resolve a default account
        ID by hitting Tradier's /user/profile endpoint."""
        if not self.access_token:
            logger.error("[Tradier] No access_token supplied")
            return False
        try:
            self._session = aiohttp.ClientSession(headers=self._headers())
            async with self._session.get(f"{self.base_url}/user/profile") as r:
                if r.status != 200:
                    body = await r.text()
                    logger.error(f"[Tradier] /user/profile -> {r.status}: {body[:300]}")
                    return False
                data = await r.json()

            # If the caller didn't pre-set an account_id, take the first one
            # returned by the profile.
            if not self.account_id:
                profile = (data or {}).get("profile") or {}
                accts = profile.get("account")
                if isinstance(accts, list) and accts:
                    self.account_id = str(accts[0].get("account_number"))
                elif isinstance(accts, dict):
                    self.account_id = str(accts.get("account_number"))

            self._connected = True
            logger.info(f"[Tradier] Connected {'(SANDBOX)' if self.is_demo else '(LIVE)'} | account_id={self.account_id}")
            return True
        except Exception as e:
            logger.error(f"[Tradier] connect error: {e}")
            return False

    async def disconnect(self):
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._connected = False

    # ─────────────────────────────────────────────────────────────────────
    # Orders
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _map_order_type(ot: OrderType) -> str:
        return {
            OrderType.MARKET:     "market",
            OrderType.LIMIT:      "limit",
            OrderType.STOP:       "stop",
            OrderType.STOP_LIMIT: "stop_limit",
        }.get(ot, "market")

    @staticmethod
    def _map_equity_side(side: OrderSide) -> str:
        return "buy" if side == OrderSide.BUY else "sell"

    @staticmethod
    def _map_option_side(side: OrderSide, is_closing: bool) -> str:
        """Tradier options use buy_to_open / sell_to_close style sides. We
        only support opening + closing long positions in the engine for
        now (no short option selling), so the mapping is:
            BUY  + opening → buy_to_open
            SELL + closing → sell_to_close"""
        if side == OrderSide.BUY:
            return "buy_to_close" if is_closing else "buy_to_open"
        return "sell_to_close" if is_closing else "sell_to_open"

    async def get_balance(self) -> dict:
        """Return current account equity + buying power + cash from Tradier.

        Shape: {
          "equity": float,                # total account value
          "buying_power": float,          # how much we can deploy (>= cash for margin accounts)
          "cash": float,
          "margin_call": bool,
          "account_type": str,            # "cash" | "margin"
          "raw": dict,                    # full Tradier response for debugging
        }
        """
        if not self._connected:
            raise RuntimeError("Not connected to Tradier")
        if not self.account_id:
            raise RuntimeError("No account_id resolved")
        url = f"{self.base_url}/accounts/{self.account_id}/balances"
        async with self._session.get(url) as r:
            data = await r.json()
        bal = data.get("balances") or {}
        # Tradier returns a "type" of "margin" | "cash" | "pdt" — coerce pdt to margin.
        raw_type = (bal.get("account_type") or bal.get("type") or "cash").lower()
        acct_type = "margin" if raw_type in ("margin", "pdt") else "cash"
        # Buying power lives in different sub-objects depending on account type.
        if acct_type == "margin":
            sub = bal.get("margin") or {}
            bp = float(sub.get("stock_buying_power") or sub.get("option_buying_power") or bal.get("total_equity") or 0)
        else:
            sub = bal.get("cash") or {}
            bp = float(sub.get("cash_available") or bal.get("total_cash") or bal.get("total_equity") or 0)
        return {
            "equity":       float(bal.get("total_equity") or 0),
            "buying_power": bp,
            "cash":         float(bal.get("total_cash") or 0),
            "margin_call":  bool(bal.get("margin_call") or False),
            "account_type": acct_type,
            "raw":          bal,
        }


    async def get_account_history(self, limit: int = 200, activity_type: str | None = "trade") -> list[dict]:
        """Pull account activity history from Tradier.

        Endpoint: GET /v1/accounts/{id}/history?limit=N[&type=trade]

        Tradier wraps responses inconsistently — `history.event` can be a list
        OR a single dict. We always normalise to a list of flat dicts so the
        reconcile layer never has to think about Tradier's wire format.

        Returns:
            [{"date":..., "symbol":..., "side":"buy"|"sell", "quantity":N,
              "price":..., "amount":..., "commission":..., "trade_type":...,
              "raw":{...full event...}}, ...]

        Defensive: empty/missing responses → [], NEVER raises.
        """
        if not self._connected or not self._session:
            logger.warning("[Tradier] get_account_history called before connect()")
            return []
        if not self.account_id:
            logger.warning("[Tradier] get_account_history: no account_id resolved")
            return []
        params: dict = {"limit": str(int(limit or 200))}
        if activity_type:
            # Tradier accepts type=trade|option|dividend|journal|... — passing
            # 'trade' filters to trade fills, which is what reconcile cares about.
            params["type"] = str(activity_type)
        url = f"{self.base_url}/accounts/{self.account_id}/history"
        try:
            async with self._session.get(url, params=params) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"[Tradier] history -> {r.status}: {body[:200]}")
                    return []
                data = await r.json()
        except Exception as e:
            logger.warning(f"[Tradier] get_account_history error: {e}")
            return []

        # Tradier sometimes returns a non-dict (`null`/string) when there is
        # no history matching the filter. Guard every step so we never call
        # .get on something that isn't a dict.
        if not isinstance(data, dict):
            logger.info(f"[Tradier] get_account_history: non-dict response ({type(data).__name__}); treating as empty")
            return []
        history = data.get("history")
        if not isinstance(history, dict):
            return []
        events = history.get("event")
        if not events:
            return []
        # Normalise single-dict → list (Tradier returns a bare dict when
        # there's exactly one event).
        if isinstance(events, dict):
            events = [events]
        if not isinstance(events, list):
            return []
        # Diagnostic: log the type-distribution so we can see what Tradier
        # actually exposes for this account (different sandboxes return
        # different sets — e.g., flatten_all closes may be under 'option'
        # or unclassified rather than 'trade').
        try:
            from collections import Counter
            type_counts = Counter((ev.get("type") if isinstance(ev, dict) else None) for ev in events)
            logger.info(f"[Tradier] get_account_history returned {len(events)} events; types={dict(type_counts)}")
        except Exception:
            pass

        out: list[dict] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = (ev.get("type") or "").lower()
            date  = ev.get("date") or ev.get("trade_date") or None
            amount = None
            try:
                if ev.get("amount") is not None:
                    amount = float(ev.get("amount"))
            except (TypeError, ValueError):
                amount = None

            # Trade / option fills live in a nested sub-object that mirrors the
            # event's `type` field. We surface symbol/qty/price/side from there.
            inner = ev.get(etype) if etype in ("trade", "option") else None
            if etype in ("trade", "option") and isinstance(inner, dict):
                symbol = inner.get("symbol") or ev.get("symbol")
                # Tradier's `quantity` is signed: positive = buy, negative = sell.
                try:
                    qty_raw = float(inner.get("quantity") or 0)
                except (TypeError, ValueError):
                    qty_raw = 0.0
                qty = int(abs(qty_raw)) if qty_raw else 0
                # Side: prefer the explicit `trade_type` ("buy"/"sell"); fall
                # back to the sign of quantity.
                tt = (inner.get("trade_type") or "").lower()
                if tt in ("buy", "sell"):
                    side = tt
                else:
                    side = "buy" if qty_raw >= 0 else "sell"
                try:
                    price = float(inner.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                try:
                    commission = float(inner.get("commission") or 0)
                except (TypeError, ValueError):
                    commission = 0.0
                trade_type = inner.get("trade_type") or etype
            else:
                # Non-trade events (journal/dividend/cash/etc.) — still surface
                # the basics so the caller can compute deposits/withdrawals from
                # the same list.
                symbol = ev.get("symbol")
                qty = 0
                side = ""
                price = 0.0
                commission = 0.0
                trade_type = etype

            out.append({
                "type":        etype,
                "date":        date,
                "symbol":      symbol,
                "side":        side,
                "quantity":    qty,
                "price":       price,
                "amount":      amount,
                "commission":  commission,
                "trade_type":  trade_type,
                "raw":         ev,
            })
        return out


    async def place_order(self, order: OrderRequest) -> OrderResponse:
        if not self._connected:
            raise RuntimeError("Not connected to Tradier")
        if not self.account_id:
            return OrderResponse(broker_order_id="", status=OrderStatus.REJECTED,
                                  message="No account_id resolved — set credentials.account_id")

        is_option = _is_option_symbol(order.instrument)
        is_closing = bool((order.client_order_id or "").endswith("|close"))

        payload = {
            "class":         "option" if is_option else "equity",
            "symbol":        _occ_to_tradier(order.instrument) if is_option else order.instrument.upper(),
            "side":          self._map_option_side(order.side, is_closing) if is_option else self._map_equity_side(order.side),
            "quantity":      str(order.quantity),
            "type":          self._map_order_type(order.order_type),
            "duration":      order.time_in_force if order.time_in_force in ("day", "gtc", "pre", "post") else "day",
        }
        # Options need the underlying symbol on the order
        if is_option:
            # OCC: first 1-6 chars are the underlying root
            root = _occ_to_tradier(order.instrument)
            # Walk back from the end to find the start of the YYMMDD
            for i in range(1, 7):
                if root[i:i+6].isdigit():
                    payload["symbol"] = root[:i]
                    payload["option_symbol"] = _occ_to_tradier(order.instrument)
                    break

        if order.price is not None:
            payload["price"] = str(order.price)
        if order.stop_price is not None:
            payload["stop"] = str(order.stop_price)

        url = f"{self.base_url}/accounts/{self.account_id}/orders"
        try:
            async with self._session.post(url, data=payload) as r:
                data = await r.json()
                if r.status == 200 and "order" in data:
                    o = data["order"]
                    return OrderResponse(
                        broker_order_id=str(o.get("id", "")),
                        status=OrderStatus.PENDING,
                        message=o.get("status", "submitted"),
                    )
                err_msg = (data.get("errors") or {}).get("error") or str(data)
                logger.error(f"[Tradier] order rejected: {err_msg}")
                return OrderResponse(broker_order_id="", status=OrderStatus.REJECTED, message=str(err_msg))
        except Exception as e:
            logger.error(f"[Tradier] place_order error: {e}")
            return OrderResponse(broker_order_id="", status=OrderStatus.REJECTED, message=str(e))

    async def cancel_order(self, broker_order_id: str) -> bool:
        url = f"{self.base_url}/accounts/{self.account_id}/orders/{broker_order_id}"
        try:
            async with self._session.delete(url) as r:
                return r.status == 200
        except Exception as e:
            logger.error(f"[Tradier] cancel_order error: {e}")
            return False

    async def get_order_status(self, broker_order_id: str) -> OrderResponse:
        url = f"{self.base_url}/accounts/{self.account_id}/orders/{broker_order_id}"
        try:
            async with self._session.get(url) as r:
                data = await r.json()
                o = data.get("order") or {}
                tradier_status = (o.get("status") or "").lower()
                status_map = {
                    "open":       OrderStatus.PENDING,
                    "pending":    OrderStatus.PENDING,
                    "accepted":   OrderStatus.PENDING,
                    "submitted":  OrderStatus.PENDING,
                    "filled":     OrderStatus.FILLED,
                    "canceled":   OrderStatus.CANCELLED,
                    "cancelled":  OrderStatus.CANCELLED,
                    "expired":    OrderStatus.CANCELLED,
                    "rejected":   OrderStatus.REJECTED,
                    "partially_filled": OrderStatus.PARTIAL,
                }
                return OrderResponse(
                    broker_order_id=broker_order_id,
                    status=status_map.get(tradier_status, OrderStatus.PENDING),
                    filled_price=float(o["avg_fill_price"]) if o.get("avg_fill_price") else None,
                    filled_quantity=int(o.get("exec_quantity", 0)),
                    message=tradier_status,
                )
        except Exception as e:
            logger.error(f"[Tradier] get_order_status error: {e}")
            return OrderResponse(broker_order_id=broker_order_id, status=OrderStatus.PENDING)

    # ─────────────────────────────────────────────────────────────────────
    # Account
    # ─────────────────────────────────────────────────────────────────────

    async def get_account_info(self) -> AccountInfo:
        url = f"{self.base_url}/accounts/{self.account_id}/balances"
        try:
            async with self._session.get(url) as r:
                data = await r.json()
                b = data.get("balances") or {}
                # Tradier returns different account-type fields:
                #   cash, margin, pdt — each with their own structure
                # We surface the most useful: total equity, available margin, open P&L
                equity = float(b.get("total_equity", 0.0) or 0.0)
                # Available "buying power" is shaped differently per account class
                buying_power = (b.get("margin") or b.get("cash") or {}).get("stock_buying_power")
                if buying_power is None:
                    buying_power = (b.get("margin") or {}).get("option_buying_power", 0.0)
                return AccountInfo(
                    account_id=self.account_id,
                    balance=equity,
                    available_margin=float(buying_power or 0.0),
                    open_pnl=float(b.get("open_pl", 0.0) or 0.0),
                    broker="tradier",
                )
        except Exception as e:
            logger.error(f"[Tradier] get_account_info error: {e}")
            return AccountInfo(self.account_id or "", 0.0, 0.0, 0.0, "tradier")

    async def get_positions(self) -> list[dict]:
        url = f"{self.base_url}/accounts/{self.account_id}/positions"
        try:
            async with self._session.get(url) as r:
                data = await r.json()
                p = (data.get("positions") or {}).get("position")
                if not p:
                    return []
                if isinstance(p, dict):
                    p = [p]
                # Normalise to the shape our engine expects
                out = []
                for row in p:
                    out.append({
                        "symbol":     row.get("symbol"),
                        "quantity":   int(float(row.get("quantity", 0))),
                        "cost_basis": float(row.get("cost_basis", 0.0)),
                        "date_acquired": row.get("date_acquired"),
                    })
                return out
        except Exception as e:
            logger.error(f"[Tradier] get_positions error: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────────
    # Options market data — Tradier's killer feature for our use case
    # ─────────────────────────────────────────────────────────────────────

    async def get_option_chain(self, underlying: str, expiration: str,
                                include_greeks: bool = True) -> list[dict]:
        """Pull the full options chain for one underlying×expiration. The
        `expiration` is YYYY-MM-DD. Tradier's `greeks=true` query parameter
        adds live delta/gamma/theta/vega/rho + IV to every row — this is what
        Polygon's free tier doesn't give us."""
        url = f"{self.base_url}/markets/options/chains"
        params = {
            "symbol":     underlying.upper(),
            "expiration": expiration,
            "greeks":     "true" if include_greeks else "false",
        }
        try:
            async with self._session.get(url, params=params) as r:
                data = await r.json()
                options = (data.get("options") or {}).get("option") or []
                if isinstance(options, dict):
                    options = [options]
                return options
        except Exception as e:
            logger.error(f"[Tradier] get_option_chain error: {e}")
            return []

    async def get_option_expirations(self, underlying: str) -> list[str]:
        """List all expirations for `underlying` (YYYY-MM-DD strings)."""
        url = f"{self.base_url}/markets/options/expirations"
        try:
            async with self._session.get(url, params={"symbol": underlying.upper()}) as r:
                data = await r.json()
                exps = (data.get("expirations") or {}).get("date") or []
                if isinstance(exps, str):
                    exps = [exps]
                return exps
        except Exception as e:
            logger.error(f"[Tradier] get_option_expirations error: {e}")
            return []

    async def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Snapshot quotes for a list of symbols (equity or option tickers).
        Returns {symbol: {bid, ask, last, ...}}."""
        if not symbols:
            return {}
        url = f"{self.base_url}/markets/quotes"
        params = {"symbols": ",".join(symbols), "greeks": "true"}
        try:
            async with self._session.get(url, params=params) as r:
                data = await r.json()
                rows = (data.get("quotes") or {}).get("quote") or []
                if isinstance(rows, dict):
                    rows = [rows]
                return {q.get("symbol"): q for q in rows}
        except Exception as e:
            logger.error(f"[Tradier] get_quotes error: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────
    # Streaming (WebSocket)
    # ─────────────────────────────────────────────────────────────────────
    #
    # Tradier exposes a market-data WebSocket at wss://ws.tradier.com/v1/markets/events.
    # The handshake is HTTP-first: we POST to /markets/events/session to get
    # a short-lived session id, then connect the WS and send a subscribe
    # frame. Server pushes JSON frames on every trade/quote/timesale event.

    async def _create_ws_session(self) -> Optional[str]:
        """Create a market-events streaming session id. Tradier sessions are
        single-use and expire ~5 minutes after creation, so we re-create on
        every WS reconnect."""
        if not self._session:
            self._session = aiohttp.ClientSession(headers=self._headers())
        url = f"{self.base_url}/markets/events/session"
        try:
            async with self._session.post(url) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.error(f"[Tradier] WS session create -> {r.status}: {body[:200]}")
                    return None
                data = await r.json()
                sid = (data.get("stream") or {}).get("sessionid")
                self._ws_session_id = sid
                return sid
        except Exception as e:
            logger.error(f"[Tradier] WS session create error: {e}")
            return None

    async def _ws_connect_and_listen(self):
        """Background coroutine: opens the WS, subscribes to currently-
        tracked symbols, and dispatches frames to registered callbacks.
        Auto-reconnects with exponential back-off on disconnect."""
        backoff = 1
        while self._connected:
            try:
                sid = await self._create_ws_session()
                if not sid:
                    await asyncio.sleep(min(60, backoff))
                    backoff = min(60, backoff * 2)
                    continue
                if not self._session:
                    return
                async with self._session.ws_connect(
                    "wss://ws.tradier.com/v1/markets/events"
                ) as ws:
                    self._ws = ws
                    backoff = 1  # reset on successful connect
                    # Subscribe to every symbol we have a callback for
                    symbols = list({sym for sym, _ in self._subscriptions.get("quotes", [])})
                    if not symbols:
                        # Nothing to stream yet; sleep and retry
                        await asyncio.sleep(5)
                        continue
                    sub_payload = {
                        "sessionid": sid,
                        "symbols":   symbols,
                        "filter":    ["trade", "quote"],
                        "linebreak": False,
                    }
                    await ws.send_str(json.dumps(sub_payload))
                    logger.info(f"[Tradier] WS subscribed to {len(symbols)} symbols")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                frame = json.loads(msg.data)
                            except Exception:
                                continue
                            await self._dispatch_frame(frame)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Tradier] WS error (reconnecting in {backoff}s): {e}")
                await asyncio.sleep(min(60, backoff))
                backoff = min(60, backoff * 2)

    async def _dispatch_frame(self, frame: dict):
        """Route a Tradier WS frame to subscribed callbacks. The frame shape
        varies by event type — for 'trade' we use `price`, for 'quote' we
        use the bid/ask mid."""
        sym = frame.get("symbol")
        if not sym:
            return
        # Normalise to a tick dict our callbacks expect
        ftype = frame.get("type")
        price = None
        if ftype == "trade":
            try:
                price = float(frame.get("price")) if frame.get("price") else None
            except Exception:
                price = None
        elif ftype == "quote":
            bid = frame.get("bid")
            ask = frame.get("ask")
            try:
                if bid and ask:
                    price = (float(bid) + float(ask)) / 2
            except Exception:
                price = None
        if price is None:
            return
        tick = {
            "symbol":    sym,
            "price":     price,
            "type":      ftype,
            "timestamp": datetime.now(timezone.utc),
            "raw":       frame,
        }
        for s, cb in self._subscriptions.get("quotes", []):
            if s == sym:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(tick))
                else:
                    try:
                        cb(tick)
                    except Exception as e:
                        logger.error(f"[Tradier] quote callback error: {e}")

    async def subscribe_quotes(self, instrument: str, callback: Callable):
        """Register a callback for live quotes on `instrument` (equity ticker
        or OCC option symbol — stripped of the optional `O:` prefix). Starts
        the WS listener task on the first subscription."""
        sym = instrument[2:] if instrument.startswith("O:") else instrument
        self._subscriptions.setdefault("quotes", []).append((sym, callback))
        # Lazily spin up the WS listener once
        if not hasattr(self, "_ws_task") or self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._ws_connect_and_listen())

    async def subscribe_bars(self, instrument: str, timeframe: str, callback: Callable):
        # Tradier doesn't push bars over WS; bars come from /markets/timesales
        # (intraday) or /markets/history (daily). Caller should poll these
        # on its own cadence. We register the callback so callers see a
        # uniform interface across brokers, but it'll never fire.
        self._subscriptions.setdefault("bars", []).append((instrument, timeframe, callback))

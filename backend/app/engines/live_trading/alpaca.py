"""Alpaca broker adapter — stocks + options + crypto.

API docs: https://docs.alpaca.markets/reference

Auth modes:
  • API key + secret (header auth) — simplest, what we use here
  • OAuth 2.0 — for multi-tenant apps that don't ask users for keys (later)

Sandbox vs Live:
  • Sandbox URL: https://paper-api.alpaca.markets
  • Live URL:    https://api.alpaca.markets
  Each gets its own API keys (signed up separately at alpaca.markets/paper or /live).
"""
import asyncio
from typing import Optional
from loguru import logger
import aiohttp

from app.engines.live_trading.broker_base import (
    BrokerBase, OrderRequest, OrderResponse, OrderType, OrderSide, OrderStatus,
)

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets/v2"
ALPACA_LIVE_URL  = "https://api.alpaca.markets/v2"


class AlpacaBroker(BrokerBase):
    """Alpaca stocks + options adapter."""

    def __init__(self, credentials: dict, is_demo: bool = True):
        super().__init__(credentials, is_demo)
        self.base_url = ALPACA_PAPER_URL if is_demo else ALPACA_LIVE_URL
        self.api_key    = (credentials.get("api_key") or credentials.get("access_token") or "").strip()
        self.api_secret = (credentials.get("api_secret") or credentials.get("secret") or "").strip()
        self.account_id = credentials.get("account_id", "").strip()
        self._session: Optional[aiohttp.ClientSession] = None

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID":     self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type":        "application/json",
            "Accept":              "application/json",
        }

    # ── Connection ────────────────────────────────────────────────────────
    async def connect(self) -> bool:
        if not self.api_key or not self.api_secret:
            logger.error("[Alpaca] missing api_key or api_secret")
            return False
        try:
            self._session = aiohttp.ClientSession(headers=self._headers())
            async with self._session.get(f"{self.base_url}/account") as r:
                if r.status != 200:
                    body = await r.text()
                    logger.error(f"[Alpaca] connect failed: {r.status} — {body[:200]}")
                    await self._session.close()
                    self._session = None
                    return False
                data = await r.json()
                self.account_id = self.account_id or data.get("account_number") or data.get("id", "")
            self._connected = True
            logger.info(f"[Alpaca] Connected {'(SANDBOX)' if self.is_demo else '(LIVE)'} | account={self.account_id}")
            return True
        except Exception as e:
            logger.error(f"[Alpaca] connect error: {e}")
            if self._session:
                await self._session.close()
                self._session = None
            return False

    async def disconnect(self):
        self._connected = False
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ── Order helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _map_order_type(ot: OrderType) -> str:
        return {
            OrderType.MARKET:     "market",
            OrderType.LIMIT:      "limit",
            OrderType.STOP:       "stop",
            OrderType.STOP_LIMIT: "stop_limit",
        }[ot]

    @staticmethod
    def _map_side(side: OrderSide) -> str:
        return "buy" if side == OrderSide.BUY else "sell"

    @staticmethod
    def _map_tif(tif: str) -> str:
        # Alpaca: day | gtc | opg | cls | ioc | fok
        return (tif or "day").lower() if tif.lower() in ("day", "gtc", "opg", "cls", "ioc", "fok") else "day"

    # ── Place order ───────────────────────────────────────────────────────
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        if not self._connected:
            raise RuntimeError("Not connected to Alpaca")
        payload = {
            "symbol":        order.instrument.upper(),
            "qty":           str(order.quantity),
            "side":          self._map_side(order.side),
            "type":          self._map_order_type(order.order_type),
            "time_in_force": self._map_tif(order.time_in_force or "day"),
        }
        if order.price is not None:
            payload["limit_price"] = str(order.price)
        if order.stop_price is not None:
            payload["stop_price"] = str(order.stop_price)
        if order.client_order_id:
            payload["client_order_id"] = order.client_order_id

        try:
            async with self._session.post(f"{self.base_url}/orders", json=payload) as r:
                data = await r.json()
                if r.status in (200, 201):
                    return OrderResponse(
                        broker_order_id=str(data.get("id", "")),
                        status=OrderStatus.PENDING,
                        message=data.get("status", "accepted"),
                    )
                err = data.get("message") or data.get("detail") or str(data)
                logger.error(f"[Alpaca] order rejected: {err}")
                return OrderResponse(broker_order_id="", status=OrderStatus.REJECTED, message=str(err))
        except Exception as e:
            logger.error(f"[Alpaca] place_order error: {e}")
            return OrderResponse(broker_order_id="", status=OrderStatus.REJECTED, message=str(e))

    # ── Cancel ────────────────────────────────────────────────────────────
    async def cancel_order(self, broker_order_id: str) -> bool:
        if not self._connected:
            return False
        try:
            async with self._session.delete(f"{self.base_url}/orders/{broker_order_id}") as r:
                return r.status in (200, 204)
        except Exception as e:
            logger.error(f"[Alpaca] cancel error: {e}")
            return False

    # ── Account balance + buying power ────────────────────────────────────
    async def get_balance(self) -> dict:
        """Return equity, buying_power, cash, account_type per BrokerBase shape."""
        if not self._connected:
            raise RuntimeError("Not connected to Alpaca")
        async with self._session.get(f"{self.base_url}/account") as r:
            data = await r.json()
        # Alpaca returns shorts as negative; equity = portfolio_value
        equity = float(data.get("portfolio_value") or data.get("equity") or 0)
        cash   = float(data.get("cash") or 0)
        # Alpaca buying power is 2× equity for margin, = cash for cash accounts
        buying_power = float(data.get("buying_power") or data.get("non_marginable_buying_power") or cash)
        # Alpaca account type: 'cash' or 'margin' (via 'multiplier' field: 1 = cash, 2/4 = margin)
        multiplier = int(data.get("multiplier") or 1)
        acct_type = "margin" if multiplier >= 2 else "cash"
        return {
            "equity":       equity,
            "buying_power": buying_power,
            "cash":         cash,
            "margin_call":  bool(data.get("trading_blocked") or data.get("account_blocked")),
            "account_type": acct_type,
            "raw":          data,
        }

    async def fetch_bars(self, instrument: str, timeframe: str = "1m", count: int = 60):
        """Pull recent bars for an underlying. Used by the live runner.
        Returns list of {timestamp, open, high, low, close, volume}."""
        if not self._connected:
            return []
        # Alpaca market-data endpoint is on a different base
        md_base = "https://data.alpaca.markets/v2/stocks"
        tf_map = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day"}
        tf = tf_map.get(timeframe, "1Min")
        url = f"{md_base}/{instrument.upper()}/bars?timeframe={tf}&limit={count}"
        try:
            async with self._session.get(url) as r:
                data = await r.json()
            bars = data.get("bars") or []
            return [{
                "timestamp": b.get("t"),
                "open":  float(b.get("o", 0)),
                "high":  float(b.get("h", 0)),
                "low":   float(b.get("l", 0)),
                "close": float(b.get("c", 0)),
                "volume": int(b.get("v", 0)),
            } for b in bars]
        except Exception as e:
            logger.error(f"[Alpaca] fetch_bars error: {e}")
            return []

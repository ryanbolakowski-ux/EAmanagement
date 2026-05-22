"""
Tradovate Broker Integration.
Uses the Tradovate REST + WebSocket API.
Docs: https://api.tradovate.com
"""
import asyncio
import aiohttp
import json
from typing import Optional, Callable
from loguru import logger
from datetime import datetime

from app.engines.live_trading.broker_base import (
    BrokerBase, OrderRequest, OrderResponse, AccountInfo,
    OrderStatus, OrderType, OrderSide,
)
from app.config import settings


TRADOVATE_LIVE_URL  = "https://live.tradovate.com/v1"
TRADOVATE_DEMO_URL  = "https://demo.tradovate.com/v1"
TRADOVATE_WS_LIVE   = "wss://live.tradovate.com/v1/websocket"
TRADOVATE_WS_DEMO   = "wss://demo.tradovate.com/v1/websocket"

# CME front-month contract codes follow CCYY: H=Mar, M=Jun, U=Sep, Z=Dec.
# Tradovate accepts the bare root (`ESM6`) — month code + 1-digit year.
# `current_front_month("ES")` returns the active contract for today.
def current_front_month(root: str, today=None) -> str:
    from datetime import date as _date
    today = today or _date.today()
    # Quarterly cycle — Mar(3), Jun(6), Sep(9), Dec(12). Active contract
    # is the next quarterly month strictly after the current.
    months = [(3, "H"), (6, "M"), (9, "U"), (12, "Z")]
    yr = today.year
    code = None
    for m, c in months:
        # Roll one week before expiry (8th of the expiry month). Front-month
        # is "this quarter" until that date, then the next quarter.
        if today.month < m or (today.month == m and today.day < 8):
            code = c
            yr_code = yr % 10
            break
    if code is None:
        # Past December — front month is next year's March
        code = "H"
        yr_code = (yr + 1) % 10
    return f"{root}{code}{yr_code}"


def instrument_to_tradovate_symbol(instrument: str) -> str:
    """Translate the platform's instrument code (e.g. `ES`, `NQ`, `MES`,
    `MNQ`) into the correct Tradovate front-month contract symbol."""
    roots = {"ES": "ES", "NQ": "NQ", "RTY": "RTY", "YM": "YM",
             "MES": "MES", "MNQ": "MNQ", "M2K": "M2K", "MYM": "MYM"}
    root = roots.get(instrument.upper(), instrument.upper())
    return current_front_month(root)


# Legacy alias — uses the dynamic resolver
class _InstrumentMap(dict):
    def get(self, key, default=None):
        try:
            return instrument_to_tradovate_symbol(key)
        except Exception:
            return default or key
INSTRUMENT_MAP = _InstrumentMap()


class TradovateBroker(BrokerBase):
    """
    Tradovate broker integration.
    Supports both demo and live environments.
    """

    def __init__(self, credentials: dict, is_demo: bool = True):
        super().__init__(credentials, is_demo)
        self.base_url = TRADOVATE_DEMO_URL if is_demo else TRADOVATE_LIVE_URL
        self.ws_url   = TRADOVATE_WS_DEMO  if is_demo else TRADOVATE_WS_LIVE
        self._access_token: Optional[str] = None
        self._account_id: Optional[int]   = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._subscriptions: dict[str, list[Callable]] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Connection & Authentication
    # ─────────────────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            self._session = aiohttp.ClientSession()
            payload = {
                "name":       self.credentials["username"],
                "password":   self.credentials["password"],
                "appId":      self.credentials.get("app_id", ""),
                "appVersion": self.credentials.get("app_version", "1.0"),
                "cid":        self.credentials.get("cid", ""),
                "sec":        self.credentials.get("sec", ""),
            }
            async with self._session.post(f"{self.base_url}/auth/accesstokenrequest", json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"[Tradovate] Auth failed: {resp.status}")
                    return False
                data = await resp.json()
                self._access_token = data.get("accessToken")
                self._account_id   = data.get("userId")
                if not self._access_token:
                    logger.error(f"[Tradovate] No access token in response: {data}")
                    return False

            self._connected = True
            logger.info(f"[Tradovate] Connected {'(DEMO)' if self.is_demo else '(LIVE)'} | Account: {self._account_id}")
            return True

        except Exception as e:
            logger.error(f"[Tradovate] Connection error: {e}")
            return False

    async def disconnect(self):
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        self._connected = False
        logger.info("[Tradovate] Disconnected")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"}

    # ─────────────────────────────────────────────────────────────────────────
    # Orders
    # ─────────────────────────────────────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        if not self._connected:
            raise RuntimeError("Not connected to Tradovate")

        contract_symbol = INSTRUMENT_MAP.get(order.instrument, order.instrument)

        # Build Tradovate order payload
        payload = {
            "accountSpec":    str(self._account_id),
            "accountId":      self._account_id,
            "action":         "Buy" if order.side == OrderSide.BUY else "Sell",
            "symbol":         contract_symbol,
            "orderQty":       order.quantity,
            "orderType":      self._map_order_type(order.order_type),
            "timeInForce":    order.time_in_force,
            "isAutomated":    True,
        }
        if order.price:
            payload["price"] = order.price
        if order.stop_price:
            payload["stopPrice"] = order.stop_price

        try:
            async with self._session.post(
                f"{self.base_url}/order/placeorder",
                json=payload,
                headers=self._headers(),
            ) as resp:
                data = await resp.json()
                if resp.status == 200 and "orderId" in data:
                    return OrderResponse(
                        broker_order_id=str(data["orderId"]),
                        status=OrderStatus.PENDING,
                        message="Order placed",
                    )
                else:
                    logger.error(f"[Tradovate] Order placement failed: {data}")
                    return OrderResponse(
                        broker_order_id="",
                        status=OrderStatus.REJECTED,
                        message=str(data),
                    )
        except Exception as e:
            logger.error(f"[Tradovate] place_order error: {e}")
            return OrderResponse(broker_order_id="", status=OrderStatus.REJECTED, message=str(e))

    async def cancel_order(self, broker_order_id: str) -> bool:
        try:
            async with self._session.post(
                f"{self.base_url}/order/cancelorder",
                json={"orderId": int(broker_order_id)},
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"[Tradovate] cancel_order error: {e}")
            return False

    async def get_order_status(self, broker_order_id: str) -> OrderResponse:
        try:
            async with self._session.get(
                f"{self.base_url}/order/item?id={broker_order_id}",
                headers=self._headers(),
            ) as resp:
                data = await resp.json()
                status_map = {
                    "Working":   OrderStatus.PENDING,
                    "Completed": OrderStatus.FILLED,
                    "Cancelled": OrderStatus.CANCELLED,
                    "Rejected":  OrderStatus.REJECTED,
                }
                status_str = data.get("ordStatus", "Working")
                return OrderResponse(
                    broker_order_id=broker_order_id,
                    status=status_map.get(status_str, OrderStatus.PENDING),
                    filled_price=data.get("avgPrice"),
                    filled_quantity=data.get("cumQty", 0),
                )
        except Exception as e:
            logger.error(f"[Tradovate] get_order_status error: {e}")
            return OrderResponse(broker_order_id=broker_order_id, status=OrderStatus.PENDING)

    # ─────────────────────────────────────────────────────────────────────────
    # Account
    # ─────────────────────────────────────────────────────────────────────────

    async def get_account_info(self) -> AccountInfo:
        try:
            async with self._session.post(
                f"{self.base_url}/cashbalance/getcashbalancesnapshot",
                json={"accountId": self._account_id},
                headers=self._headers(),
            ) as resp:
                data = await resp.json()
                return AccountInfo(
                    account_id=str(self._account_id),
                    balance=data.get("totalCashValue", 0.0),
                    available_margin=data.get("availableFunds", 0.0),
                    open_pnl=data.get("openPnL", 0.0),
                    broker="tradovate",
                )
        except Exception as e:
            logger.error(f"[Tradovate] get_account_info error: {e}")
            return AccountInfo(str(self._account_id), 0.0, 0.0, 0.0, "tradovate")

    async def get_positions(self) -> list[dict]:
        try:
            async with self._session.get(
                f"{self.base_url}/position/list",
                headers=self._headers(),
            ) as resp:
                return await resp.json()
        except Exception as e:
            logger.error(f"[Tradovate] get_positions error: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # Market data subscriptions (WebSocket)
    # ─────────────────────────────────────────────────────────────────────────

    async def subscribe_quotes(self, instrument: str, callback: Callable):
        symbol = INSTRUMENT_MAP.get(instrument, instrument)
        if "quotes" not in self._subscriptions:
            self._subscriptions["quotes"] = []
        self._subscriptions["quotes"].append((symbol, callback))
        await self._ensure_ws_connected()
        await self._ws.send_str(f'quote/subscribe\n1\n\n{{"symbol":"{symbol}"}}')
        logger.info(f"[Tradovate] Subscribed to quotes: {symbol}")

    async def subscribe_bars(self, instrument: str, timeframe: str, callback: Callable):
        symbol   = INSTRUMENT_MAP.get(instrument, instrument)
        interval = self._map_timeframe(timeframe)
        key = f"bars_{symbol}_{timeframe}"
        self._subscriptions[key] = callback
        await self._ensure_ws_connected()
        payload = json.dumps({"symbol": symbol, "chartDescription": {"underlyingType": "MinuteBar", "elementSize": interval, "elementSizeUnit": "UnderlyingUnits"}})
        await self._ws.send_str(f'md/subscribeHistogramData\n2\n\n{payload}')
        logger.info(f"[Tradovate] Subscribed to {timeframe} bars: {symbol}")

    async def _ensure_ws_connected(self):
        if self._ws is None or self._ws.closed:
            self._ws = await self._session.ws_connect(
                self.ws_url,
                headers=self._headers(),
            )
            asyncio.create_task(self._ws_listener())

    async def _ws_listener(self):
        """Listen for WebSocket messages and dispatch to callbacks."""
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._dispatch_ws_message(data)
                except Exception as e:
                    logger.warning(f"[Tradovate] WS parse error: {e}")

    async def _dispatch_ws_message(self, data: dict):
        event_type = data.get("e", "")
        if event_type == "quote":
            for symbol, cb in self._subscriptions.get("quotes", []):
                if data.get("d", {}).get("quotes", [{}])[0].get("contractId"):
                    tick = {
                        "instrument": symbol,
                        "price": data["d"]["quotes"][0].get("price", 0.0),
                        "timestamp": datetime.utcnow(),
                        "volume": data["d"]["quotes"][0].get("bidSize", 0),
                    }
                    await cb(tick) if asyncio.iscoroutinefunction(cb) else cb(tick)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _map_order_type(self, ot: OrderType) -> str:
        return {"market": "Market", "limit": "Limit", "stop": "Stop", "stop_limit": "StopLimit"}.get(ot.value, "Market")

    def _map_timeframe(self, tf: str) -> int:
        return {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240}.get(tf, 1)

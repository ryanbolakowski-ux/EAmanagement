"""
Abstract broker interface — all broker integrations must implement this.
Provides a consistent API for live order placement regardless of the broker.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from enum import Enum


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT  = "limit"
    STOP   = "stop"
    STOP_LIMIT = "stop_limit"


class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING   = "pending"
    FILLED    = "filled"
    CANCELLED = "cancelled"
    REJECTED  = "rejected"
    PARTIAL   = "partial"


@dataclass
class OrderRequest:
    instrument: str
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "GTC"
    client_order_id: Optional[str] = None


@dataclass
class OrderResponse:
    broker_order_id: str
    status: OrderStatus
    filled_price: Optional[float] = None
    filled_quantity: int = 0
    message: str = ""


@dataclass
class AccountInfo:
    account_id: str
    balance: float
    available_margin: float
    open_pnl: float
    broker: str


class BrokerBase(ABC):
    """Abstract base class for all broker integrations."""

    def __init__(self, credentials: dict, is_demo: bool = True):
        self.credentials = credentials
        self.is_demo     = is_demo
        self._connected  = False

    # ─────────────────────────────────────────────────────────────────────────
    # Connection
    # ─────────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> bool:
        """Authenticate and establish connection. Returns True on success."""
        ...

    @abstractmethod
    async def disconnect(self):
        ...

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ─────────────────────────────────────────────────────────────────────────
    # Orders
    # ─────────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place a market or limit order."""
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> OrderResponse:
        ...

    # ─────────────────────────────────────────────────────────────────────────
    # Account
    # ─────────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_account_info(self) -> AccountInfo:
        ...

    @abstractmethod
    async def get_positions(self) -> list[dict]:
        ...

    # ─────────────────────────────────────────────────────────────────────────
    # Market data
    # ─────────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def subscribe_quotes(self, instrument: str, callback):
        """Subscribe to real-time quotes. callback(tick_dict) called on each quote."""
        ...

    @abstractmethod
    async def subscribe_bars(self, instrument: str, timeframe: str, callback):
        """Subscribe to real-time bar closes. callback(bar_dict) called on each close."""
        ...

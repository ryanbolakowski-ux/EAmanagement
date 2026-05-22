"""Build the right broker adapter from a stored BrokerAccount row.

Why this exists: the LiveTrader engine takes a `BrokerBase`, which is
broker-agnostic. But the start-session flow only has a `BrokerAccount`
row with a `broker` string ('tradovate' / 'tradier') and encrypted
credentials. This factory bridges the two — decrypt, instantiate the
right subclass, return it ready-to-connect.
"""
from typing import Optional

from app.engines.live_trading.broker_base import BrokerBase
from app.models.user import BrokerAccount
from app.core.security import decrypt_credentials


def build_broker_from_account(account: BrokerAccount) -> Optional[BrokerBase]:
    """Returns a connected-style (not-yet-connected) BrokerBase subclass
    instance for this account. Caller must invoke `await broker.connect()`."""
    creds = decrypt_credentials(account.encrypted_credentials)
    name = (account.broker or "").lower()
    if name == "tradovate":
        from app.engines.live_trading.tradovate import TradovateBroker
        return TradovateBroker(creds, is_demo=bool(account.is_demo or account.sandbox_mode))
    if name == "alpaca":
        from app.engines.live_trading.alpaca import AlpacaBroker
        return AlpacaBroker(creds, is_demo=bool(account.is_demo or account.sandbox_mode))
    if name == "tradier":
        from app.engines.live_trading.tradier import TradierBroker
        return TradierBroker(creds, is_demo=bool(account.is_demo or account.sandbox_mode))
    return None

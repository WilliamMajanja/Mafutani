"""Broker layer: a zero-dependency interface to trading APIs.

Quick start::

    from brokers import get_broker, BrokerSession

    broker = get_broker("paper", user_id=1).connect()
    broker.submit_order({"symbol": "AAPL", "side": "buy", "qty": 10})

    # or drive it conversationally
    session = BrokerSession(broker)
    print(session.handle("status"))
    print(session.handle("buy AAPL 10"))

Point it at a real venue by describing the venue's shape (or writing a
`VenueProfile`) -- no application code changes::

    broker = get_broker("bearer_style",
                        credentials=Credentials("key", "secret"))
    broker.connect()
    broker.positions()

Everything here is stdlib-only. No ccxt, no requests, no vendor SDKs.
"""

from .base import (Broker, BrokerError, BrokerAuthError, BrokerRateLimitError,
                   BrokerUnavailableError, BrokerRejectedError,
                   BrokerValidationError, BrokerNotSupported, Credentials,
                   Order, OrderRequest, Fill, Position, AccountInfo, Quote,
                   BUY, SELL, MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING,
                   NEW, OPEN, PARTIAL, FILLED, CANCELLED, REJECTED, EXPIRED,
                   TERMINAL_STATUSES, SIDES, ORDER_TYPES, STATUSES,
                   assert_live_allowed, live_trading_enabled)
from .registry import (get_broker, available, describe, register,
                       resolve_credentials, env_prefix_for, credential_status)
from .profiles import (VenueProfile, get_profile, list_profiles, PROFILES,
                       BINANCE_STYLE, BEARER_STYLE, EXCHANGE_STYLE)
from .paper import PaperBroker
from .rest import RestBroker
from .interactive import BrokerSession
from . import transport, profiles, registry

__all__ = [
    "Broker", "BrokerError", "BrokerAuthError", "BrokerRateLimitError",
    "BrokerUnavailableError", "BrokerRejectedError", "BrokerValidationError",
    "BrokerNotSupported", "Credentials", "Order", "OrderRequest", "Fill",
    "Position", "AccountInfo", "Quote", "BUY", "SELL", "MARKET", "LIMIT",
    "STOP", "STOP_LIMIT", "TRAILING", "NEW", "OPEN", "PARTIAL", "FILLED",
    "CANCELLED", "REJECTED", "EXPIRED", "TERMINAL_STATUSES", "SIDES",
    "ORDER_TYPES", "STATUSES", "assert_live_allowed", "live_trading_enabled",
    "get_broker", "available", "describe", "register",
    "resolve_credentials", "env_prefix_for", "credential_status",
    "VenueProfile", "get_profile", "list_profiles",
    "PROFILES", "BINANCE_STYLE", "BEARER_STYLE", "EXCHANGE_STYLE",
    "PaperBroker", "RestBroker", "BrokerSession",
    "transport", "profiles", "registry",
]

__version__ = "1.0.0"

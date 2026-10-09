"""Broker abstraction: the contract every venue adapter must satisfy.

This is the seam between the application and any trading API. The app
(order routes, the terminal, the UI) only ever talks to a `Broker`, so
adding a venue means writing one adapter -- not touching app code.

Design rules
------------
1. **Zero dependency.** stdlib only. No ccxt, no requests, no SDKs.
2. **Paper by default.** `Broker.is_live` is False unless an adapter
   explicitly declares otherwise, and :func:`assert_live_allowed`
   re-checks the environment at call time. Live is never implicit.
3. **Normalized vocabulary.** Venues disagree about everything (side,
   order type, status, field names). Adapters translate *into* these
   types; nothing downstream of a broker knows a venue's dialect.
4. **Errors are typed.** Callers branch on the exception class, never on
   a message string.
5. **Secrets never surface.** Credentials live in a `Credentials` object
   that is not repr'd, not logged, and not returned by any API call.
"""

import os
import time
import uuid
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- vocabulary
BUY = "buy"
SELL = "sell"
SIDES = (BUY, SELL)

MARKET = "market"
LIMIT = "limit"
STOP = "stop"
STOP_LIMIT = "stop_limit"
TRAILING = "trailing"
ORDER_TYPES = (MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING)

# Order status, normalized. Venues map their own dialects onto these.
NEW = "new"
OPEN = "open"
PARTIAL = "partial"
FILLED = "filled"
CANCELLED = "cancelled"
REJECTED = "rejected"
EXPIRED = "expired"
STATUSES = (NEW, OPEN, PARTIAL, FILLED, CANCELLED, REJECTED, EXPIRED)

TERMINAL_STATUSES = frozenset([FILLED, CANCELLED, REJECTED, EXPIRED])


# --------------------------------------------------------------------- errors
class BrokerError(Exception):
    """Base for every broker failure.

    `venue` and `status` are preserved so callers can log or branch on
    them without parsing the message.
    """

    def __init__(self, message, venue="", status=None, retryable=False):
        super().__init__(message)
        self.message = str(message)
        self.venue = str(venue or "")
        self.status = status
        self.retryable = bool(retryable)


class BrokerValidationError(BrokerError):
    """The request was malformed. Never retryable -- fix the input."""


class BrokerAuthError(BrokerError):
    """Credentials missing, wrong, or expired."""


class BrokerRateLimitError(BrokerError):
    """Venue throttled us. Retryable, with `retry_after` seconds."""

    def __init__(self, message, venue="", status=None, retry_after=0):
        super().__init__(message, venue, status, retryable=True)
        self.retry_after = float(retry_after or 0)


class BrokerUnavailableError(BrokerError):
    """Venue unreachable or returned 5xx. Retryable."""


class BrokerRejectedError(BrokerError):
    """Venue understood the order and refused it (risk, margin, halt)."""


class BrokerNotSupported(BrokerError):
    """Adapter does not implement this capability."""


# ----------------------------------------------------------------- credentials
class Credentials:
    """API key material.

    Deliberately opaque: no `__repr__` of the values, no `__eq__` that
    could leak via assertion output, and a `fingerprint()` that is safe
    to log so you can tell *which* key was used without printing it.
    """

    __slots__ = ("_api_key", "_secret", "_passphrase", "_extra", "_fingerprint")

    def __init__(self, api_key="", secret="", passphrase="", extra=None):
        self._api_key = str(api_key or "")
        self._secret = str(secret or "")
        self._passphrase = str(passphrase or "")
        self._extra = dict(extra or {})
        # Derived, non-reversible, truncated: identifies the key without
        # disclosing it.
        import hashlib
        blob = "\x00".join([self._api_key, self._secret, self._passphrase])
        self._fingerprint = hashlib.sha256(blob.encode()).hexdigest()[:12]

    @classmethod
    def from_env(cls, prefix="BROKER_"):
        """Build credentials from environment variables.

        `BROKER_API_KEY`, `BROKER_SECRET`, `BROKER_PASSPHRASE` plus any
        `BROKER_EXTRA_*` pairs. Returns None when nothing is set, so
        callers can distinguish "unconfigured" from "empty".
        """
        api_key = os.environ.get(prefix + "API_KEY", "")
        secret = os.environ.get(prefix + "SECRET", "")
        passphrase = os.environ.get(prefix + "PASSPHRASE", "")
        extra = {}
        for k, v in os.environ.items():
            if k.startswith(prefix + "EXTRA_") and v:
                extra[k[len(prefix) + len("EXTRA_"):].lower()] = v
        if not any([api_key, secret, passphrase, extra]):
            return None
        return cls(api_key, secret, passphrase, extra)

    def fingerprint(self):
        return self._fingerprint

    def get(self, name, default=""):
        """Read a named secret (e.g. from the `extra` map)."""
        if name == "api_key":
            return self._api_key
        if name == "secret":
            return self._secret
        if name == "passphrase":
            return self._passphrase
        return self._extra.get(name, default)

    def has(self):
        return bool(self._api_key or self._secret or self._passphrase
                    or self._extra)

    def __bool__(self):
        return self.has()

    def __repr__(self):
        return "Credentials(fingerprint={}, set={})".format(
            self._fingerprint, sorted(
                [n for n in ("api_key", "secret", "passphrase")
                 if getattr(self, "_" + n)]))

    __str__ = __repr__


# ------------------------------------------------------------------ data types
@dataclass
class OrderRequest:
    """A venue-neutral order intent.

    `client_order_id` is generated if not supplied and is the idempotency
    key: submitting the same id twice must not create two orders. Every
    adapter forwards it unchanged.
    """

    symbol: str
    side: str = BUY
    qty: float = 0.0
    type: str = MARKET
    price: float = None
    stop_price: float = None
    trail_pct: float = None
    time_in_force: str = "day"
    client_order_id: str = field(default_factory=lambda: "coid-" + uuid.uuid4().hex[:16])
    reduce_only: bool = False
    metadata: dict = field(default_factory=dict)

    def validate(self):
        """Raise BrokerValidationError if this order cannot be sent."""
        sym = str(self.symbol or "").strip().upper()
        if not sym:
            raise BrokerValidationError("symbol is required")
        self.symbol = sym
        side = str(self.side or "").strip().lower()
        if side not in SIDES:
            raise BrokerValidationError(
                "side must be one of {}".format("/".join(SIDES)))
        self.side = side
        otype = str(self.type or "").strip().lower()
        if otype not in ORDER_TYPES:
            raise BrokerValidationError(
                "type must be one of {}".format("/".join(ORDER_TYPES)))
        self.type = otype
        try:
            self.qty = float(self.qty)
        except (TypeError, ValueError):
            raise BrokerValidationError("qty must be a number")
        if not (self.qty > 0):
            raise BrokerValidationError("qty must be > 0")
        if otype in (LIMIT, STOP_LIMIT) and not self.price:
            raise BrokerValidationError("{} order requires price".format(otype))
        if otype in (STOP, STOP_LIMIT) and not self.stop_price:
            raise BrokerValidationError("{} order requires stop_price".format(otype))
        if otype == TRAILING and not self.trail_pct:
            raise BrokerValidationError("trailing order requires trail_pct")
        if self.price is not None:
            self.price = float(self.price)
        if self.stop_price is not None:
            self.stop_price = float(self.stop_price)
        return self

    def to_dict(self):
        return asdict(self)


@dataclass
class Order:
    """A venue's view of an order, normalized."""

    symbol: str
    side: str
    qty: float
    type: str = MARKET
    status: str = NEW
    price: float = None
    filled_qty: float = 0.0
    avg_price: float = None
    order_id: str = ""
    client_order_id: str = ""
    venue: str = ""
    ts: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def is_terminal(self):
        return self.status in TERMINAL_STATUSES

    @property
    def remaining_qty(self):
        return max(0.0, float(self.qty) - float(self.filled_qty or 0.0))

    def to_dict(self):
        d = asdict(self)
        d["is_terminal"] = self.is_terminal
        d["remaining_qty"] = self.remaining_qty
        return d


@dataclass
class Fill:
    order_id: str = ""
    client_order_id: str = ""
    symbol: str = ""
    side: str = ""
    qty: float = 0.0
    price: float = 0.0
    fee: float = 0.0
    ts: int = 0
    venue: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class Position:
    symbol: str
    qty: float = 0.0        # signed: negative = short
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = None
    venue: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_flat(self):
        return abs(float(self.qty)) < 1e-12

    @property
    def is_long(self):
        return float(self.qty) > 0

    @property
    def is_short(self):
        return float(self.qty) < 0

    def to_dict(self):
        d = asdict(self)
        d.update({"is_flat": self.is_flat, "is_long": self.is_long,
                  "is_short": self.is_short})
        return d


@dataclass
class AccountInfo:
    """Account summary. Balances are in the account's base currency."""

    account_id: str = ""
    venue: str = ""
    currency: str = "USD"
    cash: float = 0.0
    equity: float = 0.0
    buying_power: float = 0.0
    margin_used: float = 0.0
    currency_pairs: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class Quote:
    symbol: str
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    ts: int = 0
    venue: str = ""

    @property
    def mid(self):
        if self.bid and self.ask:
            return (float(self.bid) + float(self.ask)) / 2.0
        return float(self.last or 0.0)

    @property
    def spread(self):
        if self.bid and self.ask:
            return float(self.ask) - float(self.bid)
        return 0.0

    def to_dict(self):
        d = asdict(self)
        d.update({"mid": self.mid, "spread": self.spread})
        return d


# -------------------------------------------------------------- live guard
def live_trading_enabled():
    """Env kill-switch. Defaults OFF; must be an explicit '1'."""
    return os.environ.get("LIVE_TRADING_ENABLED", "0") == "1"


def assert_live_allowed(venue="", confirm=False, tier_live_enabled=None):
    """Gate every live order. Raises BrokerError unless all three hold.

    Three independent conditions, all required:
      1. `LIVE_TRADING_ENABLED=1` in the environment,
      2. the caller's tier has `live_enabled`,
      3. an explicit confirmation flag on the request.

    `confirm` is deliberately not defaulted to True anywhere: a caller
    that forgets it fails closed.
    """
    if not live_trading_enabled():
        raise BrokerError(
            "live trading disabled (set LIVE_TRADING_ENABLED=1)", venue)
    if tier_live_enabled is False:
        raise BrokerError("tier is not permitted to trade live", venue)
    if not confirm:
        raise BrokerError("live order requires explicit confirmation", venue)
    return True


# ------------------------------------------------------------------ broker ABC
class Broker:
    """Interface every adapter implements.

    Adapters should raise `BrokerNotSupported` (not `AttributeError`) for
    capabilities they genuinely cannot provide, so callers can degrade
    gracefully.
    """

    #: short registry name, e.g. "paper"
    name = "base"
    #: human label
    venue = "base"
    #: True only for adapters that send real orders
    is_live = False
    #: True if the adapter can stream updates
    supports_streaming = False

    def __init__(self, credentials=None, **config):
        self.credentials = credentials
        self.config = dict(config)
        self._connected = False

    # -- lifecycle ------------------------------------------------------
    def connect(self):
        """Verify credentials / reachability. Idempotent."""
        self._connected = True
        return self

    def close(self):
        self._connected = False

    @property
    def connected(self):
        return self._connected

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self):
        return "<{} venue={} live={} connected={}>".format(
            type(self).__name__, self.venue, self.is_live, self._connected)

    # -- trading --------------------------------------------------------
    def submit_order(self, request):
        """Place an order. Returns an `Order`."""
        raise NotImplementedError

    def cancel_order(self, order_id):
        """Cancel a working order. Returns an `Order`."""
        raise NotImplementedError

    def get_order(self, order_id):
        raise NotImplementedError

    def list_orders(self, symbol=None, status=None, limit=100):
        raise NotImplementedError

    def positions(self):
        """Return a list of `Position`."""
        raise NotImplementedError

    def account(self):
        """Return `AccountInfo`."""
        raise NotImplementedError

    # -- market data ----------------------------------------------------
    def quote(self, symbol):
        raise NotImplementedError

    def quotes(self, symbols):
        return [self.quote(s) for s in symbols]

    def candles(self, symbol, timeframe="1m", limit=100):
        raise NotImplementedError

    def symbols(self):
        """Tradable instruments, if the venue advertises them."""
        raise BrokerNotSupported("{}: no symbol list".format(self.venue))

    # -- helpers for adapters -------------------------------------------
    def _now(self):
        return int(time.time())

    def health(self):
        """Non-throwing status dict for the UI / terminal."""
        return {
            "name": self.name,
            "venue": self.venue,
            "live": self.is_live,
            "connected": self._connected,
            "streaming": self.supports_streaming,
            "credentials": bool(self.credentials),
            "fingerprint": (self.credentials.fingerprint()
                            if self.credentials else None),
            "live_trading_enabled": live_trading_enabled(),
        }

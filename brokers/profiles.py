"""Venue profiles: declarative descriptions of a trading API's shape.

A profile is data, not code. It tells `RestBroker` where the endpoints
live, how the venue spells things, how it authenticates, and how to map
its response fields onto our normalized types. Adding a venue is usually a
new `VenueProfile(...)` literal -- no new adapter class, no new HTTP code.

The bundled profiles are *shape templates* modelled on the public REST
conventions of common venue families (HMAC-signed crypto exchanges,
bearer-token REST, exchange-style batch endpoints, broker-style accounts).
They are deliberately conservative about what they claim: every field
mapping here is something you would verify against that venue's own
documentation before trading real money.
"""

from dataclasses import dataclass, field


@dataclass
class VenueProfile:
    """Declarative description of one venue's REST API."""

    name: str
    venue: str
    base_url: str
    # endpoint templates; `{symbol}` / `{order_id}` are substituted
    endpoints: dict = field(default_factory=dict)
    # field name mapping: our canonical name -> venue's field name
    fields: dict = field(default_factory=dict)
    # venue status string -> our normalized status
    status_map: dict = field(default_factory=dict)
    # venue order-type string -> ours (and the inverse for submit)
    type_map: dict = field(default_factory=dict)
    # venue side string -> ours
    side_map: dict = field(default_factory=dict)
    # 'bearer' | 'header' | 'query' | 'hmac'
    auth_style: str = "bearer"
    # query params appended to every request (e.g. recvWindow)
    default_params: dict = field(default_factory=dict)
    # venue reports quantity in base units? (False => lots/contracts)
    lot_size: float = 1.0
    is_live_capable: bool = True
    notes: str = ""

    def endpoint(self, key, **subs):
        """Return a concrete URL for `key`, substituting placeholders."""
        path = self.endpoints.get(key)
        if not path:
            from .base import BrokerNotSupported
            raise BrokerNotSupported("{}: no endpoint '{}'".format(
                self.venue, key))
        for k, v in (subs or {}).items():
            token = "{" + k + "}"
            if token in path:
                path = path.replace(token, str(v))
        return self.base_url.rstrip("/") + path

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


# Canonical field names an adapter understands. A profile maps each to
# whatever the venue calls it; anything absent falls back to our name.
_CANONICAL_FIELDS = (
    "symbol", "side", "qty", "type", "status", "price", "stop_price",
    "filled_qty", "avg_price", "order_id", "client_order_id", "ts",
    "fee", "last", "bid", "ask", "bid_size", "ask_size",
    "cash", "equity", "buying_power", "currency", "account_id",
    "avg_entry", "realized_pnl", "position_qty",
)


def _fields(**over):
    """Build a field map: canonical name -> venue name (identity default)."""
    out = {k: k for k in _CANONICAL_FIELDS}
    out.update({k: v for k, v in (over or {}).items() if v})
    return out


# ------------------------------------------------------------------ HMAC REST
# HMAC-signed REST exchange (generic shape: key+secret, signed requests,
# `clientOrderId` for idempotency, list-of-dicts responses).
HMAC_STYLE = VenueProfile(
    name="hmac_style",
    venue="HMAC REST Exchange",
    base_url="https://api.example-hmac-exchange.com",
    endpoints={
        "time": "/api/v3/time",
        "exchange_info": "/api/v3/exchangeInfo",
        "ticker": "/api/v3/ticker/bookTicker",
        "klines": "/api/v3/klines",
        "order": "/api/v3/order",
        "open_orders": "/api/v3/openOrders",
        "account": "/api/v3/account",
        "positions": "/api/v3/positionRisk",
    },
    fields=_fields(
        symbol="symbol", side="side", qty="quantity", type="type",
        status="status", price="price", stop_price="stopPrice",
        filled_qty="executedQty", avg_price="avgPrice", order_id="orderId",
        client_order_id="clientOrderId", ts="updateTime", fee="commission",
        last="lastPrice", bid="bidPrice", ask="askPrice",
        bid_size="bidQty", ask_size="askQty", cash="freeBalance",
        equity="totalWalletBalance", currency="quoteAsset",
        position_qty="positionAmt", avg_entry="entryPrice",
        realized_pnl="realizedProfit",
    ),
    # The venue's UPPERCASE status vocabulary.
    status_map={
        "NEW": "new", "PARTIALLY_FILLED": "partial", "FILLED": "filled",
        "CANCELED": "cancelled", "PENDING_CANCEL": "cancelled",
        "REJECTED": "rejected", "EXPIRED": "expired", "EXPIRED_IN_MATCH": "expired",
    },
    type_map={"MARKET": "market", "LIMIT": "limit", "STOP_LOSS": "stop",
              "STOP_LOSS_LIMIT": "stop_limit"},
    side_map={"BUY": "buy", "SELL": "sell"},
    auth_style="hmac",
    default_params={"recvWindow": 5000},
    lot_size=1.0,
    notes="HMAC-SHA256 signed REST. Verify signature construction and "
          "recvWindow limits against the venue's own docs before going live.",
)

# ------------------------------------------------------------- bearer REST API
# OAuth/bearer-token REST (many modern broker APIs). Values arrive as
# strings; the adapter coerces.
BEARER_STYLE = VenueProfile(
    name="bearer_style",
    venue="Bearer REST API",
    base_url="https://api.example-broker.com",
    endpoints={
        "account": "/v3/accounts",
        "instruments": "/v3/instruments",
        "quote": "/v3/instruments/{symbol}/quote",
        "candles": "/v3/instruments/{symbol}/candles",
        "orders": "/v3/orders",
        "order": "/v3/orders/{order_id}",
        "positions": "/v3/positions",
    },
    fields=_fields(
        symbol="instrument", side="direction", qty="amount", type="order_type",
        status="state", price="limit_price", stop_price="stop_price",
        filled_qty="filled_amount", avg_price="average_price",
        order_id="id", client_order_id="client_ref", ts="created_at",
        fee="commission", last="last", bid="bid", ask="ask",
        bid_size="bid_size", ask_size="ask_size", cash="cash_balance",
        equity="equity", buying_power="buying_power", currency="currency",
        position_qty="net_quantity", avg_entry="average_entry",
        realized_pnl="realised_pnl",
    ),
    status_map={
        "pending": "new", "accepted": "open", "working": "open",
        "partially_filled": "partial", "filled": "filled",
        "cancelled": "cancelled", "canceled": "cancelled",
        "rejected": "rejected", "expired": "expired",
    },
    type_map={"MARKET": "market", "LIMIT": "limit", "STOP": "stop",
              "STOP_LIMIT": "stop_limit", "TRAILING": "trailing"},
    side_map={"buy": "buy", "sell": "sell", "BUY": "buy", "SELL": "sell",
              "long": "buy", "short": "sell"},
    auth_style="bearer",
    default_params={},
    lot_size=1.0,
    notes="OAuth/bearer REST. Many venues paginate; add `default_params` and "
          "endpoint tweaks per venue rather than forking the adapter.",
)

# ------------------------------------------------------- exchange-style batch
# Classic exchange REST: single-symbol endpoints, array responses, header
# auth. Useful shape for adapting legacy venue APIs.
EXCHANGE_STYLE = VenueProfile(
    name="exchange_style",
    venue="Legacy Exchange REST",
    base_url="https://api.example-legacy-exchange.com",
    endpoints={
        "quote": "/rest/quote/{symbol}",
        "candles": "/rest/candles/{symbol}",
        "submit": "/rest/order/new",
        "cancel": "/rest/order/cancel",
        "status": "/rest/order/status",
        "open_orders": "/rest/orders/open",
        "account": "/rest/account",
        "positions": "/rest/positions",
    },
    fields=_fields(
        symbol="instrument", side="side", qty="quantity", type="order_type",
        status="order_status", price="price", stop_price="stop",
        filled_qty="filled", avg_price="average_price", order_id="order_id",
        client_order_id="clOrdID", ts="timestamp", fee="fees",
        last="last", bid="bid", ask="ask", bid_size="bid_size",
        ask_size="ask_size", cash="balance", equity="net_liquidation",
        currency="currency", position_qty="net_position",
        avg_entry="avg_entry_price", realized_pnl="realised_pnl",
    ),
    status_map={
        "0": "new", "1": "open", "2": "partial", "3": "filled",
        "4": "cancelled", "5": "rejected", "6": "expired",
        "NEW": "new", "WORKING": "open", "PARTIALLY_FILLED": "partial",
        "FILLED": "filled", "CANCELLED": "cancelled", "REJECTED": "rejected",
    },
    type_map={"MARKET": "market", "LIMIT": "limit", "STOP": "stop",
              "STOP_LIMIT": "stop_limit"},
    side_map={"B": "buy", "S": "sell", "BUY": "buy", "SELL": "sell"},
    auth_style="header",
    default_params={},
    lot_size=1.0,
    notes="Legacy numeric status codes and single-letter sides -- this is the "
          "shape that most needs a profile rather than a forked adapter.",
)

# ----------------------------------------------------------------- registry
PROFILES = {
    p.name: p for p in (HMAC_STYLE, BEARER_STYLE, EXCHANGE_STYLE)
}


def get_profile(name):
    """Look up a profile by name. Raises BrokerNotSupported if unknown."""
    from .base import BrokerNotSupported
    prof = PROFILES.get(str(name or "").strip().lower())
    if prof is None:
        raise BrokerNotSupported(
            "unknown venue profile '{}'; have: {}".format(
                name, ", ".join(sorted(PROFILES))))
    return prof


def list_profiles():
    return [p.to_dict() for p in PROFILES.values()]

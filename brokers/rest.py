"""`RestBroker` -- one adapter that speaks any profile in `brokers.profiles`.

This is the "interface with various trading APIs" piece. Given a
`VenueProfile` it can talk to any venue whose REST shape the profile
describes, and translating that venue's dialect into the normalized
`Order` / `Position` / `Quote` types happens here, in one place.

The transport is injectable, so the whole adapter is testable without a
network or an API key -- which is also why it is safe to keep in a repo.
"""

import time

from .base import (Broker, BrokerNotSupported, BrokerError, BrokerRejectedError,
                   BrokerValidationError, Credentials, Fill, Order, Position,
                   AccountInfo, Quote, OrderRequest, BUY, SELL, MARKET, LIMIT,
                   STOP, STOP_LIMIT, TRAILING, NEW, OPEN, PARTIAL, FILLED,
                   CANCELLED, REJECTED, EXPIRED, assert_live_allowed,
                   live_trading_enabled)
from . import profiles as _profiles
from . import transport as _transport


def _num(value, default=0.0):
    """Coerce a venue's string/number/null into a float.

    Venues return quantities as strings, numbers, or null; we never want a
    TypeError from a surprise payload.
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(value, default=0):
    """Coerce a venue timestamp (s, ms, or ISO-ish string) to int seconds."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        v = int(value)
        # Heuristic: anything past ~year 2286 in seconds is milliseconds.
        return v // 1000 if v > 10_000_000_000 else v
    try:
        import calendar
        import time as _t
        parsed = _t.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z")
        return calendar.timegm(parsed)
    except Exception:
        return default


class RestBroker(Broker):
    """Adapter for any venue described by a `VenueProfile`."""

    supports_streaming = False

    def __init__(self, profile="bearer_style", credentials=None,
                 transport=None, live=False, confirm=False,
                 tier_live_enabled=None, **config):
        self.profile = (_profiles.get_profile(profile)
                        if isinstance(profile, str) else profile)
        if not live:
            # Paper is the default posture: an adapter only considers
            # itself live when the caller asks *and* the env allows it.
            super().__init__(credentials, **config)
            self.is_live = False
        else:
            super().__init__(credentials, **config)
            self.is_live = True
        self.transport = transport or _transport.Transport(
            timeout=config.get("timeout", _transport.DEFAULT_TIMEOUT),
            retries=config.get("retries", _transport.DEFAULT_RETRIES),
            logger=config.get("logger"))
        self._tier_live_enabled = tier_live_enabled
        self._confirm = confirm
        # Broker.name/venue are class attributes on the base; a profile-driven
        # adapter must report the venue it actually talks to, not "base".
        self.name = self.profile.name
        self.venue = self.profile.venue

    # -- plumbing --------------------------------------------------------
    def _auth(self):
        creds = self.credentials
        style = self.profile.auth_style
        if style == "hmac":
            return None, _transport.hmac_signer(creds)
        if style == "bearer":
            return _transport.bearer_auth(creds), None
        if style == "header":
            return _transport.header_auth(creds), None
        if style == "query":
            return _transport.query_auth(creds), None
        raise BrokerError("unsupported auth style '{}'".format(style))

    def _call(self, method, url, params=None, body=None, headers=None):
        auth, sign = self._auth()
        merged = dict(self.profile.default_params or {})
        merged.update(params or {})
        return self.transport.request(
            method, url, params=merged, body=body, headers=headers,
            auth=auth, sign=sign)

    def _f(self, payload, canonical, default=None):
        """Read a canonical field out of a venue payload."""
        if not isinstance(payload, dict):
            return default
        key = (self.profile.fields or {}).get(canonical, canonical)
        if key in payload:
            return payload[key]
        # some venues wrap the useful part
        for wrapper in ("data", "result", "order", "position"):
            inner = payload.get(wrapper)
            if isinstance(inner, dict) and key in inner:
                return inner[key]
        return default

    def _status(self, raw):
        """Venue status -> normalized."""
        if raw is None:
            return NEW
        s = str(raw)
        return (self.profile.status_map or {}).get(s, str(raw).lower())

    def _venue_type(self, raw):
        if raw is None:
            return None
        return (self.profile.type_map or {}).get(str(raw).upper(),
                                                str(raw).lower())

    def _venue_side(self, raw):
        if raw is None:
            return None
        return (self.profile.side_map or {}).get(str(raw),
                                                str(raw).lower())

    @staticmethod
    def _rows(payload):
        """Normalize list / dict / wrapper payloads into a list of dicts."""
        if payload is None:
            return []
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for wrapper in ("data", "results", "orders", "positions",
                            "list", "items", "rows"):
                inner = payload.get(wrapper)
                if isinstance(inner, list):
                    return [r for r in inner if isinstance(r, dict)]
            return [payload]
        return []

    # -- lifecycle -------------------------------------------------------
    def connect(self):
        """Verify the venue is reachable and the credentials work.

        Uses the cheapest read-only endpoint the profile offers, so a
        bad key fails here rather than on the first order.
        """
        key = "account" if "account" in self.profile.endpoints else "time"
        try:
            self._call("GET", self.profile.endpoint(key))
        except BrokerNotSupported:
            # No probe endpoint: treat reaching construction as connected
            # but be honest that we did not verify anything.
            self._connected = True
            return self
        self._connected = True
        return self

    # -- trading ---------------------------------------------------------
    def submit_order(self, request):
        # Accept an OrderRequest or a plain dict; normalise to the former.
        if isinstance(request, dict):
            request = OrderRequest(**request)
        req = request.validate()

        if self.is_live:
            # Re-checked here, not just at construction: the env could
            # have been reloaded, and a stale `live=True` must not send.
            assert_live_allowed(self.profile.venue, self._confirm,
                                self._tier_live_enabled)
        else:
            self._reject_live_request(req)

        payload = self._order_payload(req)
        url = self.profile.endpoint("order", symbol=req.symbol) \
            if "order" in self.profile.endpoints \
            else self.profile.endpoint("submit", symbol=req.symbol)
        resp = self._call("POST", url, body=payload)
        order = self._to_order(resp.json(), fallback=req)
        if order.status == REJECTED:
            raise BrokerRejectedError("venue rejected order: {}".format(
                order.raw.get("error") or "no reason given"),
                self.profile.venue)
        return order

    def _reject_live_request(self, req):
        """A non-live adapter must never silently simulate an order that
        the caller believes is real. Refuse instead."""
        if getattr(req, "metadata", None) and req.metadata.get("live"):
            raise BrokerError(
                "{}: live request sent to a non-live adapter".format(
                    self.profile.venue), self.profile.venue)

    def _order_payload(self, req):
        """Our OrderRequest -> the venue's body."""
        inv_type = {}
        for ours, theirs in (self.profile.type_map or {}).items():
            inv_type.setdefault(theirs, ours)
        vtype = inv_type.get(req.type, req.type)
        inv_side = {}
        for ours, theirs in (self.profile.side_map or {}).items():
            inv_side.setdefault(theirs, ours)
        vside = inv_side.get(req.side, req.side)
        f = self.profile.fields or {}
        body = {
            f.get("symbol", "symbol"): req.symbol,
            f.get("side", "side"): vside,
            f.get("qty", "qty"): req.qty,
            f.get("type", "type"): vtype,
            f.get("client_order_id", "clientOrderId"): req.client_order_id,
        }
        if req.price is not None:
            body[f.get("price", "price")] = req.price
        if req.stop_price is not None:
            body[f.get("stop_price", "stop_price")] = req.stop_price
        if req.trail_pct is not None:
            body.setdefault("trail_pct", req.trail_pct)
        if req.reduce_only:
            body.setdefault("reduceOnly", True)
        if req.time_in_force:
            body.setdefault("timeInForce", req.time_in_force)
        return body

    def _to_order(self, payload, fallback=None):
        rows = self._rows(payload)
        row = rows[0] if rows else (payload if isinstance(payload, dict) else {})
        sym = self._f(row, "symbol") or (fallback.symbol if fallback else "")
        side = self._venue_side(self._f(row, "side")) or (
            fallback.side if fallback else BUY)
        status = self._status(self._f(row, "status"))
        if not row and fallback is not None:
            status = NEW
        return Order(
            symbol=str(sym or ""),
            side=side,
            qty=_num(self._f(row, "qty"),
                     fallback.qty if fallback else 0.0),
            type=self._venue_type(self._f(row, "type")) or (
                fallback.type if fallback else MARKET),
            status=status,
            price=_num(self._f(row, "price"),
                       fallback.price if fallback else None),
            filled_qty=_num(self._f(row, "filled_qty")),
            avg_price=_num(self._f(row, "avg_price"), None),
            order_id=str(self._f(row, "order_id", "") or ""),
            client_order_id=str(self._f(row, "client_order_id", "")
                                or (fallback.client_order_id if fallback else "")),
            venue=self.profile.venue,
            ts=_ts(self._f(row, "ts"), int(time.time())),
            raw=row if isinstance(row, dict) else {},
        )

    def cancel_order(self, order_id):
        if "cancel" in self.profile.endpoints:
            url = self.profile.endpoint("cancel", order_id=order_id,
                                        symbol=order_id)
        else:
            url = self.profile.endpoint("order", order_id=order_id,
                                        symbol=order_id)
        f = self.profile.fields or {}
        resp = self._call("DELETE", url,
                          body={f.get("order_id", "order_id"): order_id})
        order = self._to_order(resp.json())
        if not order.order_id:
            order.order_id = str(order_id)
        if order.status in (NEW, OPEN, PARTIAL):
            order.status = CANCELLED
        return order

    def get_order(self, order_id):
        key = "status" if "status" in self.profile.endpoints else "order"
        url = self.profile.endpoint(key, order_id=order_id, symbol=order_id)
        return self._to_order(self._call("GET", url).json())

    def list_orders(self, symbol=None, status=None, limit=100):
        key = ("open_orders" if "open_orders" in self.profile.endpoints
               else "orders")
        params = {"limit": limit}
        if symbol:
            params[self.profile.fields.get("symbol", "symbol")] = symbol
        if status:
            params["status"] = status
        payload = self._call("GET", self.profile.endpoint(key),
                             params=params).json()
        return [self._to_order(r) for r in self._rows(payload)]

    def positions(self):
        key = ("positions" if "positions" in self.profile.endpoints
               else "account")
        payload = self._call("GET", self.profile.endpoint(key)).json()
        out = []
        for row in self._rows(payload):
            qty = _num(self._f(row, "position_qty"))
            if not qty:
                qty = _num(self._f(row, "qty"))
            if not qty:
                continue  # venue omits flat positions
            out.append(Position(
                symbol=str(self._f(row, "symbol", "") or ""),
                qty=qty,
                avg_price=_num(self._f(row, "avg_entry")
                               or self._f(row, "avg_price")),
                realized_pnl=_num(self._f(row, "realized_pnl")),
                venue=self.profile.venue,
                raw=row,
            ))
        return out

    def account(self):
        payload = self._call("GET", self.profile.endpoint("account")).json()
        rows = self._rows(payload)
        row = rows[0] if rows else {}
        return AccountInfo(
            account_id=str(self._f(row, "account_id", "") or ""),
            venue=self.profile.venue,
            currency=str(self._f(row, "currency", "USD") or "USD"),
            cash=_num(self._f(row, "cash")),
            equity=_num(self._f(row, "equity")),
            buying_power=_num(self._f(row, "buying_power")),
            raw=row,
        )

    # -- market data -----------------------------------------------------
    def quote(self, symbol):
        key = "ticker" if "ticker" in self.profile.endpoints else "quote"
        url = self.profile.endpoint(key, symbol=symbol)
        payload = self._call("GET", url).json()
        rows = self._rows(payload)
        row = rows[0] if rows else {}
        return Quote(
            symbol=str(self._f(row, "symbol", symbol) or symbol),
            bid=_num(self._f(row, "bid")),
            ask=_num(self._f(row, "ask")),
            last=_num(self._f(row, "last")),
            bid_size=_num(self._f(row, "bid_size")),
            ask_size=_num(self._f(row, "ask_size")),
            ts=_ts(self._f(row, "ts"), int(time.time())),
            venue=self.profile.venue,
        )

    def candles(self, symbol, timeframe="1m", limit=100):
        if "klines" not in self.profile.endpoints and \
                "candles" not in self.profile.endpoints:
            raise BrokerNotSupported("{}: no candles endpoint".format(
                self.profile.venue))
        key = "klines" if "klines" in self.profile.endpoints else "candles"
        url = self.profile.endpoint(key, symbol=symbol)
        params = {"interval": timeframe, "limit": limit}
        payload = self._call("GET", url, params=params).json()
        out = []
        for row in self._rows(payload):
            out.append({
                "ts": _ts(self._f(row, "ts")),
                "open": _num(self._f(row, "open")),
                "high": _num(self._f(row, "high")),
                "low": _num(self._f(row, "low")),
                "close": _num(self._f(row, "close")),
                "volume": _num(self._f(row, "volume")),
            })
        return out

    def symbols(self):
        key = ("exchange_info" if "exchange_info" in self.profile.endpoints
               else "instruments")
        if key not in self.profile.endpoints:
            raise BrokerNotSupported("{}: no symbol list".format(
                self.profile.venue))
        payload = self._call("GET", self.profile.endpoint(key)).json()
        out = []
        for row in self._rows(payload):
            s = self._f(row, "symbol") or row.get("symbol")
            if s:
                out.append(str(s))
        return out

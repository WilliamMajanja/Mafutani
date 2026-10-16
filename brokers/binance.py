"""Binance spot adapter. Stdlib only, no ccxt, no vendor SDK.

The first venue in this codebase that can actually execute.

Why this module rather than another `VenueProfile`
-------------------------------------------------
`profiles.py` describes venues *by shape*, and every shipped profile points
at `api.example-*.com`. That is deliberate for fixtures, but it means the
generic `RestBroker` cannot reach a real exchange: the endpoints are
placeholders and, more importantly, none of the profiles can *sign* a
request. Binance authenticates by HMAC-SHA256 over the query string with
the secret appended, which is a per-venue rule no amount of endpoint data
describes. So a real venue needs real code, which is this file.

Signing, and why it is stdlib
-----------------------------
`hmac`, `hashlib`, `urllib`, `secrets`, `json`. All standard library, so
the project stays zero-dependency. The canonical string is
`query_string + request_body`, signed with SHA256, hex, and sent in the
`X-MBX-APIKEY` header alongside the key. The timestamp is in
milliseconds and must be inside the venue's `recvWindow`, which is why
`recvWindow` is sent explicitly rather than left to the default.

The three states this module refuses to conflate
------------------------------------------------
This is the whole reason the file exists. The previous live path called a
stub that returned `{"status": "routed_stub"}`, and the order handler
treated any non-rejected status as a fill -- so a "live" buy produced a
simulated position and P&L for an asset the user did not own.

So:

* `FILLED` / `PARTIAL` are only ever set from the venue's own response.
* A transport failure -- timeout, DNS, TLS, 5xx, malformed body -- raises
  `BrokerUnavailableError`. It never returns a status. The caller must
  record the order `unknown`, because "we do not know" and "it filled"
  are different facts and only one of them costs money.
* A venue rejection raises `BrokerRejectedError` carrying the venue's own
  reason code, which is the difference between "you sent dust" and
  "your key is wrong".

`UNKNOWN` is exported so the order table can hold a row that is neither
filled nor rejected, and so an operator can find it again.

Testnet
-------
`base_url` can point at `https://testnet.binance.vision`, which is a real
exchange with real signing and real order handling but no real money. It
is the only responsible way to verify this file end to end, and
`BROKER_BINANCE_TESTNET=1` selects it. Verify there before risking funds.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

from .base import (Broker, BrokerAuthError, BrokerError, BrokerRateLimitError,
                   BrokerRejectedError, BrokerUnavailableError,
                   BrokerValidationError, Credentials, Order, OrderRequest,
                   Position, Quote, BUY, SELL, MARKET, LIMIT,
                   FILLED, PARTIAL, NEW, REJECTED, CANCELLED, EXPIRED,
                   assert_live_allowed, live_trading_enabled)

# Order state we know we do not have. Deliberately not a member of
# TERMINAL_STATUSES: an unknown order is still in flight, and treating it
# as terminal would hide a position the user may well own.
UNKNOWN = "unknown"

PROD_BASE = "https://api.binance.com"
TESTNET_BASE = "https://testnet.binance.vision"

USER_AGENT = "mafutani-cta/1.1"

# Venue error code -> exception class. Codes are Binance's own and are
# stable; the two that matter most are -2015 (rejected by filters: dust,
# bad step size) and -2014 (bad key), because they mean the opposite
# things and an operator must be able to tell them apart.
_AUTH_CODES = frozenset([-2014, -2015, -1022])
_RATE_CODES = frozenset([-1003, -1006])
_FILTER_CODES = frozenset([-1013, -2010, -2011, -2013, -2015, -4164])


class BinanceBroker(Broker):
    """Binance spot. Signs with HMAC-SHA256, submits, and reconciles."""

    venue = "binance"

    def __init__(self, credentials=None, live=False, confirm=False,
                 tier_live_enabled=None, transport=None, base_url=None,
                 recv_window=5000, timeout=10, sleep=time.sleep, **config):
        self._creds = credentials or Credentials()
        self._live = bool(live)
        self._confirm = bool(confirm)
        self._tier_live = tier_live_enabled
        self._transport = transport
        self._recv_window = int(recv_window)
        self._timeout = float(timeout)
        self._sleep = sleep
        self._connected = False
        # `user_id` and friends travel through the base interface's
        # **config bag; kept rather than rejected so get_broker() can pass
        # them without every adapter having to declare them.
        self.config = dict(config)
        self.base_url = (base_url or self._default_base()).rstrip("/")

    @staticmethod
    def _default_base():
        if os.environ.get("BROKER_BINANCE_TESTNET", "0") == "1":
            return TESTNET_BASE
        return PROD_BASE

    @property
    def is_live(self):
        return self._live

    # ------------------------------------------------------------------ auth
    def _require_creds(self):
        if not (self._creds.get("api_key") and self._creds.get("secret")):
            raise BrokerAuthError(
                "binance: no credentials. Set BROKER_BINANCE_API_KEY and "
                "BROKER_BINANCE_SECRET.", self.venue)

    def _signed(self, method, path, params=None):
        """Return (url, headers) for a signed request.

        The canonical string is the urlencoded query with the timestamp
        appended *before* signing, and the signature covers exactly those
        bytes. Building the query in one place and reusing the same string
        for both the signature and the wire is deliberate: reassembling it
        a second time is how a signer ends up signing one string and
        sending another.
        """
        self._require_creds()
        q = dict(params or {})
        q["timestamp"] = int(time.time() * 1000)
        q["recvWindow"] = self._recv_window
        qs = urllib.parse.urlencode(q)
        mac = hmac.new(str(self._creds.get("secret")).encode("utf-8"),
                       qs.encode("utf-8"), hashlib.sha256).hexdigest()
        # The signature belongs IN the query string, appended after it has
        # been signed -- not in a header. Signing and forgetting to send it
        # is silently wrong in a way that looks like a credentials problem:
        # Binance answers -1102 ("signature was not sent") and an operator
        # goes looking for a bad key instead of a missing parameter.
        qs = qs + "&signature=" + mac
        return (self.base_url + path + "?" + qs,
                {"X-MBX-APIKEY": str(self._creds.get("api_key"))})

    def _public(self, path, params=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._http("GET", url, None)

    def _http(self, method, url, body=None, headers=None):
        """One HTTP call, mapped onto the venue's own error vocabulary.

        Raises rather than returning a degraded dict. A caller that cannot
        distinguish "no answer" from "answered" will eventually report an
        order it never placed.

        Routed through the operator's proxy when one is configured, so a
        signed live order need not originate from this machine's IP. With
        no proxy set, this is the same urllib call it always was.
        """
        try:
            from data import proxy as _proxy
        except ImportError:  # pragma: no cover - alternate sys.path
            _proxy = None
        if _proxy is not None and _proxy.proxy_url() is not None:
            try:
                status, _hdrs, raw = _proxy.request(
                    method, url, body=body, headers=headers,
                    timeout=self._timeout)
            except Exception as e:
                # Timeout, DNS, TLS, dead proxy: the order's fate is
                # UNKNOWN, never reported as filled.
                raise BrokerUnavailableError(
                    "binance: transport failure via proxy ({}): {}".format(
                        type(e).__name__, e), self.venue)
            if status >= 400:
                self._raise_for_error(raw, status)
                raise BrokerUnavailableError(
                    "binance: HTTP {} with an unreadable body".format(status),
                    self.venue)
            try:
                return json.loads(raw)
            except (TypeError, ValueError) as e:
                raise BrokerUnavailableError(
                    "binance: non-JSON response: {}".format(e), self.venue)

        req = urllib.request.Request(url, method=method, data=body,
                                     headers=headers or {})
        req.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            self._raise_for_error(raw, getattr(e, "code", 0))
            raise BrokerUnavailableError(
                "binance: HTTP {} with an unreadable body".format(
                    getattr(e, "code", "?")), self.venue)
        except Exception as e:
            # Timeouts, DNS, TLS resets, malformed responses. All of these
            # mean the order's fate is UNKNOWN -- never filled.
            raise BrokerUnavailableError(
                "binance: transport failure ({}): {}".format(
                    type(e).__name__, e), self.venue)
        if status >= 400:
            self._raise_for_error(raw, status)
            raise BrokerUnavailableError(
                "binance: HTTP {} with an unreadable body".format(status),
                self.venue)
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as e:
            raise BrokerUnavailableError(
                "binance: non-JSON response: {}".format(e), self.venue)

    def _raise_for_error(self, raw, http_status):
        code, msg = None, ""
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                code = body.get("code")
                msg = body.get("msg") or ""
        except (TypeError, ValueError):
            pass
        detail = "{} (code={}, http={})".format(msg or raw[:160], code, http_status)
        if code in _AUTH_CODES and http_status in (401, 403):
            raise BrokerAuthError("binance: rejected credentials: " + detail,
                                  self.venue)
        if code in _RATE_CODES or http_status == 429:
            raise BrokerRateLimitError("binance: rate limited: " + detail,
                                       self.venue)
        if code is not None:
            raise BrokerRejectedError("binance: " + detail, self.venue)
        raise BrokerUnavailableError("binance: " + detail, self.venue)

    # ------------------------------------------------------------- lifecycle
    def connect(self):
        """Verify the key actually works, using the cheapest signed read.

        A bad key must fail here rather than on the first order, where the
        failure would be ambiguous with a market rejection.
        """
        if self._live:
            assert_live_allowed(self.venue, self._confirm, self._tier_live)
        url, headers = self._signed("GET", "/api/v3/account")
        self._http("GET", url, None, headers)
        self._connected = True
        return self

    # --------------------------------------------------------------- trading
    def submit_order(self, request):
        if isinstance(request, dict):
            request = OrderRequest(**request)
        req = request.validate()
        if self._live:
            # Re-derived, not cached: the env or the tier can change under
            # a long-lived process, and a stale "live is fine" is the one
            # thing that must not reach an exchange.
            assert_live_allowed(self.venue, self._confirm, self._tier_live)
        else:
            raise BrokerError(
                "binance: refusing to submit -- adapter is not live. A "
                "non-live adapter must never place a real order.", self.venue)

        params = {
            "symbol": self._venue_symbol(req.symbol),
            "side": req.side.upper(),
            "type": req.type.upper(),
            "quantity": self._fmt_qty(req.qty),
        }
        if req.type in (LIMIT,) and req.price:
            params["price"] = self._fmt_px(req.price)
            params["timeInForce"] = "GTC"
        if req.client_order_id:
            params["newClientOrderId"] = req.client_order_id

        url, headers = self._signed("POST", "/api/v3/order", params)
        raw = self._http("POST", url, None, headers)
        return self._to_order(raw, fallback=req)

    def cancel_order(self, order_id, symbol):
        url, headers = self._signed("DELETE", "/api/v3/order",
                                    {"symbol": self._venue_symbol(symbol),
                                     "orderId": order_id})
        raw = self._http("DELETE", url, None, headers)
        return self._to_order(raw)

    def get_order(self, order_id, symbol):
        url, headers = self._signed("GET", "/api/v3/order",
                                    {"symbol": self._venue_symbol(symbol),
                                     "orderId": order_id})
        raw = self._http("GET", url, None, headers)
        return self._to_order(raw)

    def positions(self):
        """Spot balances as signed positions.

        Spot has no short and no per-contract position: a balance IS the
        position. `free` excludes anything locked in an open order, so a
        quantity that is committed is not double-counted as available.
        """
        url, headers = self._signed("GET", "/api/v3/account")
        raw = self._http("GET", url, None, headers)
        out = []
        for b in (raw.get("balances") or []):
            try:
                free = float(b.get("free") or 0)
                locked = float(b.get("locked") or 0)
            except (TypeError, ValueError):
                continue
            total = free + locked
            if total <= 0:
                continue
            out.append(Position(symbol=str(b.get("asset") or "").upper(),
                                qty=total, venue=self.venue,
                                avg_price=None,
                                raw={"free": free, "locked": locked}))
        return out

    def quote(self, symbol):
        raw = self._public("/api/v3/ticker/bookTicker",
                           {"symbol": self._venue_symbol(symbol)})
        try:
            bid, ask = float(raw["bidPrice"]), float(raw["askPrice"])
        except (KeyError, TypeError, ValueError) as e:
            raise BrokerUnavailableError(
                "binance: unusable ticker for {}: {}".format(symbol, e),
                self.venue)
        return Quote(symbol=str(symbol).upper(), bid=bid, ask=ask,
                     venue=self.venue, ts=int((raw.get("closeTime") or 0) / 1000)
                     or int(time.time()))

    def account(self):
        """Balances as AccountInfo.

        Spot reports per-asset balances, not a single cash figure, so
        `cash` is left None rather than being summed across assets that
        are not fungible. Summing BTC and USDT into one number would be a
        category error dressed up as a total.
        """
        from .base import AccountInfo
        url, headers = self._signed("GET", "/api/v3/account")
        raw = self._http("GET", url, None, headers)
        return AccountInfo(venue=self.venue, account_id=None,
                           currency=None, cash=None, equity=None,
                           buying_power=None, margin_used=None,
                           currency_pairs=None, raw=raw)

    # -------------------------------------------------------------- mapping
    _STATUS = {"NEW": NEW, "PARTIALLY_FILLED": PARTIAL, "FILLED": FILLED,
               "CANCELED": CANCELLED, "PENDING_CANCEL": CANCELLED,
               "EXPIRED": EXPIRED, "REJECTED": REJECTED}

    def _to_order(self, raw, fallback=None):
        """Venue response -> Order. Status comes only from the venue."""
        if not isinstance(raw, dict):
            raise BrokerUnavailableError(
                "binance: unusable order response: {}".format(str(raw)[:160]),
                self.venue)
        status = self._STATUS.get(str(raw.get("status") or "").upper(), UNKNOWN)
        qty = _f(raw.get("origQty")) or (fallback.qty if fallback else 0.0)
        filled = _f(raw.get("executedQty")) or 0.0
        avg = _f(raw.get("cummulativeQuoteQty"))
        price = avg / filled if (avg and filled) else _f(raw.get("price"))
        symbol = raw.get("symbol") or (fallback.symbol if fallback else "")
        return Order(
            symbol=symbol, side=str(raw.get("side") or
                                    (fallback.side if fallback else "")).lower(),
            qty=qty, type=str(raw.get("type") or
                              (fallback.type if fallback else MARKET)).lower(),
            status=status,
            price=_f(raw.get("price")) or price,
            filled_qty=filled, avg_price=avg or price,
            order_id=str(raw.get("orderId") or ""),
            client_order_id=str(raw.get("clientOrderId") or
                                (fallback.client_order_id if fallback else "")),
            venue=self.venue, ts=int(raw.get("updateTime") or
                                     (time.time() * 1000)), raw=raw)

    # Symbol mapping: the app names instruments BTCUSD / BTCUSDT; the venue
    # lists BTCUSDT. An explicit map, never a suffix rule.
    #
    # The suffix rule this replaces ("ends in USD -> swap for USDT") produced
    # XAUUSDT for gold, and the venue's gold pair is XAUTUSDT. That is the
    # exact failure data/cryptomarkets.py documents: a symbol that does not
    # happen to be the venue's own pair name silently queries a pair that
    # does not exist. On a REST API it surfaces as a rejected order, which
    # is at least loud -- but the lesson stands, so unmapped symbols are
    # refused rather than guessed at.
    VENUE_ALIASES = {
        "BTCUSD": "BTCUSDT",
        "ETHUSD": "ETHUSDT",
        "SOLUSD": "SOLUSDT",
        "XRPUSD": "XRPUSDT",
        # Gold is XAUT on the venue, not XAU. Nothing about the app's
        # symbol name would let you derive that.
        "XAUUSD": "XAUTUSDT",
    }

    def _venue_symbol(self, symbol):
        s = str(symbol or "").strip().upper()
        if not s:
            raise BrokerValidationError("symbol is required")
        if s in self.VENUE_ALIASES:
            return self.VENUE_ALIASES[s]
        # Already venue-shaped (BTCUSDT, USDT) passes through.
        if s.endswith(("USDT", "BUSD", "BTC", "ETH", "BNB")):
            return s
        raise BrokerValidationError(
            "no binance mapping for '{}'. Known: {}. Add it to "
            "BinanceBroker.VENUE_ALIASES rather than letting a suffix rule "
            "guess.".format(s, ", ".join(sorted(self.VENUE_ALIASES))))

    def _fmt_qty(self, qty):
        """Binance rejects excess decimals with -1013.

        Formatting to 8dp and letting the venue's LOT_SIZE filter be the
        final authority is better than guessing a step size locally: a
        wrong local step produces dust orders, a wrong assumption about
        the venue's filter produces rejections we could have avoided.
        """
        return "{:.8f}".format(float(qty)).rstrip("0").rstrip(".")

    def _fmt_px(self, px):
        return "{:.8f}".format(float(px)).rstrip("0").rstrip(".")

    # ---------------------------------------------------------- description
    def describe(self):
        return {
            "venue": self.venue,
            "base_url": self.base_url,
            "live": self._live,
            "configured": bool(self._creds.get("api_key") and
                               self._creds.get("secret")),
            "recv_window_ms": self._recv_window,
            "signing": "HMAC-SHA256 over the urlencoded query string",
        }


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v

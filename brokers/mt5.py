"""MetaTrader 5 bridge: a Broker adapter driven by an MQL5 EA.

    Mafutani app (WSL2)  --HTTP-->  bridge server (Windows, stdlib)
                                         ^
                                         | WebRequest() long-poll
                                    MQL5 EA (inside the MT5 terminal)

Why this shape and not "an EA that serves HTTP"
------------------------------------------------
MQL5 has no server primitive. `WebRequest()` is strictly an outbound client
call, and every URL it touches has to be whitelisted in
Tools > Options > Expert Advisors. So an EA cannot listen on a port, and
the arrangement that actually works inverts the usual one: a Python server
runs next to the terminal, and the EA is the client.

That is not a workaround. It puts the part that can be tested (the protocol,
the adapter, the queue) on this side of the line, in Python, where it is
covered by tests, and leaves only the thin `OrderSend` shim on the far side
where it cannot be.

The single most important property of this adapter
---------------------------------------------------
**`retcode 10009` (TRADE_RETCODE_DONE) does not mean the order filled.**

It means the terminal accepted the request for processing. The order may
still be rejected by the server a moment later, may sit unfilled, or may
fill partially. This is the MT5 equivalent of an exchange returning
"accepted" and it is the single easiest way to lose real money silently,
because the response looks like a success.

So `submit_order` reports `status="open"`, never `"filled"`, and attaches
`retcode` so the caller can see exactly what the terminal said. A fill is
only ever reported when the EA reports one from an actual
`OnTradeTransaction`. This mirrors the existing invariant that a live
order can never silently simulate a fill -- there is a test for it.

MT5 retcodes are mapped in `_RETCODES`. Anything unmapped is surfaced
verbatim rather than collapsed into a generic failure, because MT5 adds
new codes and a silent default would hide them.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import (Broker, BrokerAuthError, BrokerError, BrokerNotSupported,
                   BrokerRejectedError, BrokerUnavailableError,
                   BrokerValidationError, Credentials, Order, OrderRequest,
                   Position, Quote, assert_live_allowed)

# The bridge speaks plain HTTP on the operator's own machine, where the
# terminal has already authenticated to its broker. There are no venue
# credentials in flight -- the trust boundary is "who can reach this port".
#
# That is a different justification than the one `transport.Transport`
# refuses HTTP for. That module protects *venue API keys* travelling to a
# third party over a network we do not control. Here the listener is
# loopback on the operator's desktop and the request carries only a shared
# token, so the plaintext is not carrying anything a sniff of the loopback
# could not already reach.
#
# It is still opt-in and still refuses any non-loopback host by default,
# because the failure mode of getting this wrong is a stranger placing
# orders in a real account.
BRIDGE_HOSTS_ALLOWED_PLAINTEXT = ("127.0.0.1", "::1", "localhost",
                                  "host.docker.internal")

# The EA's WebRequest poll interval. A terminal on OnTimer cannot poll much
# faster than this without being throttled by the terminal itself.
DEFAULT_POLL_S = 0.5

# How long the bridge server holds a command open waiting for the EA.
DEFAULT_BRIDGE_WAIT_S = 20.0

# Added to the socket timeout so the bridge's own 504 wins the race against
# the client's socket timeout. See the note on `_call`.
DEFAULT_TIMEOUT_MARGIN_S = 5.0

# How long the bridge holds an open /next request waiting for a command.
DEFAULT_POLL_TIMEOUT_S = 25.0

# Subset of MQL5 `ENUM_TRADE_RETCODE` we translate. Codes are stable API
# constants in the terminal, not HTTP statuses, and the distinction between
# "rejected, fix the order" and "busy, try again" decides whether retrying
# is safe -- so the mapping is explicit rather than derived.
_RETCODES = {
    10009: ("accepted", "terminal accepted the request; NOT filled"),
    10013: ("rejected", "no money"),
    10014: ("rejected", "invalid volume for this symbol"),
    10015: ("rejected", "invalid price"),
    10016: ("rejected", "invalid stops"),
    10017: ("rejected", "invalid trade volume / lot step"),
    10018: ("rejected", "market closed"),
    10019: ("rejected", "no changes"),
    10020: ("rejected", "order disabled"),
    10021: ("rejected", "not enough money"),
    10022: ("rejected", "invalid parameter"),
    10023: ("rejected", "invalid order ticket"),
    10024: ("rejected", "order not supported by this account"),
    10025: ("rejected", "trade context busy"),
    10027: ("rejected", "trade context busy"),
    10028: ("rejected", "requote"),
    10029: ("rejected", "order locked"),
    10030: ("rejected", "too many requests"),
}

#: retcodes worth retrying rather than surfacing as a hard failure.
RETRYABLE_RETCODES = (10025, 10027, 10028, 10030)


def map_retcode(code):
    """(status, explanation) for an MT5 retcode. Never raises.

    An unmapped code returns `("rejected", "unmapped retcode <n>")` rather
    than a success-shaped value, because the failure direction of a new
    MT5 code is always "something went wrong" and defaulting to anything
    permissive is how an unmapped code becomes a silent loss.
    """
    try:
        code = int(code)
    except (TypeError, ValueError):
        return ("rejected", "no retcode reported")
    if code in _RETCODES:
        return _RETCODES[code]
    if code == 10009:
        return ("accepted", "terminal accepted the request; NOT filled")
    return ("rejected", "unmapped retcode {}".format(code))


def is_accepted(code):
    """True only for retcodes the terminal confirmed it accepted."""
    return map_retcode(code)[0] == "accepted"


class MT5Broker(Broker):
    """Broker adapter for a MetaTrader 5 terminal via the EA bridge."""

    name = "mt5"
    venue = "MetaTrader 5"
    is_live = True
    supports_streaming = False

    def __init__(self, credentials=None, live=False, confirm=False,
                 tier_live_enabled=None, token="", host=None, port=None,
                 timeout=10.0, **_kw):
        super().__init__(credentials=credentials)
        self.live = bool(live)
        self.confirm = bool(confirm)
        self.tier_live_enabled = tier_live_enabled
        self.token = token or (credentials.get("api_key") if credentials else "")
        host = host or os.environ.get("MAFUTANI_MT5_HOST", "127.0.0.1")
        port = port or os.environ.get("MAFUTANI_MT5_PORT", "8031")
        self.base_url = "http://{}:{}".format(host, port)
        self.timeout = float(timeout)
        self._probe_result = None

    # -- lifecycle ------------------------------------------------------
    def connect(self):
        """Verify the bridge is up. Idempotent; raises if it is not."""
        res = self._read({"op": "ping"})
        self._probe_result = res
        self._connected = True
        return self

    def terminal_info(self):
        """What the EA reports about itself: build, account, terminal."""
        if self._probe_result is None:
            self.connect()
        return dict(self._probe_result or {})

    # -- reads ----------------------------------------------------------
    def quote(self, symbol):
        data = self._read({"op": "quote", "symbol": str(symbol).upper()})
        return Quote(symbol=str(symbol).upper(),
                     bid=float(data.get("bid") or 0.0),
                     ask=float(data.get("ask") or 0.0),
                     bid_size=float(data.get("bid_size") or 0.0),
                     ask_size=float(data.get("ask_size") or 0.0),
                     last=float(data.get("last") or 0.0),
                     ts=int(data.get("ts") or 0))

    def account(self):
        d = self._read({"op": "account"})
        return {"cash": float(d.get("cash") or 0.0),
                "equity": float(d.get("equity") or 0.0),
                "buying_power": float(d.get("buying_power") or 0.0),
                "currency": d.get("currency") or "",
                "account_id": d.get("account_id") or ""}

    def positions(self):
        out = []
        for d in self._read({"op": "positions"}).get("positions") or []:
            out.append(Position(
                symbol=d.get("symbol", ""),
                qty=float(d.get("qty") or 0.0),
                avg_price=float(d.get("avg_price") or d.get("avg_entry") or 0.0),
                realized_pnl=float(d.get("realized_pnl") or 0.0),
                unrealized_pnl=(float(d["unrealized_pnl"])
                                if d.get("unrealized_pnl") is not None else None),
                venue=self.venue,
                raw=dict(d)))
        return out

    def list_orders(self, symbol=None, status=None, limit=100):
        payload = {"op": "open_orders"}
        if symbol:
            payload["symbol"] = str(symbol).upper()
        if status:
            payload["status"] = status
        payload["limit"] = int(limit)
        out = []
        for d in self._read(payload).get("orders") or []:
            out.append(self._order_from(d))
        return out

    def get_order(self, order_id):
        d = self._read({"op": "order", "ticket": int(order_id)})
        if not d:
            raise BrokerNotSupported("MT5 reported no such ticket")
        return self._order_from(d)

    # -- writes ---------------------------------------------------------
    def submit_order(self, request):
        """Send an order. Reports `open` on acceptance, never `filled`.

        See the module docstring: retcode 10009 is acceptance, not a fill,
        and collapsing the two is the failure this adapter exists to avoid.
        """
        req = (request if isinstance(request, OrderRequest)
               else OrderRequest(**dict(request or {})))
        req.validate()

        # Same three-part gate as every other live path in this codebase.
        # Called before any command is queued so a blocked order never
        # reaches the terminal at all.
        assert_live_allowed(venue=self.venue, confirm=self.confirm,
                            tier_live_enabled=self.tier_live_enabled)

        payload = {"op": "submit", "order": self._order_to(req)}
        res = self._call(payload)
        if not res.get("ok"):
            # The EA refused outright (not logged in, market closed, ...).
            # `res` carries no retcode in that case, so this is a rejection
            # by the bridge itself rather than by the trade server.
            raise BrokerRejectedError(
                "MT5 terminal refused the order: {}".format(
                    res.get("error") or "unspecified"), venue=self.venue)
        d = res.get("data") or {}
        retcode = d.get("retcode")
        status, why = map_retcode(retcode)
        if not is_accepted(retcode):
            raise BrokerRejectedError(
                "MT5 rejected {}: retcode {} ({})".format(
                    req.symbol, retcode, why), venue=self.venue,
                status=int(retcode) if retcode is not None else None,
                retryable=(int(retcode) in RETRYABLE_RETCODES
                           if retcode is not None else False))
        return Order(
            order_id=str(d.get("ticket") or d.get("order_id") or ""),
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            type=req.type,
            status="open",
            price=float(d.get("price") or req.price or 0.0),
            filled_qty=float(d.get("filled_qty") or 0.0),
            avg_price=float(d.get("avg_price") or 0.0),
            venue=self.venue,
            ts=int(d.get("ts") or time.time()),
            # MT5-specific detail lives in `raw`, which is this codebase's
            # place for anything the normalized Order has no field for.
            # `filled: False` is the load-bearing entry -- it records that
            # acceptance is not a fill, so nothing downstream can read an
            # accepted-but-unfilled order as a completed one.
            raw={"retcode": retcode, "retcode_meaning": why,
                 "filled": False,
                 "note": "accepted by terminal, not confirmed filled"})

    def cancel_order(self, order_id):
        res = self._call({"op": "cancel", "ticket": int(order_id)})
        d = res.get("data") or {}
        retcode = d.get("retcode")
        if not is_accepted(retcode):
            _s, why = map_retcode(retcode)
            raise BrokerRejectedError(
                "MT5 cancel rejected: retcode {} ({})".format(retcode, why),
                venue=self.venue)
        return {"order_id": str(order_id), "status": "cancelled"}

    # -- translation ----------------------------------------------------
    def _order_to(self, req):
        """Our OrderRequest -> the EA's command shape.

        `client_order_id` is carried across specifically so the EA can echo
        it back. MT5 has no client-order-id field, so this is the only
        thing tying our order to the terminal's ticket, and dropping it
        would make retries unreconcilable.
        """
        return {"symbol": req.symbol, "side": req.side, "qty": req.qty,
                "type": req.type, "price": req.price,
                "stop_price": req.stop_price, "trail_pct": req.trail_pct,
                "client_order_id": req.client_order_id,
                "reduce_only": bool(req.reduce_only),
                "time_in_force": req.time_in_force}

    def _order_from(self, d):
        return Order(
            order_id=str(d.get("ticket") or d.get("order_id") or ""),
            client_order_id=d.get("client_order_id") or "",
            symbol=d.get("symbol", ""), side=d.get("side", "buy"),
            qty=float(d.get("qty") or 0.0), type=d.get("type", "market"),
            status=self._norm_status(d.get("status")),
            price=float(d.get("price") or 0.0),
            filled_qty=float(d.get("filled_qty") or 0.0),
            avg_price=float(d.get("avg_price") or 0.0),
            venue=self.venue, ts=int(d.get("ts") or 0),
            raw=dict(d))

    @staticmethod
    def _norm_status(s):
        v = str(s or "").strip().lower()
        return {"placed": "open", "accepted": "open", "filled": "filled",
                "canceled": "cancelled", "cancelled": "cancelled",
                "partial": "partial", "rejected": "rejected",
                "expired": "expired"}.get(v, v or "open")

    # -- transport ------------------------------------------------------
    def _read(self, cmd):
        res = self._call(cmd)
        if not res.get("ok"):
            raise BrokerRejectedError(
                "MT5 bridge reported an error: {}".format(
                    res.get("error") or "unspecified"), venue=self.venue)
        return res.get("data") or {}

    def _call(self, cmd, timeout=None):
        """POST one command, wait for the result. Raises on bridge failure.

        Errors are separated by kind on purpose: a bridge that is down is
        `Unavailable` and may be retried, a command the terminal refused is
        a rejection and must not be, and an auth failure needs the operator.

        Timeout layering, which is a correctness issue and not tuning. The
        bridge holds a command open for `_timeout` seconds and then answers
        with an explicit 504 carrying the "may or may not have executed --
        reconcile by ticket" warning. The socket timeout below is set
        *above* that deliberately. If the client's timeout were the shorter
        of the two, the client would instead see its own generic socket
        timeout, lose the reconcile warning, and a lost order result would
        look like an ordinary connectivity blip rather than the
        possibly-live order it is. The specific warning has to win.
        """
        url = self.base_url + "/exec"
        body = dict(cmd or {})
        wait = float(body.pop("_timeout", DEFAULT_BRIDGE_WAIT_S))
        body["_timeout"] = wait
        payload = json.dumps(body).encode("utf-8")
        sock = float(timeout or self.timeout)
        if timeout is None and sock <= wait:
            sock = wait + DEFAULT_TIMEOUT_MARGIN_S
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Mafutani-Token", str(self.token))
        try:
            with urllib.request.urlopen(req, timeout=sock) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode()).get("error", "")
            except Exception:
                pass
            if e.code in (401, 403):
                raise BrokerAuthError(
                    "MT5 bridge refused the token (HTTP {}); check "
                    "MAFUTANI_MT5_TOKEN".format(e.code), venue=self.venue,
                    status=e.code)
            if e.code == 400:
                raise BrokerValidationError(
                    "MT5 bridge rejected the command", venue=self.venue,
                    status=e.code)
            if e.code == 504:
                # The EA never answered. The command may have executed and
                # only the result was lost, so this is neither a rejection
                # nor retryable -- retrying blind is how an account ends up
                # holding two positions where one was intended.
                raise BrokerUnavailableError(
                    detail or "MT5 terminal did not respond; the order may "
                              "or may not have executed -- reconcile by "
                              "ticket before retrying",
                    venue=self.venue, status=504, retryable=False)
            raise BrokerUnavailableError(
                "MT5 bridge HTTP {}".format(e.code), venue=self.venue,
                status=e.code, retryable=True)
        except (urllib.error.URLError, OSError) as e:
            raise BrokerUnavailableError(
                "cannot reach MT5 bridge at {}: {}".format(
                    self.base_url, e), venue=self.venue)
        try:
            res = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise BrokerUnavailableError(
                "MT5 bridge returned non-JSON", venue=self.venue)
        if not isinstance(res, dict):
            raise BrokerUnavailableError(
                "MT5 bridge returned an unexpected shape", venue=self.venue)
        return res


def build(**kw):
    """Registry entry point. Refuses plaintext to a non-loopback host."""
    host = kw.get("host") or os.environ.get("MAFUTANI_MT5_HOST", "127.0.0.1")
    if host not in BRIDGE_HOSTS_ALLOWED_PLAINTEXT:
        raise BrokerValidationError(
            "refusing plaintext MT5 bridge to non-loopback host '{}': pass a "
            "token and put the bridge behind TLS, or keep it on the same "
            "machine".format(host))
    return MT5Broker(**kw)

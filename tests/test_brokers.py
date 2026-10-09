"""Tests for the broker layer.

Covers the normalized contract (`base`), the stdlib REST transport,
profile-driven venue translation, the paper adapter, the registry, and the
interactive session's live-trading gates.

No test touches the network or needs an API key: `FakeTransport` stands
in for HTTP, and `Transport` is driven against an injected `sleep` so
retry/backoff behaviour is deterministic.
"""
import json
import os
import sys
import unittest
import urllib.error

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from brokers import base, transport, profiles, rest, registry, interactive  # noqa: E402
from brokers.base import (BrokerError, BrokerAuthError, BrokerValidationError,
                          BrokerRateLimitError, BrokerUnavailableError,
                          BrokerRejectedError, BrokerNotSupported, Credentials,
                          OrderRequest, Quote, assert_live_allowed)  # noqa: E402


# ------------------------------------------------------------- fake transport
class FakeTransport:
    """Records requests and replays canned responses.

    Stands in for `transport.Transport` so adapter tests never open a
    socket. `routes` maps "METHOD /path" -> (status, body) or a callable.
    """

    def __init__(self, routes=None, default=(200, {})):
        self.routes = dict(routes or {})
        self.default = default
        self.calls = []

    def request(self, method, url, params=None, body=None, headers=None,
                auth=None, sign=None):
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        key = "{} {}".format(method.upper(), parts.path)
        self.calls.append({
            "method": method.upper(), "url": url, "path": parts.path,
            "query": dict(params or {}), "body": body,
            "headers": dict(headers or {}),
        })
        if sign is not None:
            sign(method, url, params, body, "1700000000000")
        if auth is not None:
            auth(dict(headers or {}), dict(params or {}))
        route = self.routes.get(key, self.default)
        if callable(route):
            route = route(self)
        status, payload = route
        if status == "exception":
            raise payload
        return transport.Response(status, {}, payload, json.dumps(payload), url)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)


def env(**kw):
    """Set env vars for the duration of a test."""
    old = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: str(v) for k, v in kw.items()})
    return old


def restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ------------------------------------------------------------ credentials
class TestCredentials(unittest.TestCase):
    def test_repr_never_leaks(self):
        c = Credentials("AKIA-SECRET-KEY", "shhh", "pw")
        text = repr(c) + str(c)
        self.assertNotIn("AKIA-SECRET-KEY", text)
        self.assertNotIn("shhh", text)
        self.assertNotIn("pw", text)
        self.assertIn(c.fingerprint(), text)

    def test_fingerprint_is_stable_and_distinct(self):
        a = Credentials("k", "s")
        self.assertEqual(a.fingerprint(), Credentials("k", "s").fingerprint())
        self.assertNotEqual(a.fingerprint(), Credentials("k", "other").fingerprint())

    def test_get_accessors(self):
        c = Credentials("k", "s", "p", {"region": "eu"})
        self.assertEqual(c.get("api_key"), "k")
        self.assertEqual(c.get("secret"), "s")
        self.assertEqual(c.get("passphrase"), "p")
        self.assertEqual(c.get("region"), "eu")
        self.assertEqual(c.get("nope", "d"), "d")

    def test_bool(self):
        self.assertFalse(bool(Credentials()))
        self.assertTrue(bool(Credentials("k")))

    def test_from_env(self):
        old = env(BROKER_API_KEY="ek", BROKER_SECRET="es",
                  BROKER_EXTRA_REGION="eu")
        try:
            c = Credentials.from_env()
            self.assertEqual(c.get("api_key"), "ek")
            self.assertEqual(c.get("secret"), "es")
            self.assertEqual(c.get("region"), "eu")
        finally:
            restore(old)

    def test_from_env_returns_none_when_unset(self):
        for k in ("BROKER_API_KEY", "BROKER_SECRET", "BROKER_PASSPHRASE"):
            os.environ.pop(k, None)
        self.assertIsNone(Credentials.from_env())


# ------------------------------------------------------------ order request
class TestOrderRequest(unittest.TestCase):
    def test_validates_and_normalizes(self):
        o = OrderRequest(symbol=" aapl ", side="BUY", qty="10").validate()
        self.assertEqual(o.symbol, "AAPL")
        self.assertEqual(o.side, "buy")
        self.assertEqual(o.qty, 10.0)

    def test_rejects_bad_input(self):
        cases = [
            ({"symbol": "", "qty": 1}, "symbol"),
            ({"symbol": "AAPL", "qty": 1, "side": "up"}, "side"),
            ({"symbol": "AAPL", "qty": 1, "type": "iceberg"}, "type"),
            ({"symbol": "AAPL", "qty": 0}, "qty"),
            ({"symbol": "AAPL", "qty": -5}, "qty"),
            ({"symbol": "AAPL", "qty": "abc"}, "qty"),
            ({"symbol": "AAPL", "qty": 1, "type": "limit"}, "price"),
            ({"symbol": "AAPL", "qty": 1, "type": "stop"}, "stop_price"),
            ({"symbol": "AAPL", "qty": 1, "type": "trailing"}, "trail_pct"),
        ]
        for kwargs, needle in cases:
            with self.assertRaises(BrokerValidationError, msg=kwargs) as cm:
                OrderRequest(**kwargs).validate()
            self.assertIn(needle, str(cm.exception).lower(), kwargs)

    def test_client_order_id_is_generated_and_unique(self):
        a, b = OrderRequest(symbol="X", qty=1), OrderRequest(symbol="X", qty=1)
        self.assertTrue(a.client_order_id)
        self.assertNotEqual(a.client_order_id, b.client_order_id)

    def test_client_order_id_is_preserved(self):
        o = OrderRequest(symbol="X", qty=1, client_order_id="mine").validate()
        self.assertEqual(o.client_order_id, "mine")


# ------------------------------------------------------------ live guard
class TestLiveGuard(unittest.TestCase):
    def test_disabled_by_default(self):
        os.environ.pop("LIVE_TRADING_ENABLED", None)
        self.assertFalse(base.live_trading_enabled())
        with self.assertRaises(BrokerError):
            assert_live_allowed("v", confirm=True, tier_live_enabled=True)

    def test_all_three_gates_required(self):
        old = env(LIVE_TRADING_ENABLED="1")
        try:
            # env on, tier off -> fail
            with self.assertRaises(BrokerError):
                assert_live_allowed("v", confirm=True, tier_live_enabled=False)
            # env on, tier on, no confirm -> fail (fails closed)
            with self.assertRaises(BrokerError):
                assert_live_allowed("v", confirm=False, tier_live_enabled=True)
            # all three -> pass
            self.assertTrue(assert_live_allowed("v", confirm=True,
                                                tier_live_enabled=True))
        finally:
            restore(old)

    def test_env_value_must_be_exactly_one(self):
        for bad in ("0", "true", "yes", ""):
            old = env(LIVE_TRADING_ENABLED=bad)
            try:
                self.assertFalse(base.live_trading_enabled(), bad)
            finally:
                restore(old)


# ------------------------------------------------------------ transport
class TestTransport(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.t = transport.Transport(retries=2, backoff=0.01,
                                    sleep=self.slept.append)

    def test_rejects_non_https(self):
        for bad in ("http://x.com/a", "ftp://x.com/a"):
            with self.assertRaises(BrokerValidationError):
                self.t.get(bad)

    def test_rejects_relative_url(self):
        with self.assertRaises(BrokerValidationError):
            self.t.get("/no/scheme")

    def test_build_url_encodes_query(self):
        u = self.t._build_url("https://api.x.com/v1/o",
                              {"symbol": "A/B", "n": 2})
        self.assertIn("symbol=A%2FB", u)
        self.assertIn("n=2", u)

    def test_error_status_mapping(self):
        """Each HTTP status must map to the right typed error."""
        cases = [
            (401, BrokerAuthError), (403, BrokerAuthError),
            (429, BrokerRateLimitError), (400, BrokerValidationError),
            (422, BrokerValidationError), (500, BrokerUnavailableError),
            (503, BrokerUnavailableError), (404, BrokerError),
        ]
        for status, exc in cases:
            with self.assertRaises(exc, msg=status):
                self.t._parse(status, {}, b"{}", "https://x/y")

    def test_retry_after_is_captured(self):
        with self.assertRaises(BrokerRateLimitError) as cm:
            self.t._parse(429, {"retry-after": "12"}, b"{}", "https://x/y")
        self.assertEqual(cm.exception.retry_after, 12.0)

    def test_error_detail_extracted(self):
        for payload, needle in (
                ({"error": "insufficient margin"}, "insufficient margin"),
                ({"message": "bad symbol"}, "bad symbol"),
                ({"errors": ["a", "b"]}, "a, b"),
                ({"reason": "halted"}, "halted")):
            with self.assertRaises(BrokerError) as cm:
                self.t._parse(400, {}, json.dumps(payload).encode(), "https://x")
            self.assertIn(needle, str(cm.exception))

    def test_retry_on_5xx_then_success(self):
        """A retryable failure must be retried; the sleep must be called."""
        state = {"n": 0}

        class Flaky(transport.Transport):
            def _once(self, method, url, payload, headers):
                state["n"] += 1
                if state["n"] < 3:
                    return self._parse(503, {}, b"{}", url, http_error=True)
                return transport.Response(200, {}, {"ok": True}, "{}", url)
        f = Flaky(retries=3, backoff=0.01, sleep=self.slept.append)
        r = f.get("https://x/y")
        self.assertEqual(r.status, 200)
        self.assertEqual(state["n"], 3)
        self.assertTrue(self.slept, "backoff sleep must happen")

    def test_auth_error_never_retried(self):
        state = {"n": 0}

        class AuthFlaky(transport.Transport):
            def _once(self, method, url, payload, headers):
                state["n"] += 1
                return self._parse(401, {}, b"{}", url, http_error=True)
        f = AuthFlaky(retries=5, backoff=0.01, sleep=self.slept.append)
        with self.assertRaises(BrokerAuthError):
            f.get("https://x/y")
        self.assertEqual(state["n"], 1,
                         "retrying bad credentials risks account lockout")

    def test_rate_limit_waits_retry_after(self):
        class Limited(transport.Transport):
            def _once(self, method, url, payload, headers):
                return self._parse(429, {"retry-after": "0.05"}, b"{}", url, http_error=True)
        slept = []
        f = Limited(retries=1, backoff=0.01, sleep=slept.append)
        with self.assertRaises(BrokerRateLimitError):
            f.get("https://x/y")
        self.assertTrue(any(s >= 0.05 for s in slept), slept)

    def test_logs_are_redacted(self):
        seen = []
        t = transport.Transport(retries=0, logger=seen.append)

        class Ok(transport.Transport):
            def _once(self, method, url, payload, headers):
                return transport.Response(200, {}, {}, "", url)
        o = Ok(retries=0, logger=seen.append)
        o.get("https://u:pw@x.com/v1?api_key=TOPSECRET")
        joined = "\n".join(seen)
        self.assertNotIn("TOPSECRET", joined)
        self.assertNotIn("pw", joined)

    # -- auth styles -----------------------------------------------------
    def test_bearer_auth(self):
        h = {}
        transport.bearer_auth(Credentials("tok"))(h, {})
        self.assertEqual(h["Authorization"], "Bearer tok")

    def test_bearer_auth_requires_token(self):
        with self.assertRaises(BrokerAuthError):
            transport.bearer_auth(Credentials())({}, {})

    def test_query_auth(self):
        q = {}
        transport.query_auth(Credentials("k", "s"))({}, q)
        self.assertEqual(q["api_key"], "k")
        self.assertEqual(q["api_secret"], "s")

    def test_hmac_signer_is_deterministic(self):
        signer = transport.hmac_signer(Credentials("k", "s"))
        a = signer("POST", "https://x/v1/o", {"a": 1}, '{"b":2}', "123")
        b = signer("POST", "https://x/v1/o", {"a": 1}, '{"b":2}', "123")
        self.assertEqual(a, b)
        self.assertEqual(a["X-API-KEY"], "k")
        self.assertIn("X-SIGNATURE", a)

    def test_hmac_signature_changes_with_secret(self):
        s1 = transport.hmac_signer(Credentials("k", "one"))(
            "POST", "https://x/v1/o", {}, "", "1")
        s2 = transport.hmac_signer(Credentials("k", "two"))(
            "POST", "https://x/v1/o", {}, "", "1")
        self.assertNotEqual(s1["X-SIGNATURE"], s2["X-SIGNATURE"])


# ------------------------------------------------------------ profiles
class TestProfiles(unittest.TestCase):
    def test_get_profile_by_name(self):
        p = profiles.get_profile("BINANCE_STYLE")
        self.assertEqual(p.name, "binance_style")

    def test_unknown_profile_raises(self):
        with self.assertRaises(BrokerNotSupported):
            profiles.get_profile("nope")

    def test_endpoint_substitution(self):
        p = profiles.BEARER_STYLE
        url = p.endpoint("quote", symbol="AAPL")
        self.assertIn("/v3/instruments/AAPL/quote", url)
        self.assertTrue(url.startswith("https://"))

    def test_endpoint_without_venue_key_is_constant(self):
        p = profiles.BEARER_STYLE
        self.assertTrue(p.endpoint("account").endswith("/v3/accounts"))

    def test_missing_endpoint_raises(self):
        with self.assertRaises(BrokerNotSupported):
            profiles.BEARER_STYLE.endpoint("nonexistent")

    def test_status_maps_only_target_known_statuses(self):
        for prof in profiles.PROFILES.values():
            for raw, norm in prof.status_map.items():
                self.assertIn(norm, base.STATUSES,
                              "{}/{} -> {}".format(prof.name, raw, norm))

    def test_list_profiles(self):
        self.assertEqual(len(profiles.list_profiles()),
                         len(profiles.PROFILES))


# ------------------------------------------------------------ rest broker
class TestRestBroker(unittest.TestCase):
    def broker(self, profile="binance_style", routes=None, creds=None,
               **kw):
        return rest.RestBroker(
            profile=profile,
            credentials=creds if creds is not None else Credentials("k", "s"),
            transport=FakeTransport(routes), **kw)

    def test_reports_profile_name_and_venue(self):
        """Broker.name/venue must describe the real venue, not 'base'."""
        b = self.broker("binance_style")
        self.assertEqual(b.name, "binance_style")
        self.assertEqual(b.venue, profiles.BINANCE_STYLE.venue)
        self.assertNotEqual(b.venue, "base")

    def test_submit_order_translates_fields(self):
        t = FakeTransport({"POST /api/v3/order": (200, {
            "orderId": 99, "symbol": "BTCUSD", "status": "NEW",
            "price": "100.5", "executedQty": "0"})})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        o = b.submit_order(OrderRequest(symbol="btcusd", side="buy", qty=2))
        call = t.calls[-1]
        # our field -> venue field
        self.assertEqual(call["body"]["symbol"], "BTCUSD")
        self.assertEqual(call["body"]["side"], "BUY")
        self.assertEqual(call["body"]["quantity"], 2)
        self.assertIn("clientOrderId", call["body"])
        # venue response -> normalized
        self.assertEqual(o.order_id, "99")
        self.assertEqual(o.status, "new")
        self.assertEqual(o.price, 100.5)
        self.assertEqual(o.filled_qty, 0.0)
        self.assertEqual(o.venue, b.profile.venue)

    def test_status_dialects_all_normalize(self):
        """Every venue status dialect must land on a normalized status.

        Exercised through `get_order` rather than `submit_order`, because a
        REJECTED status is *supposed* to raise on submit (see
        test_rejected_order_raises).
        """
        cases = [("PARTIALLY_FILLED", "partial"), ("FILLED", "filled"),
                 ("CANCELED", "cancelled"), ("EXPIRED", "expired"),
                 ("REJECTED", "rejected"), ("NEW", "new")]
        for raw, want in cases:
            t = FakeTransport({"GET /api/v3/order": (200, {"status": raw})})
            b = rest.RestBroker(profile="binance_style",
                                credentials=Credentials("k", "s"), transport=t)
            o = b.get_order(1)
            self.assertEqual(o.status, want, raw)
            self.assertIn(o.status, base.STATUSES)

    def test_numeric_status_codes_normalize(self):
        t = FakeTransport({"GET /rest/orders/open": (200, [
            {"order_status": "2", "instrument": "X", "net_position": "0"}])})
        b = rest.RestBroker(profile="exchange_style",
                            credentials=Credentials("k", "s"), transport=t)
        orders = b.list_orders()
        self.assertEqual(orders[0].status, "partial")

    def test_rejected_order_raises(self):
        t = FakeTransport({"POST /api/v3/order": (200, {
            "status": "REJECTED", "error": "insufficient margin"})})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        with self.assertRaises(BrokerRejectedError) as cm:
            b.submit_order(OrderRequest(symbol="X", qty=1))
        self.assertIn("insufficient margin", str(cm.exception))

    def test_dict_order_input_accepted(self):
        t = FakeTransport({"POST /api/v3/order": (200, {"status": "NEW"})})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        o = b.submit_order({"symbol": "X", "side": "sell", "qty": 3})
        self.assertEqual(o.side, "sell")

    def test_invalid_order_never_reaches_network(self):
        t = FakeTransport()
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        with self.assertRaises(BrokerValidationError):
            b.submit_order(OrderRequest(symbol="X", qty=-1))
        self.assertEqual(t.calls, [], "must validate before any HTTP call")

    def test_non_live_adapter_refuses_live_flagged_request(self):
        t = FakeTransport({"POST /api/v3/order": (200, {"status": "NEW"})})
        b = rest.RestBroker(profile="binance_style", live=False,
                            credentials=Credentials("k", "s"), transport=t)
        req = OrderRequest(symbol="X", qty=1, metadata={"live": True})
        with self.assertRaises(BrokerError):
            b.submit_order(req)
        self.assertEqual(t.calls, [],
                         "a non-live adapter must not simulate a 'live' order")

    def test_live_adapter_blocked_when_env_off(self):
        t = FakeTransport({"POST /api/v3/order": (200, {"status": "NEW"})})
        os.environ.pop("LIVE_TRADING_ENABLED", None)
        b = rest.RestBroker(profile="binance_style", live=True, confirm=True,
                            credentials=Credentials("k", "s"), transport=t)
        with self.assertRaises(BrokerError):
            b.submit_order(OrderRequest(symbol="X", qty=1))
        self.assertEqual(t.calls, [])

    def test_live_adapter_sends_when_all_gates_pass(self):
        t = FakeTransport({"POST /api/v3/order": (200, {"status": "NEW"})})
        old = env(LIVE_TRADING_ENABLED="1")
        try:
            b = rest.RestBroker(profile="binance_style", live=True,
                                confirm=True, tier_live_enabled=True,
                                credentials=Credentials("k", "s"), transport=t)
            o = b.submit_order(OrderRequest(symbol="X", qty=1))
            self.assertEqual(o.status, "new")
            self.assertEqual(len(t.calls), 1)
        finally:
            restore(old)

    def test_live_adapter_without_confirmation_blocked(self):
        t = FakeTransport({"POST /api/v3/order": (200, {"status": "NEW"})})
        old = env(LIVE_TRADING_ENABLED="1")
        try:
            b = rest.RestBroker(profile="binance_style", live=True,
                                confirm=False, tier_live_enabled=True,
                                credentials=Credentials("k", "s"), transport=t)
            with self.assertRaises(BrokerError):
                b.submit_order(OrderRequest(symbol="X", qty=1))
        finally:
            restore(old)

    def test_quote_parsing(self):
        t = FakeTransport({"GET /api/v3/ticker/bookTicker": (200, {
            "symbol": "BTCUSD", "bidPrice": "99.5", "askPrice": "100.5",
            "lastPrice": "100.0"})})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        q = b.quote("BTCUSD")
        self.assertEqual(q.bid, 99.5)
        self.assertEqual(q.ask, 100.5)
        self.assertEqual(q.mid, 100.0)
        self.assertEqual(q.spread, 1.0)

    def test_quote_handles_wrapped_payload(self):
        t = FakeTransport({"GET /v3/instruments/X/quote": (200, {
            "data": {"instrument": "X", "bid": "1.0", "ask": "2.0"}})})
        b = rest.RestBroker(profile="bearer_style",
                            credentials=Credentials("k"), transport=t)
        q = b.quote("X")
        self.assertEqual(q.bid, 1.0)
        self.assertEqual(q.ask, 2.0)

    def test_quote_tolerates_garbage_numbers(self):
        t = FakeTransport({"GET /api/v3/ticker/bookTicker": (200, {
            "symbol": "X", "bidPrice": "n/a", "askPrice": None,
            "lastPrice": ""})})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        q = b.quote("X")
        self.assertEqual(q.bid, 0.0)
        self.assertEqual(q.ask, 0.0)

    def test_positions_skip_flat(self):
        t = FakeTransport({"GET /api/v3/positionRisk": (200, [
            {"symbol": "BTCUSD", "positionAmt": "0.5", "entryPrice": "100"},
            {"symbol": "ETHUSD", "positionAmt": "0"},
            {"symbol": "XRPUSD", "positionAmt": "-100", "entryPrice": "2"}])})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        pos = b.positions()
        self.assertEqual(len(pos), 2, "flat positions must be omitted")
        self.assertTrue(pos[0].is_long)
        self.assertTrue(pos[1].is_short)

    def test_account_parsing(self):
        t = FakeTransport({"GET /api/v3/account": (200, {
            "freeBalance": "1000.5", "totalWalletBalance": "2500.0",
            "quoteAsset": "USDT"})})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        a = b.account()
        self.assertEqual(a.cash, 1000.5)
        self.assertEqual(a.equity, 2500.0)
        self.assertEqual(a.currency, "USDT")

    def test_cancel(self):
        t = FakeTransport({"DELETE /api/v3/order": (200, {"orderId": 7})})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        o = b.cancel_order(7)
        self.assertEqual(o.order_id, "7")
        self.assertEqual(o.status, "cancelled")

    def test_default_params_merged(self):
        t = FakeTransport({"GET /api/v3/account": (200, {})})
        b = rest.RestBroker(profile="binance_style",
                            credentials=Credentials("k", "s"), transport=t)
        b.account()
        self.assertEqual(t.calls[-1]["query"].get("recvWindow"), 5000)

    def test_unsupported_capability_raises_not_supported(self):
        prof = profiles.VenueProfile(
            name="bare", venue="Bare", base_url="https://x.example.com",
            endpoints={"order": "/o"}, auth_style="bearer")
        t = FakeTransport()
        b = rest.RestBroker(profile=prof, credentials=Credentials("k"),
                            transport=t)
        with self.assertRaises(BrokerNotSupported):
            b.symbols()
        with self.assertRaises(BrokerNotSupported):
            b.candles("X")


# ------------------------------------------------------------ registry
class TestRegistry(unittest.TestCase):
    def test_paper_refuses_live_construction(self):
        """A simulated venue must never be constructible as live."""
        with self.assertRaises(BrokerValidationError):
            registry.get_broker("paper", user_id=1, live=True)

    def test_paper_is_registered(self):
        self.assertIn("paper", registry.available())

    def test_all_profiles_registered(self):
        for name in profiles.PROFILES:
            self.assertIn(name, registry.available())

    def test_named_profile_is_actually_used(self):
        """Regression: the registry name must reach the builder.

        Without the bound builder every REST broker constructed the
        default profile, so `binance_style` silently returned a
        `bearer_style` adapter pointed at the wrong venue shape.
        """
        for name in profiles.PROFILES:
            b = registry.get_broker(name)
            self.assertEqual(b.profile.name, name)
            self.assertEqual(b.venue, profiles.get_profile(name).venue)

    def test_unknown_broker_raises(self):
        with self.assertRaises(BrokerNotSupported):
            registry.get_broker("nope")

    def test_get_paper_broker(self):
        b = registry.get_broker("paper", user_id=1)
        self.assertIsInstance(b, registry.PaperBroker)
        self.assertFalse(b.is_live)

    def test_describe(self):
        d = registry.describe()
        self.assertTrue(any(x["name"] == "paper" for x in d))

    def test_custom_registration(self):
        registry.register("dummy", lambda **kw: "made", "test broker")
        try:
            self.assertIn("dummy", registry.available())
            self.assertEqual(registry.get_broker("dummy"), "made")
        finally:
            registry._REGISTRY.pop("dummy", None)


# ------------------------------------------------------------ session
class TestBrokerSession(unittest.TestCase):
    def session(self, broker):
        self.events = []
        return interactive.BrokerSession(
            broker, on_event=self.events.append,
            clock=lambda: 1_000_000.0)

    def test_help_lists_commands(self):
        s = self.session(None)
        out = s.handle("help")
        for cmd in ("status", "quote", "buy", "arm", "use"):
            self.assertIn(cmd, out)

    def test_unknown_command(self):
        s = self.session(None)
        self.assertIn("unknown command", s.handle("frobnicate"))

    def test_blank_input(self):
        s = self.session(None)
        self.assertEqual(s.handle(""), "")
        self.assertEqual(s.handle("   "), "")

    def test_command_without_broker(self):
        s = self.session(None)
        self.assertIn("no broker", s.handle("status"))
        self.assertIn("invalid", s.handle("quote AAPL"))

    def test_bad_syntax_does_not_raise(self):
        s = self.session(None)
        self.assertIn("could not parse", s.handle('buy "unclosed'))

    def test_use_lists_brokers(self):
        s = self.session(None)
        out = s.handle("brokers")
        self.assertIn("paper", out)

    def test_use_switches_and_disarms(self):
        s = self.session(None)
        s._armed_until = 1_000_001.0
        s.handle("use paper")
        self.assertFalse(s.armed, "switching broker must disarm")

    def test_status_reports_health(self):
        s = self.session(registry.get_broker("paper", user_id=1))
        out = s.handle("status")
        self.assertIn("live:", out)
        self.assertIn("armed:", out)

    def test_credentials_not_accepted_as_argument(self):
        """Passing a secret on the command line would leak it to history."""
        s = self.session(registry.get_broker("paper", user_id=1))
        s.handle("use paper --api-key=SUPERSECRET")
        self.assertNotIn("SUPERSECRET", "\n".join(s.history))
        self.assertNotIn("SUPERSECRET", json.dumps(self.events, default=str))

    def test_arm_requires_yes(self):
        b = registry.get_broker("paper", user_id=1)
        s = self.session(b)
        out = s.handle("arm")
        self.assertIn("usage: arm yes", out)
        self.assertFalse(s.armed)

    def test_arm_requires_env(self):
        b = registry.get_broker("paper", user_id=1)
        b.is_live = True
        s = self.session(b)
        os.environ.pop("LIVE_TRADING_ENABLED", None)
        out = s.handle("arm yes")
        self.assertIn("LIVE_TRADING_ENABLED", out)
        self.assertFalse(s.armed)

    def test_arm_requires_live_broker(self):
        b = registry.get_broker("paper", user_id=1)
        s = self.session(b)
        old = env(LIVE_TRADING_ENABLED="1")
        try:
            out = s.handle("arm yes")
            self.assertIn("not live", out)
            self.assertFalse(s.armed)
        finally:
            restore(old)

    def test_arm_then_expire(self):
        b = registry.get_broker("paper", user_id=1)
        b.is_live = True
        old = env(LIVE_TRADING_ENABLED="1")
        now = [1000.0]
        events = []
        s = interactive.BrokerSession(b, on_event=events.append,
                                      clock=lambda: now[0])
        try:
            self.assertIn("armed", s.handle("arm yes"))
            self.assertTrue(s.armed)
            now[0] += interactive.ARM_TTL_SECONDS + 1
            self.assertFalse(s.armed, "arming must expire")
            self.assertIn("not armed", s.handle("buy AAPL 1"))
        finally:
            restore(old)

    def test_disarm(self):
        b = registry.get_broker("paper", user_id=1)
        b.is_live = True
        old = env(LIVE_TRADING_ENABLED="1")
        try:
            s = self.session(b)
            s.handle("arm yes")
            s.handle("disarm")
            self.assertFalse(s.armed)
        finally:
            restore(old)

    def test_live_buy_requires_armed(self):
        b = registry.get_broker("paper", user_id=1)
        b.is_live = True
        s = self.session(b)
        out = s.handle("buy AAPL 1")
        self.assertIn("not armed", out)

    def test_broker_errors_are_reported_not_raised(self):
        class Boom(registry.Broker):
            name = "boom"
            def quote(self, symbol):
                raise BrokerUnavailableError("venue down", "v", 503)
        s = self.session(Boom())
        out = s.handle("quote AAPL")
        self.assertIn("venue down", out)
        self.assertTrue(any(e["kind"] == "broker.error" for e in self.events))

    def test_unexpected_exception_is_contained(self):
        class Bad(registry.Broker):
            name = "bad"
            def quote(self, symbol):
                raise ZeroDivisionError("boom")
        s = self.session(Bad())
        out = s.handle("quote AAPL")
        self.assertIn("ZeroDivisionError", out)
        self.assertTrue(any(e["kind"] == "broker.exception" for e in self.events))


# ------------------------------------------------------------ paper broker
class TestPaperBroker(unittest.TestCase):
    def setUp(self):
        import sqlite3
        fd, self.path = __import__("tempfile").mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql")) as fh:
            self.conn.executescript(fh.read())
        self.conn.execute(
            "INSERT INTO users (email, password_hash, tier)"
            " VALUES ('p@local','x','Pro')")
        self.conn.commit()
        self.uid = self.conn.execute(
            "SELECT id FROM users WHERE email='p@local'").fetchone()[0]
        now = int(__import__("time").time())
        for i in range(5):
            self.conn.execute(
                "INSERT INTO candles_1m (symbol, ts, open, high, low, close, volume)"
                " VALUES ('AAPL', ?, 100, 101, 99, 100.5, 1000)",
                (now - (5 - i) * 60,))
        self.conn.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size, last)"
            " VALUES ('AAPL', ?, 100.4, 100.6, 10, 10, 100.5)", (now,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def broker(self):
        return registry.get_broker("paper", conn=self.conn, user_id=self.uid)

    def test_paper_is_never_live(self):
        b = self.broker()
        self.assertFalse(b.is_live)
        b.is_live = True
        b2 = self.broker()
        self.assertFalse(b2.is_live, "paper broker must never report live")

    def test_connect_requires_real_user(self):
        b = registry.get_broker("paper", conn=self.conn, user_id=99999)
        with self.assertRaises(BrokerValidationError):
            b.connect()

    def test_quote(self):
        q = self.broker().quote("AAPL")
        self.assertEqual(q.bid, 100.4)
        self.assertEqual(q.ask, 100.6)
        self.assertEqual(q.mid, 100.5)

    def test_quote_unknown_symbol(self):
        with self.assertRaises(BrokerNotSupported):
            self.broker().quote("NOPE")

    def test_candles_ascending(self):
        rows = self.broker().candles("AAPL", limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["ts"] for r in rows],
                         sorted(r["ts"] for r in rows))

    def test_candles_rejects_other_timeframe(self):
        with self.assertRaises(BrokerNotSupported):
            self.broker().candles("AAPL", timeframe="1h")

    def test_positions_and_orders_start_empty(self):
        b = self.broker()
        self.assertEqual(b.positions(), [])
        self.assertEqual(b.list_orders(), [])

    def test_submit_validates(self):
        b = self.broker()
        with self.assertRaises(BrokerValidationError):
            b.submit_order(OrderRequest(symbol="AAPL", qty=0))

    def test_cancel_unknown_order(self):
        with self.assertRaises(BrokerNotSupported):
            self.broker().cancel_order(4242)

    def test_symbols_lists_universe(self):
        syms = self.broker().symbols()
        self.assertIn("AAPL", syms)

    def test_health(self):
        h = self.broker().health()
        self.assertEqual(h["name"], "paper")
        self.assertFalse(h["live"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

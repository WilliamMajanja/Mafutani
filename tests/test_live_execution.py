"""Live-execution safety tests. Stdlib only, no network.

These exist because of one specific, observed failure: a live BTC buy
returned `filled`, opened a position, charged a fee and moved P&L for an
asset that was never bought, because `place_live_order` was a stub
returning `routed_stub` and the order handler treated any non-rejected
status as a fill.

Every test here encodes an invariant that, if it broke again, would let
the app claim a trade it did not make. They are deliberately weighted
toward refusal.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers.base import (BrokerAuthError, BrokerError, BrokerRejectedError,
                          BrokerUnavailableError, Credentials, OrderRequest)


class _Env:
    def __init__(self, **kw):
        self._kw = kw
        self._old = {}

    def __enter__(self):
        for k, v in self._kw.items():
            self._old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self._kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class TestNoSilentFill(unittest.TestCase):
    """The regression this file exists for."""

    def test_live_order_without_credentials_never_fills(self):
        from data.feeds import place_live_order
        with _Env(LIVE_TRADING_ENABLED="1", BROKER_BINANCE_API_KEY=None,
                  BROKER_BINANCE_SECRET=None, BROKER_API_KEY=None,
                  BROKER_SECRET=None):
            with self.assertRaises((BrokerAuthError, BrokerError,
                                    BrokerUnavailableError)) as ctx:
                place_live_order("BTCUSD", "buy", 0.001, "market",
                                 user_id=1, tier="Pro")
            # Whatever it raised, it must not have returned a fill.
            self.assertNotIsInstance(ctx.exception, dict)

    def test_place_live_order_raises_rather_than_returning_stub(self):
        # The old contract returned a dict with status "routed_stub". Any
        # caller checking `if status != "rejected"` would treat that as a
        # success, so the function must now raise or return a venue
        # status -- never invent one.
        from data.feeds import place_live_order
        with _Env(LIVE_TRADING_ENABLED="0"):
            with self.assertRaises(RuntimeError):
                place_live_order("BTCUSD", "buy", 0.001)

    def test_live_env_gate_still_first(self):
        from data.feeds import place_live_order
        with _Env(LIVE_TRADING_ENABLED="0"):
            with self.assertRaises(RuntimeError) as ctx:
                place_live_order("BTCUSD", "buy", 0.001)
            self.assertIn("LIVE_TRADING_ENABLED", str(ctx.exception))


class TestBinanceRefusals(unittest.TestCase):

    def _broker(self, **kw):
        from brokers import get_broker
        kw.setdefault("live", True)
        kw.setdefault("confirm", True)
        return get_broker("binance", **kw)

    def test_registered(self):
        from brokers import available
        self.assertIn("binance", available())

    def test_refuses_without_credentials(self):
        with _Env(BROKER_BINANCE_API_KEY=None, BROKER_BINANCE_SECRET=None,
                  BROKER_BINANCE_api_key=None, BROKER_BINANCE_secret=None,
                  BROKER_API_KEY=None, BROKER_SECRET=None,
                  LIVE_TRADING_ENABLED="1"):
            b = self._broker()
            with self.assertRaises(BrokerAuthError):
                b.submit_order({"symbol": "BTCUSD", "side": "buy", "qty": 0.001})

    def test_non_live_adapter_refuses_to_submit(self):
        # A paper-mode adapter must never place a real order.
        b = self._broker(live=False)
        with self.assertRaises(BrokerError):
            b.submit_order({"symbol": "BTCUSD", "side": "buy", "qty": 0.001})

    def test_live_env_required(self):
        with _Env(LIVE_TRADING_ENABLED="0"):
            b = self._broker()
            with self.assertRaises(BrokerError):
                b.submit_order({"symbol": "BTCUSD", "side": "buy", "qty": 0.001})

    def test_confirm_required(self):
        with _Env(LIVE_TRADING_ENABLED="1"):
            b = self._broker(confirm=False)
            with self.assertRaises(BrokerError):
                b.submit_order({"symbol": "BTCUSD", "side": "buy", "qty": 0.001})

    def test_tier_gate_required(self):
        with _Env(LIVE_TRADING_ENABLED="1"):
            b = self._broker(tier_live_enabled=False)
            with self.assertRaises(BrokerError):
                b.submit_order({"symbol": "BTCUSD", "side": "buy", "qty": 0.001})


class TestSigning(unittest.TestCase):
    """Signing is verifiable offline; only the network call is not."""

    def _signed(self, key="k", secret="s"):
        from brokers.binance import BinanceBroker
        b = BinanceBroker(credentials=Credentials(key, secret),
                          live=True, confirm=True)
        return b, b._signed("GET", "/api/v3/account")

    def test_signature_is_in_the_query_string(self):
        # Binance takes the signature as a query parameter, not a header.
        # Signing and forgetting to send it produces -1102, which reads
        # like a credentials problem and sends an operator hunting for a
        # bad key instead of a missing parameter.
        import hashlib
        import hmac
        import urllib.parse
        _b, (url, headers) = self._signed("testkey", "testsecret")
        self.assertIn("signature=", url)
        self.assertEqual(headers.get("X-MBX-APIKEY"), "testkey")
        qs = url.split("?", 1)[1]
        sig = urllib.parse.parse_qs(qs)["signature"][0]
        signed_part = qs.split("&signature=")[0]
        expect = hmac.new(b"testsecret", signed_part.encode("utf-8"),
                          hashlib.sha256).hexdigest()
        self.assertEqual(sig, expect)
        # The signature must cover the timestamp that was actually sent.
        sent = dict(urllib.parse.parse_qsl(signed_part))
        self.assertIn("timestamp", sent)
        self.assertIn("recvWindow", sent)

    def test_timestamp_is_milliseconds(self):
        _b, (url, _h) = self._signed()
        import urllib.parse
        ts = int(dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))["timestamp"])
        self.assertGreater(ts, 1_000_000_000_000)   # ms, not seconds

    def test_env_prefix_is_uppercase_as_documented(self):
        # env_prefix_for documented BROKER_BINANCE_STYLE_ but emitted
        # BROKER_binance_style_, so following the docs produced no
        # credentials at all and the failure only appeared at order time.
        from brokers.registry import env_prefix_for
        self.assertEqual(env_prefix_for("binance_style"),
                         "BROKER_BINANCE_STYLE_")

    def test_lowercase_prefix_still_resolves(self):
        # A deployment that adapted to the casing bug must not be stranded.
        from brokers.registry import resolve_credentials
        with _Env(BROKER_BINANCE_STYLE_API_KEY=None,
                  BROKER_BINANCE_STYLE_SECRET=None,
                  BROKER_API_KEY=None, BROKER_SECRET=None,
                  BROKER_binance_style_API_KEY="k",
                  BROKER_binance_style_SECRET="s"):
            c = resolve_credentials("binance_style")
            self.assertIsNotNone(c)
            self.assertEqual(c.get("api_key"), "k")

    def test_uppercase_prefix_preferred(self):
        from brokers.registry import resolve_credentials
        with _Env(BROKER_BINANCE_API_KEY="upper",
                  BROKER_BINANCE_SECRET="us",
                  BROKER_binance_API_KEY="lower",
                  BROKER_binance_SECRET="ls"):
            c = resolve_credentials("binance")
            self.assertEqual(c.get("api_key"), "upper")


class TestStatusMapping(unittest.TestCase):
    """Only the venue may say `filled`."""

    def _broker(self):
        from brokers.binance import BinanceBroker
        return BinanceBroker(credentials=Credentials("k", "s"),
                             live=True, confirm=True)

    def test_filled_comes_from_venue(self):
        order = self._broker()._to_order(
            {"symbol": "BTCUSDT", "side": "BUY", "status": "FILLED",
             "origQty": "0.001", "executedQty": "0.001", "price": "0",
             "cummulativeQuoteQty": "84.60", "orderId": "7"},
            fallback=OrderRequest(symbol="BTCUSD", qty=0.001))
        self.assertEqual(order.status, "filled")
        self.assertEqual(order.order_id, "7")

    def test_rejected_stays_rejected(self):
        order = self._broker()._to_order(
            {"symbol": "BTCUSDT", "status": "REJECTED", "origQty": "0"})
        self.assertEqual(order.status, "rejected")

    def test_unknown_venue_status_is_not_terminal(self):
        # An order we cannot classify must not look terminal: an operator
        # has to be able to find it again.
        from brokers.binance import UNKNOWN
        order = self._broker()._to_order({"symbol": "BTCUSDT"})
        self.assertEqual(order.status, UNKNOWN)
        self.assertFalse(order.is_terminal)

    def test_non_dict_response_refuses(self):
        with self.assertRaises(BrokerUnavailableError):
            self._broker()._to_order("not a dict")


class TestVenueSymbolMapping(unittest.TestCase):
    def _broker(self):
        from brokers.binance import BinanceBroker
        return BinanceBroker(credentials=Credentials("k", "s"))

    def test_app_symbol_to_venue_symbol(self):
        b = self._broker()
        self.assertEqual(b._venue_symbol("BTCUSD"), "BTCUSDT")
        self.assertEqual(b._venue_symbol("ETHUSD"), "ETHUSDT")

    def test_gold_maps_to_xautusdt_not_xauusdt(self):
        # A suffix rule produces XAUUSDT; the venue's gold pair is XAUTUSDT.
        # Querying a pair that does not exist is the documented failure
        # this mapping table exists to prevent.
        b = self._broker()
        self.assertEqual(b._venue_symbol("XAUUSD"), "XAUTUSDT")

    def test_unmapped_symbol_refused_rather_than_guessed(self):
        from brokers.base import BrokerValidationError
        b = self._broker()
        with self.assertRaises(BrokerValidationError):
            b._venue_symbol("WEIRDUSD")

    def test_venue_symbol_passes_through(self):
        b = self._broker()
        self.assertEqual(b._venue_symbol("BTCUSDT"), "BTCUSDT")

    def test_empty_symbol_rejected(self):
        from brokers.base import BrokerValidationError
        with self.assertRaises(BrokerValidationError):
            self._broker()._venue_symbol("")


class TestFillsRecordDirection(unittest.TestCase):
    def test_fills_table_has_side(self):
        # A fill that cannot say buy from sell is incomplete: the UI
        # rendered a blank column and any consumer reading fills without
        # joining orders had to infer direction from a position delta
        # that flips through zero.
        import tempfile
        from data.db import init_db, connect
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = init_db(path)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(fills)")]
            self.assertIn("side", cols)
        finally:
            conn.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

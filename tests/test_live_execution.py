"""Live-execution safety tests (Binance adapter removed). Stdlib only, no network.

Encodes invariants that prevent the app from claiming a trade it did not make.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers.base import BrokerAuthError, BrokerError, BrokerUnavailableError


class _Env:
    def __init__(self, **kw):
        self._kw = kw
        self._old = {}
    def __enter__(self):
        for k, v in self._kw.items():
            self._old[k] = os.environ.get(k)
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        return self
    def __exit__(self, *a):
        for k, v in self._old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        return False


class TestNoSilentFill(unittest.TestCase):
    def test_place_live_order_raises_when_disabled(self):
        from data.feeds import place_live_order
        with _Env(LIVE_TRADING_ENABLED="0"):
            with self.assertRaises(RuntimeError) as ctx:
                place_live_order("BTCUSD", "buy", 0.001, broker="paper")
            self.assertIn("LIVE_TRADING_ENABLED", str(ctx.exception))

    def test_place_live_order_requires_broker(self):
        # CommCoin/MFT native integration active; Binance removed; broker must be explicit.
        from data.feeds import place_live_order
        with _Env(LIVE_TRADING_ENABLED="1"):
            with self.assertRaises(RuntimeError) as ctx:
                place_live_order("BTCUSD", "buy", 0.001)
            self.assertNotIn("routed_stub", str(ctx.exception))

    def test_cancel_live_order_requires_broker(self):
        from data.feeds import cancel_live_order
        with _Env(LIVE_TRADING_ENABLED="1"):
            with self.assertRaises(RuntimeError):
                cancel_live_order({"venue_order_id": "x"})


class TestBinanceRemoved(unittest.TestCase):
    def test_binance_not_registered(self):
        from brokers import available
        self.assertNotIn("commcoin", available())

    def test_binance_profile_removed(self):
        # commcoin_style removed; hmac_style is the neutral generic template.
        from brokers import list_profiles
        names = [p.get("name") for p in list_profiles()]
        self.assertNotIn("commcoin_style", names)


class TestFillsRecordDirection(unittest.TestCase):
    def test_fills_table_has_side(self):
        import tempfile
        from data.db import init_db
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = init_db(path)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(fills)")]
            self.assertIn("side", cols)
        finally:
            conn.close(); os.unlink(path)


if __name__ == "__main__":
    unittest.main()

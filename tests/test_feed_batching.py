"""Tests for the feed loop's real-quote batching.

The feed writes a tick for every enabled symbol on an interval. When a
routed crypto symbol is present it wants a real venue book, but a venue
call is network I/O: doing it per symbol inside the write loop made one
tick's wall clock the sum of every venue's latency, so a hanging venue
stalled the whole feed -- equities included.

_real_quotes() fetches the routed set concurrently under one shared
deadline and returns only what succeeded. These tests use a fake poller
that sleeps or fails, so nothing here touches the network.
"""
import os
import sys
import time
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

import app  # noqa: E402
from data import cryptomarkets as cm  # noqa: E402


class FakePoller:
    """Stands in for data.feeds.CryptoPoller.

    `behaviour` maps symbol -> float (seconds to sleep) or an exception to
    raise. Records the high-water mark of concurrent in-flight calls so a
    test can prove the batch really did run in parallel.
    """

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.active = 0
        self.peak = 0
        self.asked = []

    def __call__(self, symbols, timeout=8):
        return self

    def fetch_one(self, symbol):
        self.asked.append(symbol)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            spec = self.behaviour.get(symbol, {})
            if isinstance(spec, Exception):
                raise spec
            delay = spec.get("delay", 0) if isinstance(spec, dict) else 0
            if delay:
                time.sleep(delay)
            if isinstance(spec, dict) and spec.get("error"):
                return {"symbol": symbol, "error": str(spec["error"])}
            if isinstance(spec, dict) and spec.get("zero"):
                return {"symbol": symbol, "bid": 0, "ask": 0, "source": "x"}
            return {"symbol": symbol, "bid": 100.0, "ask": 100.1,
                    "source": spec.get("source", "fake") if isinstance(spec, dict) else "fake"}
        finally:
            self.active -= 1


def with_poller(fake, fn):
    """Run fn with CryptoPoller replaced everywhere feeds/app import it."""
    import data.feeds as feeds
    orig = feeds.CryptoPoller
    feeds.CryptoPoller = fake
    try:
        return fn()
    finally:
        feeds.CryptoPoller = orig


ROUTED = [s for s in cm.ROUTES]


class TestBatching(unittest.TestCase):
    def test_returns_quote_per_routed_symbol(self):
        out = with_poller(FakePoller({}), lambda: app._real_quotes(ROUTED))
        self.assertEqual(sorted(out), sorted(ROUTED))

    def test_unrouted_symbols_are_not_fetched(self):
        fake = FakePoller({})
        out = with_poller(fake, lambda: app._real_quotes(["AAPL", "USDT"]))
        # AAPL has no venue route, so it must never reach a poller --
        # an equity has no business being sent to a crypto venue.
        self.assertEqual(fake.asked, ["USDT"])
        self.assertEqual(sorted(out), ["USDT"])

    def test_no_routed_symbols_returns_empty_without_polling(self):
        fake = FakePoller({})
        out = with_poller(fake, lambda: app._real_quotes(["AAPL", "EURUSD"]))
        self.assertEqual(out, {})
        self.assertEqual(fake.peak, 0)

    def test_fetches_run_concurrently(self):
        # The regression: sequential fetches made a tick's wall clock the
        # sum of venue latencies. Five 0.2s symbols must not take 1s.
        delay = 0.2
        fake = FakePoller({s: {"delay": delay} for s in ROUTED})
        start = time.time()
        out = with_poller(fake, lambda: app._real_quotes(ROUTED))
        elapsed = time.time() - start
        self.assertEqual(sorted(out), sorted(ROUTED))
        self.assertGreater(fake.peak, 1, "fetches did not overlap")
        # Generous ceiling: sequential would be len(ROUTED) * delay.
        self.assertLess(elapsed, delay * len(ROUTED) * 0.75,
                        "batch took %.2fs; looks sequential" % elapsed)


class TestFailureIsolation(unittest.TestCase):
    def test_one_failing_symbol_does_not_lose_the_others(self):
        fake = FakePoller({"USDT": IOError("venue down")})
        out = with_poller(fake, lambda: app._real_quotes(ROUTED))
        self.assertNotIn("USDT", out)
        for sym in ROUTED:
            if sym != "USDT":
                self.assertIn(sym, out)

    def test_venue_error_payload_is_treated_as_failure(self):
        fake = FakePoller({"BTCUSD": {"error": "Invalid symbol."}})
        out = with_poller(fake, lambda: app._real_quotes(ROUTED))
        self.assertNotIn("BTCUSD", out)
        self.assertIn("USDT", out)

    def test_zero_book_is_rejected(self):
        # A 0.00 quote must never reach the table.
        fake = FakePoller({"BTCUSD": {"zero": True}})
        out = with_poller(fake, lambda: app._real_quotes(ROUTED))
        self.assertNotIn("BTCUSD", out)

    def test_source_is_preserved(self):
        fake = FakePoller({s: {"source": "commcoin"} for s in ROUTED})
        out = with_poller(fake, lambda: app._real_quotes(ROUTED))
        for sym in ROUTED:
            self.assertEqual(out[sym]["source"], "commcoin")

    def test_all_failing_returns_empty_not_raises(self):
        fake = FakePoller({s: IOError("down") for s in ROUTED})
        out = with_poller(fake, lambda: app._real_quotes(ROUTED))
        self.assertEqual(out, {})


class TestDeadline(unittest.TestCase):
    def test_batch_is_bounded_by_one_deadline(self):
        # Every symbol slower than the deadline: the batch must return
        # within roughly the deadline, not timeout x len(symbols).
        timeout = 0.4
        fake = FakePoller({s: {"delay": timeout * 3} for s in ROUTED})
        start = time.time()
        out = with_poller(
            fake, lambda: app._real_quotes(ROUTED, timeout=timeout))
        elapsed = time.time() - start
        self.assertEqual(out, {})
        # Allow slack for thread teardown, but far below the serial case.
        self.assertLess(elapsed, timeout * 2.0,
                        "batch exceeded its deadline (%.2fs)" % elapsed)

    def test_slow_symbol_does_not_cost_a_retry_loop(self):
        # A symbol that misses the deadline is dropped, not re-requested.
        timeout = 0.3
        fake = FakePoller({})
        for sym in ROUTED:
            fake.behaviour[sym] = {"delay": timeout * 4}
        with_poller(fake, lambda: app._real_quotes(ROUTED, timeout=timeout))
        self.assertEqual(len(fake.asked), len(set(fake.asked)),
                         "a symbol was fetched more than once")


if __name__ == "__main__":
    unittest.main()

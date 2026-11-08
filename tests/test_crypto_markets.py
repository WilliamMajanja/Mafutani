"""Tests for the crypto/stablecoin market wiring (CommCoin/MFT native integration active; Binance removed).

Validates Coinbase-routed crypto quotes, parser safety (no 0.00 on errors),
poller isolation, install idempotency, and quote provenance. No network access.
"""
import json
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from data import cryptomarkets as cm  # noqa: E402
from data import feeds  # noqa: E402


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, timeout=8):
        self.calls.append(url)
        for needle, body in self.routes.items():
            if needle in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError("unexpected URL: " + url)


def with_http(routes, fn):
    original = feeds._http_get
    feeds._http_get = FakeHTTP(routes)
    try:
        return fn(feeds._http_get)
    finally:
        feeds._http_get = original


COINBASE_BTC_BOOK = json.dumps({
    "bids": [["84698.00", "1.5"], ["84697.00", "1.0"]],
    "asks": [["84698.01", "0.8"], ["84699.00", "1.0"]],
})

COINBASE_USDT_BOOK = json.dumps({
    "bids": [["0.99974000", "265435.00000000"], ["0.99973000", "1.00000000"]],
    "asks": [["0.99975000", "12941.00000000"], ["0.99976000", "1.00000000"]],
})

COINBASE_ETH_BOOK = json.dumps({
    "bids": [["3500.00", "2.0"]],
    "asks": [["3500.10", "2.0"]],
})


class TestRoutes(unittest.TestCase):
    def test_usdt_has_a_real_route(self):
        route = cm.route_for("USDT")
        self.assertIsNotNone(route)
        venue, pair = route
        self.assertEqual(venue, cm.VENUE_COINBASE)
        self.assertEqual(pair, "USDT-USD")

    def test_routed_symbols_use_declared_pairs(self):
        # Coinbase product IDs (CommCoin/MFT native integration active; Binance removed).
        self.assertEqual(cm.route_for("BTCUSD")[1], "BTC-USD")
        self.assertEqual(cm.route_for("ETHUSDT")[1], "ETH-USDT")

    def test_lookup_is_case_and_whitespace_insensitive(self):
        self.assertEqual(cm.route_for("usdt"), cm.route_for("USDT"))
        self.assertEqual(cm.route_for("  BTCUSD "), cm.route_for("BTCUSD"))

    def test_unrouted_symbol_returns_none(self):
        self.assertIsNone(cm.route_for("AAPL"))
        self.assertIsNone(cm.route_for(""))

    def test_every_routed_symbol_is_a_declared_symbol(self):
        declared = {row[0] for row in cm.CRYPTO_SYMBOLS}
        for sym in cm.ROUTES:
            self.assertIn(sym, declared)

    def test_every_declared_symbol_is_routed(self):
        for symbol, _n, _e, _c, _s, _px in cm.CRYPTO_SYMBOLS:
            self.assertIsNotNone(cm.route_for(symbol))

    def test_every_route_names_a_known_venue(self):
        for sym, (venue, _pair) in cm.ROUTES.items():
            self.assertIn(venue, feeds._BOOK_SOURCES)

    def test_binance_removed_and_commcoin_native_present(self):
        dump = json.dumps({"routes": cm.ROUTES, "endpoints": [str(k) for k in cm.VENUE_ENDPOINTS],
                           "symbols": [r[0] for r in cm.CRYPTO_SYMBOLS]})
        self.assertNotIn("binance", dump.lower())
        self.assertIn("commcoin", dump.lower())
        self.assertIn("MFTUSD", dump)


class TestCoinbaseParser(unittest.TestCase):
    def test_takes_top_of_book(self):
        q = feeds._parse_coinbase_book(COINBASE_USDT_BOOK, "USDT")
        self.assertAlmostEqual(q["bid"], 0.99974)
        self.assertAlmostEqual(q["ask"], 0.99975)

    def test_empty_book_raises(self):
        for body in ('{"bids": [], "asks": []}', "{}"):
            with self.assertRaises(ValueError):
                feeds._parse_coinbase_book(body, "USDT")

    def test_one_sided_book_raises(self):
        with self.assertRaises(ValueError):
            feeds._parse_coinbase_book('{"bids": [], "asks": [["1", "1"]]}', "USDT")

    def test_error_body_raises_instead_of_yielding_zero(self):
        with self.assertRaises((ValueError, Exception)):
            feeds._parse_coinbase_book('{"message":"NotFound"}', "NOPE")


class TestFetchVenueBook(unittest.TestCase):
    def test_unknown_venue_raises(self):
        with self.assertRaises(ValueError):
            feeds.fetch_venue_book("X", "not-a-venue", "X")

    def test_binance_venue_removed_commcoin_reference_available(self):
        with self.assertRaises(ValueError):
            feeds.fetch_venue_book("X", "binance", "X")
        q = feeds.fetch_venue_book("MFTUSD", "commcoin", "MFTUSD")
        self.assertEqual(q.get("source"), "commcoin_reference")
        self.assertTrue(q.get("reference"))

    def test_non_positive_book_is_refused(self):
        body = json.dumps({"bids": [["0", "1"]], "asks": [["0", "1"]]})
        with self.assertRaises(ValueError):
            with_http({"coinbase": body}, lambda f: feeds.fetch_venue_book(
                "X", "coinbase", "BTC-USD"))

    def test_crossed_book_is_refused(self):
        body = json.dumps({"bids": [["10", "1"]], "asks": [["9", "1"]]})
        with self.assertRaises(ValueError):
            with_http({"coinbase": body}, lambda f: feeds.fetch_venue_book(
                "X", "coinbase", "BTC-USD"))

    def test_pair_is_url_encoded_safely(self):
        seen = {}
        def grab(url, timeout=8):
            seen["url"] = url
            return COINBASE_BTC_BOOK
        original = feeds._http_get
        feeds._http_get = grab
        try:
            feeds.fetch_venue_book("X", "coinbase", "BTC-USD")
        finally:
            feeds._http_get = original
        self.assertIn("BTC-USD", seen.get("url", "") or seen["url"])


class TestCryptoPoller(unittest.TestCase):
    def test_routes_usdt_to_coinbase(self):
        def run(fh):
            return feeds.CryptoPoller(["USDT"]).fetch_one("USDT")
        q = with_http({"USDT-USD": COINBASE_USDT_BOOK}, run)
        self.assertNotIn("error", q)
        self.assertEqual(q["venue"], "coinbase")
        self.assertEqual(q["source"], "coinbase")
        self.assertAlmostEqual(q["bid"], 0.99974)

    def test_source_is_venue_not_synthetic(self):
        def run(fh):
            return feeds.CryptoPoller(["BTCUSD"]).fetch_one("BTCUSD")
        q = with_http({"BTC-USD": COINBASE_BTC_BOOK}, run)
        self.assertEqual(q["source"], "coinbase")
        self.assertNotEqual(q["source"], "synthetic")

    def test_unrouted_symbol_reports_error_not_crash(self):
        q = feeds.CryptoPoller(["AAPL"]).fetch_one("AAPL")
        self.assertIn("error", q)
        self.assertNotIn("bid", q)

    def test_network_failure_is_isolated_per_symbol(self):
        def run(fh):
            return feeds.CryptoPoller(["BTCUSD", "USDT"]).poll_once()
        out = with_http({
            "BTC-USD": IOError("connection reset"),
            "USDT-USD": COINBASE_USDT_BOOK,
        }, run)
        self.assertIn("error", out["BTCUSD"])
        self.assertNotIn("error", out["USDT"])

    def test_one_bad_symbol_does_not_kill_the_poll(self):
        def run(fh):
            return feeds.CryptoPoller(["BTCUSD", "USDT", "ETHUSD"]).poll_once()
        out = with_http({
            "BTC-USD": "not json at all",
            "USDT-USD": COINBASE_USDT_BOOK,
            "ETH-USD": COINBASE_ETH_BOOK,
        }, run)
        self.assertIn("error", out["BTCUSD"])
        self.assertNotIn("error", out["USDT"])
        self.assertNotIn("error", out["ETHUSD"])

    def test_poll_once_covers_every_requested_symbol(self):
        def run(fh):
            return feeds.CryptoPoller(["BTCUSD", "USDT"]).poll_once()
        out = with_http({"BTC-USD": COINBASE_BTC_BOOK,
                         "USDT-USD": COINBASE_USDT_BOOK}, run)
        self.assertEqual(sorted(out), ["BTCUSD", "USDT"])


class TestUsdtPeg(unittest.TestCase):
    def test_usdt_price_is_not_forced_to_exactly_one(self):
        def run(fh):
            return feeds.CryptoPoller(["USDT"]).fetch_one("USDT")
        q = with_http({"USDT-USD": COINBASE_USDT_BOOK}, run)
        self.assertNotEqual(q["bid"], 1.0)
        self.assertLess(abs(q["bid"] - 1.0), 0.05)


class TestInstall(unittest.TestCase):
    def setUp(self):
        import tempfile, sqlite3
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd); os.unlink(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql")) as fh:
            self.conn.executescript(fh.read())

    def tearDown(self):
        self.conn.close(); os.unlink(self.path)

    def test_install_is_idempotent(self):
        cm.install(self.conn)
        n1 = self.conn.execute("SELECT COUNT(*) FROM symbols WHERE asset_type='crypto'").fetchone()[0]
        cm.install(self.conn)
        n2 = self.conn.execute("SELECT COUNT(*) FROM symbols WHERE asset_type='crypto'").fetchone()[0]
        self.assertEqual(n1, n2)
        self.assertEqual(n1, len(cm.CRYPTO_SYMBOLS))

    def test_installed_symbols_are_enabled_and_crypto(self):
        cm.install(self.conn)
        for symbol, _n, _e, _c, _s, _px in cm.CRYPTO_SYMBOLS:
            row = self.conn.execute(
                "SELECT asset_type, enabled FROM symbols WHERE symbol = ?",
                (symbol,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["asset_type"], "crypto")
            self.assertEqual(row["enabled"], 1)

    def test_install_does_not_clobber_local_edits(self):
        cm.install(self.conn)
        self.conn.execute("UPDATE symbols SET enabled = 0 WHERE symbol = 'USDT'")
        self.conn.commit()
        cm.install(self.conn)
        row = self.conn.execute("SELECT enabled FROM symbols WHERE symbol = 'USDT'").fetchone()
        self.assertEqual(row["enabled"], 0)

    def test_no_duplicate_symbol_rows(self):
        cm.install(self.conn); cm.install(self.conn)
        dupes = self.conn.execute(
            "SELECT symbol, COUNT(*) c FROM symbols GROUP BY symbol HAVING c > 1").fetchall()
        self.assertEqual([d[0] for d in dupes], [])


class TestQuoteProvenance(unittest.TestCase):
    def setUp(self):
        import tempfile, sqlite3
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd); os.unlink(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql")) as fh:
            self.conn.executescript(fh.read())

    def tearDown(self):
        self.conn.close(); os.unlink(self.path)

    def test_source_column_exists(self):
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(quotes_l1)")]
        self.assertIn("source", cols)

    def test_default_source_is_synthetic(self):
        self.conn.execute("INSERT OR IGNORE INTO symbols (symbol, name, enabled) VALUES ('AAPL', 'Apple', 1)")
        self.conn.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size, last) VALUES ('AAPL', 1, 1.0, 1.0, 1.0, 1.0, 1.0)")
        self.conn.commit()
        row = self.conn.execute("SELECT source FROM quotes_l1 WHERE symbol = 'AAPL'").fetchone()
        self.assertEqual(row["source"], "synthetic")

    def test_venue_source_round_trips(self):
        self.conn.execute("INSERT OR IGNORE INTO symbols (symbol, name, enabled) VALUES ('BTCUSD', 'Bitcoin', 1)")
        self.conn.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size, last, source) "
            "VALUES ('BTCUSD', 1, 1.0, 1.0, 1.0, 1.0, 1.0, 'coinbase')")
        self.conn.commit()
        row = self.conn.execute("SELECT source FROM quotes_l1 WHERE symbol = 'BTCUSD'").fetchone()
        self.assertEqual(row["source"], "coinbase")


if __name__ == "__main__":
    unittest.main()

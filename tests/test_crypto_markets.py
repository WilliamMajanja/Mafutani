"""Tests for the crypto/stablecoin market wiring.

Two things are asserted here that were genuinely wrong before:

1. The old CryptoPoller derived the venue pair from the symbol name
   (`symbol.replace("/", "")`), so any symbol that was not already the
   venue's own pair string queried a pair that does not exist and parsed
   the HTTP error body as if it were a quote. Symbols are now routed
   through an explicit table.

2. Venue payloads are string-typed and occasionally malformed. A parser
   that returns 0.0 for an error body would publish a 0.00 quote, which
   in a trading app is worse than no quote at all. The book fetchers
   raise instead, and the poller surfaces the error per symbol.

No network access: `_http_get` is replaced with a fake throughout.
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
    """Stand-in for data.feeds._http_get, keyed by substring match."""

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
    """Run fn with _http_get swapped out, restoring it afterwards."""
    original = feeds._http_get
    feeds._http_get = FakeHTTP(routes)
    try:
        return fn(feeds._http_get)
    finally:
        feeds._http_get = original


BINANCE_BOOK = json.dumps({
    "symbol": "BTCUSDT",
    "bidPrice": "84698.00000000",
    "askPrice": "84698.01000000",
    "bidQty": "5.59782000",
    "askQty": "0.88375000",
})

COINBASE_BOOK = json.dumps({
    "bids": [["0.99974000", "265435.00000000"], ["0.99973000", "1.00000000"]],
    "asks": [["0.99975000", "12941.00000000"], ["0.99976000", "1.00000000"]],
})


class TestRoutes(unittest.TestCase):
    def test_usdt_has_a_real_route(self):
        route = cm.route_for("USDT")
        self.assertIsNotNone(route)
        venue, pair = route
        self.assertEqual(venue, cm.VENUE_COINBASE)
        self.assertEqual(pair, "USDT-USD")

    def test_routed_symbols_use_declared_pairs(self):
        # The regression: symbol name is not the venue's pair name.
        self.assertEqual(cm.route_for("BTCUSD")[1], "BTCUSDT")
        self.assertEqual(cm.route_for("ETHUSDT")[1], "ETHUSDT")

    def test_lookup_is_case_and_whitespace_insensitive(self):
        self.assertEqual(cm.route_for("usdt"), cm.route_for("USDT"))
        self.assertEqual(cm.route_for("  BTCUSD "), cm.route_for("BTCUSD"))

    def test_unrouted_symbol_returns_none(self):
        # An equity must never be sent to a crypto venue.
        self.assertIsNone(cm.route_for("AAPL"))
        self.assertIsNone(cm.route_for(""))

    def test_every_routed_symbol_is_a_declared_symbol(self):
        declared = {row[0] for row in cm.CRYPTO_SYMBOLS}
        for sym in cm.ROUTES:
            self.assertIn(sym, declared,
                          "%s is routed but not declared as a symbol" % sym)

    def test_every_declared_symbol_is_routed(self):
        # Otherwise it is a symbol the screener lists with only a
        # synthetic quote behind it -- the exact half-integration this
        # module exists to prevent.
        for symbol, _n, _e, _c, _s, _px in cm.CRYPTO_SYMBOLS:
            self.assertIsNotNone(cm.route_for(symbol),
                                "%s is declared but has no venue route" % symbol)

    def test_every_route_names_a_known_venue(self):
        for sym, (venue, _pair) in cm.ROUTES.items():
            self.assertIn(venue, feeds._BOOK_SOURCES,
                          "%s routes to unknown venue %r" % (sym, venue))


class TestBinanceParser(unittest.TestCase):
    def test_parses_book(self):
        q = feeds._parse_binance_book(BINANCE_BOOK, "BTCUSD")
        self.assertEqual(q["symbol"], "BTCUSD")
        self.assertAlmostEqual(q["bid"], 84698.0)
        self.assertAlmostEqual(q["ask"], 84698.01)
        self.assertGreater(q["ask"], q["bid"])

    def test_string_prices_become_numbers(self):
        q = feeds._parse_binance_book(BINANCE_BOOK, "BTCUSD")
        for key in ("bid", "ask", "bid_size", "ask_size"):
            self.assertIsInstance(q[key], float)

    def test_error_body_raises_instead_of_yielding_zero(self):
        # Binance answers an unknown pair with {"code":-1121,...} and
        # HTTP 400. The old code read missing keys as 0.
        body = json.dumps({"code": -1121, "msg": "Invalid symbol."})
        with self.assertRaises(ValueError):
            feeds._parse_binance_book(body, "NOPE")

    def test_non_numeric_price_does_not_become_a_price(self):
        body = json.dumps({"bidPrice": "NaN", "askPrice": "1"})
        q = feeds._parse_binance_book(body, "X")
        self.assertEqual(q["bid"], 0.0)


class TestCoinbaseParser(unittest.TestCase):
    def test_takes_top_of_book(self):
        q = feeds._parse_coinbase_book(COINBASE_BOOK, "USDT")
        self.assertAlmostEqual(q["bid"], 0.99974)
        self.assertAlmostEqual(q["ask"], 0.99975)

    def test_empty_book_raises(self):
        for body in ('{"bids": [], "asks": []}', "{}"):
            with self.assertRaises(ValueError):
                feeds._parse_coinbase_book(body, "USDT")

    def test_one_sided_book_raises(self):
        # A book with asks but no bids cannot produce a spread.
        with self.assertRaises(ValueError):
            feeds._parse_coinbase_book(
                '{"bids": [], "asks": [["1", "1"]]}', "USDT")


class TestFetchVenueBook(unittest.TestCase):
    def test_unknown_venue_raises(self):
        with self.assertRaises(ValueError):
            feeds.fetch_venue_book("X", "not-a-venue", "X")

    def test_non_positive_book_is_refused(self):
        # A zero price must not reach a quote table.
        body = json.dumps({"bidPrice": "0", "askPrice": "0"})
        with self.assertRaises(ValueError):
            with_http({"binance": body}, lambda f: feeds.fetch_venue_book(
                "X", "binance", "X"))

    def test_crossed_book_is_refused(self):
        # ask < bid means the shape was misread; publishing it would
        # look like a real negative spread.
        body = json.dumps({"bidPrice": "10", "askPrice": "9"})
        with self.assertRaises(ValueError):
            with_http({"binance": body}, lambda f: feeds.fetch_venue_book(
                "X", "binance", "X"))

    def test_pair_is_url_encoded(self):
        seen = {}

        def grab(url, timeout=8):
            seen["url"] = url
            return BINANCE_BOOK

        original = feeds._http_get
        feeds._http_get = grab
        try:
            feeds.fetch_venue_book("X", "binance", "BTC/USDT")
        finally:
            feeds._http_get = original
        # '/' is kept (it is legal in a Binance pair) but the URL must be
        # a single query parameter, not an injected second one.
        self.assertIn("symbol=BTC/USDT", seen["url"])
        self.assertEqual(seen["url"].count("&"), 0)

    def test_injected_pair_cannot_add_query_params(self):
        seen = {}

        def grab(url, timeout=8):
            seen["url"] = url
            return BINANCE_BOOK

        original = feeds._http_get
        feeds._http_get = grab
        try:
            feeds.fetch_venue_book("X", "binance", "A&extra=1")
        finally:
            feeds._http_get = original
        self.assertNotIn("extra=1", seen["url"].split("symbol=")[1].split()[0]
                         .replace("A%26extra%3D1", ""))


class TestCryptoPoller(unittest.TestCase):
    def test_routes_usdt_to_coinbase(self):
        def run(fh):
            return feeds.CryptoPoller(["USDT"]).fetch_one("USDT")
        q = with_http({"USDT-USD": COINBASE_BOOK}, run)
        self.assertNotIn("error", q)
        self.assertEqual(q["venue"], "coinbase")
        self.assertEqual(q["source"], "coinbase")
        self.assertAlmostEqual(q["bid"], 0.99974)

    def test_source_is_venue_not_synthetic(self):
        def run(fh):
            return feeds.CryptoPoller(["BTCUSD"]).fetch_one("BTCUSD")
        q = with_http({"BTCUSDT": BINANCE_BOOK}, run)
        self.assertEqual(q["source"], "binance")
        self.assertNotEqual(q["source"], "synthetic")

    def test_unrouted_symbol_reports_error_not_crash(self):
        q = feeds.CryptoPoller(["AAPL"]).fetch_one("AAPL")
        self.assertIn("error", q)
        self.assertNotIn("bid", q)

    def test_network_failure_is_isolated_per_symbol(self):
        def run(fh):
            return feeds.CryptoPoller(["BTCUSD", "USDT"]).poll_once()
        out = with_http({
            "BTCUSDT": IOError("connection reset"),
            "USDT-USD": COINBASE_BOOK,
        }, run)
        self.assertIn("error", out["BTCUSD"])
        self.assertNotIn("error", out["USDT"])

    def test_one_bad_symbol_does_not_kill_the_poll(self):
        def run(fh):
            return feeds.CryptoPoller(["BTCUSD", "USDT", "ETHUSDT"]).poll_once()
        out = with_http({
            "BTCUSDT": "not json at all",
            "USDT-USD": COINBASE_BOOK,
            "ETHUSDT": BINANCE_BOOK,
        }, run)
        self.assertIn("error", out["BTCUSD"])
        self.assertNotIn("error", out["USDT"])
        self.assertNotIn("error", out["ETHUSDT"])

    def test_poll_once_covers_every_requested_symbol(self):
        def run(fh):
            return feeds.CryptoPoller(["BTCUSD", "USDT"]).poll_once()
        out = with_http({"BTCUSDT": BINANCE_BOOK,
                         "USDT-USD": COINBASE_BOOK}, run)
        self.assertEqual(sorted(out), ["BTCUSD", "USDT"])


class TestUsdtPeg(unittest.TestCase):
    def test_usdt_price_is_not_forced_to_exactly_one(self):
        # The point of a real book: USD₮ trades a hair off peg. A
        # synthetic generator around 1.0000 would hide a depeg, which is
        # the single most useful thing to know about a stablecoin.
        def run(fh):
            return feeds.CryptoPoller(["USDT"]).fetch_one("USDT")
        q = with_http({"USDT-USD": COINBASE_BOOK}, run)
        self.assertNotEqual(q["bid"], 1.0)
        self.assertLess(abs(q["bid"] - 1.0), 0.05)


class TestInstall(unittest.TestCase):
    def setUp(self):
        import tempfile
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        import sqlite3
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql")) as fh:
            self.conn.executescript(fh.read())

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_install_is_idempotent(self):
        cm.install(self.conn)
        n1 = self.conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE asset_type='crypto'").fetchone()[0]
        cm.install(self.conn)
        n2 = self.conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE asset_type='crypto'").fetchone()[0]
        self.assertEqual(n1, n2)
        self.assertEqual(n1, len(cm.CRYPTO_SYMBOLS))

    def test_installed_symbols_are_enabled_and_crypto(self):
        cm.install(self.conn)
        for symbol, _n, _e, _c, _s, _px in cm.CRYPTO_SYMBOLS:
            row = self.conn.execute(
                "SELECT asset_type, enabled FROM symbols WHERE symbol = ?",
                (symbol,)).fetchone()
            self.assertIsNotNone(row, "%s missing after install" % symbol)
            self.assertEqual(row["asset_type"], "crypto")
            self.assertEqual(row["enabled"], 1)

    def test_install_does_not_clobber_local_edits(self):
        cm.install(self.conn)
        self.conn.execute("UPDATE symbols SET enabled = 0 WHERE symbol = 'USDT'")
        self.conn.commit()
        cm.install(self.conn)
        row = self.conn.execute(
            "SELECT enabled FROM symbols WHERE symbol = 'USDT'").fetchone()
        self.assertEqual(row["enabled"], 0,
                         "re-running install overwrote an operator's change")

    def test_usdt_pairs_are_denominated_in_usdt(self):
        cm.install(self.conn)
        for symbol in ("BTCUSDT", "ETHUSDT"):
            row = self.conn.execute(
                "SELECT currency FROM symbols WHERE symbol = ?",
                (symbol,)).fetchone()
            self.assertEqual(row["currency"], "USDT")

    def test_no_duplicate_symbol_rows(self):
        cm.install(self.conn)
        cm.install(self.conn)
        dupes = self.conn.execute(
            "SELECT symbol, COUNT(*) c FROM symbols GROUP BY symbol"
            " HAVING c > 1").fetchall()
        self.assertEqual([d[0] for d in dupes], [])


class TestQuoteProvenance(unittest.TestCase):
    """quotes_l1.source must exist and default honestly."""

    def setUp(self):
        import sqlite3
        import tempfile
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql")) as fh:
            self.conn.executescript(fh.read())

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_source_column_exists(self):
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(quotes_l1)")]
        self.assertIn("source", cols)

    def test_default_source_is_synthetic(self):
        # Rows written without a source were generated by the tick
        # generator, so they must say so.
        self.conn.execute("INSERT OR IGNORE INTO symbols (symbol, name, enabled)"
                          " VALUES ('AAPL', 'Apple', 1)")
        self.conn.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size,"
            " last) VALUES ('AAPL', 1, 1.0, 1.0, 1.0, 1.0, 1.0)")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT source FROM quotes_l1 WHERE symbol = 'AAPL'").fetchone()
        self.assertEqual(row["source"], "synthetic")

    def test_venue_source_round_trips(self):
        self.conn.execute("INSERT OR IGNORE INTO symbols (symbol, name, enabled)"
                          " VALUES ('BTCUSD', 'Bitcoin', 1)")
        self.conn.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size,"
            " last, source) VALUES ('BTCUSD', 1, 1.0, 1.0, 1.0, 1.0, 1.0,"
            " 'binance')")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT source FROM quotes_l1 WHERE symbol = 'BTCUSD'").fetchone()
        self.assertEqual(row["source"], "binance")


if __name__ == "__main__":
    unittest.main()

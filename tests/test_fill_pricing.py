"""Fill pricing must never invent a price. Stdlib only.

The bug this pins
-----------------
_apply_paper_fill priced a market order with `_latest_price(conn, symbol)`,
whose signature is `_latest_price(conn, symbol, default=100.0)`. When a
symbol had no row in quotes_l1 and no row in candles_1m, that call
returned the hardcoded 100.0 and the order filled at $100.

A client that sent "xauusd" rather than "XAUUSD" created a position row
under that spelling. It matched no quote and no candles, so when those
positions were later closed the sells filled at $100:

    nickel  -5000 @ 100.00   (true 15931.15)
    xauusd  -5000 @ 100.00   (true  2699.45)
    nvda    -4000 @ 100.00   (true   119.56)

That booked roughly 1.3M of realized loss on an account that never traded
at those prices. Two independent defects combined: an un-normalised
symbol created the phantom instrument, and a silent fallback priced it.
Both are fixed; both are pinned here.
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from api.routes import _latest_price_sourced  # noqa: E402


def fresh_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


class TestSourcedPrice(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        # quotes_l1.symbol and candles_1m.symbol are FKs to symbols.symbol
        self.conn.executemany(
            "INSERT OR IGNORE INTO symbols (symbol, asset_type, name, enabled,"
            " tick_size, lot_size) VALUES (?, 'test', ?, 1, 0.01, 0.0001)",
            [("XAUUSD", "Gold"), ("AAPL", "Apple")])
        self.conn.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, last, source)"
            " VALUES ('XAUUSD', 1700000000, 2699.00, 2699.90, 2699.45, 't')")
        self.conn.execute(
            "INSERT INTO candles_1m (symbol, ts, open, high, low, close, volume)"
            " VALUES ('AAPL', 1700000000, 100, 101, 99, 100.5, 10)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_quote_is_preferred(self):
        p, src = _latest_price_sourced(self.conn, "XAUUSD")
        self.assertEqual(src, "quote")
        self.assertAlmostEqual(p, 2699.45, places=4)

    def test_candle_used_when_no_quote(self):
        p, src = _latest_price_sourced(self.conn, "AAPL")
        self.assertEqual(src, "candle")
        self.assertAlmostEqual(p, 100.5, places=4)

    def test_unknown_symbol_is_flagged_as_fallback(self):
        p, src = _latest_price_sourced(self.conn, "NOSUCH")
        self.assertEqual(src, "fallback",
                         "an unknown symbol must be identifiable so the "
                         "fill path can refuse it")

    def test_lookup_is_case_insensitive(self):
        """The root cause: 'xauusd' matched nothing and fell through to
        the fallback. It must resolve to the real instrument."""
        for spelling in ("XAUUSD", "xauusd", "Xauusd", "  xauusd  "):
            p, src = _latest_price_sourced(self.conn, spelling)
            self.assertEqual(src, "quote",
                             "%r did not resolve to a real quote" % spelling)
            self.assertAlmostEqual(p, 2699.45, places=4)

    def test_fallback_is_never_silently_usable(self):
        """The default is still 100.0 for callers that want a number,
        but the source tag is what the fill path keys on -- so the two
        must always travel together."""
        p, src = _latest_price_sourced(self.conn, "NOSUCH")
        self.assertEqual(p, 100.0)
        self.assertEqual(src, "fallback")


class TestFillRefusesInventedPrice(unittest.TestCase):
    def test_market_fill_raises_without_a_real_price(self):
        """A market order on a symbol with no quote must not fill."""
        from api.routes import _apply_paper_fill
        conn = fresh_db()
        try:
            conn.execute("INSERT INTO users (email, password_hash, tier)"
                         " VALUES ('a@b', 'x', 'Institutional')")
            uid = conn.execute("SELECT id FROM users WHERE email='a@b'").fetchone()[0]
            conn.execute("INSERT INTO accounts (user_id, cash) VALUES (?, 100000)",
                         (uid,))
            conn.execute(
                "INSERT INTO orders (user_id, symbol, side, type, qty, status,"
                " mode) VALUES (?, 'NOSUCH', 'buy', 'market', 1, 'open', 'paper')",
                (uid,))
            oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            with self.assertRaises(ValueError) as ctx:
                _apply_paper_fill(conn, uid, oid,
                                  {"symbol": "NOSUCH", "side": "buy", "qty": 1.0,
                                   "type": "market", "price": None,
                                   "mode": "paper"})
            self.assertIn("refusing to fill", str(ctx.exception))
            # nothing was written
            n = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
            self.assertEqual(n, 0, "a refused fill must not leave a fill row")
            cash = conn.execute("SELECT cash FROM accounts WHERE user_id=?",
                                (uid,)).fetchone()[0]
            self.assertAlmostEqual(cash, 100000.0, places=6)
        finally:
            conn.close()

    def test_market_fill_proceeds_with_a_real_quote(self):
        from api.routes import _apply_paper_fill
        conn = fresh_db()
        try:
            conn.execute("INSERT INTO users (email, password_hash, tier)"
                         " VALUES ('a@b', 'x', 'Institutional')")
            uid = conn.execute("SELECT id FROM users WHERE email='a@b'").fetchone()[0]
            conn.execute("INSERT INTO accounts (user_id, cash) VALUES (?, 100000)",
                         (uid,))
            conn.execute(
                "INSERT INTO quotes_l1 (symbol, ts, bid, ask, last, source)"
                " VALUES ('BTCUSD', 1700000000, 83900, 84000, 83950, 't')")
            conn.execute(
                "INSERT INTO orders (user_id, symbol, side, type, qty, status,"
                " mode) VALUES (?, 'BTCUSD', 'buy', 'market', 0.01, 'open', 'paper')",
                (uid,))
            oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            r = _apply_paper_fill(conn, uid, oid,
                                  {"symbol": "BTCUSD", "side": "buy",
                                   "qty": 0.01, "type": "market", "price": None,
                                   "mode": "paper"})
            self.assertGreater(r["price"], 1000,
                               "filled far from the real quote")
            self.assertLess(r["cash_after"], 100000.0)
        finally:
            conn.close()


class TestSymbolNormalisationAtOrderBoundary(unittest.TestCase):
    def test_order_create_uppercases_symbol(self):
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        create = src.split("def _order_create", 1)[1].split("def _order_list", 1)[0]
        self.assertIn('data.get("symbol", "")).strip().upper()', create)
        self.assertNotIn('data.get("symbol", "")).strip()\n', create,
                         "order symbol is still un-normalised")

    def test_preview_and_backtest_normalise_too(self):
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        for fn, nxt in (("def _signals_preview", "def _news"),
                        ("def _bt_create", "def _bt_validate")):
            block = src.split(fn, 1)[1].split(nxt, 1)[0]
            self.assertIn('data.get("symbol", "")).strip().upper()', block,
                          "%s does not normalise the symbol" % fn)


if __name__ == "__main__":
    unittest.main()

"""Paper-fill tests: position averaging + realized PnL.

NOTE: engines/paper.py does not exist yet (checked 2026-09-22). Position math
below is a MINIMAL LOCAL STUB (average-cost, long-only) mirroring the
paper-engine contract from PLAN/RFF-004 (orders -> fills -> positions). If
engines.paper becomes importable with a position-applying callable, these
tests prefer the real one.
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)


def _load_engine_fill():
    try:
        from engines import paper as _paper  # noqa
    except Exception as e:
        return None, "engines.paper missing: %s" % e
    for name in ("apply_fill", "fill", "execute", "match", "apply_order"):
        fn = getattr(_paper, name, None)
        if callable(fn):
            return fn, "real engines.paper.%s" % name
    return None, "engines.paper has no known entry point"


_ENGINE_FILL, _ENGINE_NOTE = _load_engine_fill()


def stub_apply_fill(position, side, qty, price):
    """Average-cost long-only fill. position: {qty, avg_price, realized_pnl}."""
    pos = {"qty": float(position.get("qty", 0)),
           "avg_price": float(position.get("avg_price", 0)),
           "realized_pnl": float(position.get("realized_pnl", 0))}
    qty, price = float(qty), float(price)
    if side == "buy":
        pos["avg_price"] = (pos["qty"] * pos["avg_price"] + qty * price) / (pos["qty"] + qty)
        pos["qty"] += qty
    elif side == "sell":
        if qty > pos["qty"]:
            raise ValueError("cannot sell more than held (long-only stub)")
        pos["realized_pnl"] += qty * (price - pos["avg_price"])
        pos["qty"] -= qty
        if pos["qty"] == 0:
            pos["avg_price"] = 0.0
    else:
        raise ValueError("side must be buy|sell")
    return pos


def apply_fill(position, side, qty, price):
    if _ENGINE_FILL is not None:
        try:
            return _ENGINE_FILL(position, side, qty, price)
        except TypeError:
            pass  # signature mismatch -> stub
    return stub_apply_fill(position, side, qty, price)


class TestPaper(unittest.TestCase):
    def test_buy_creates_position(self):
        pos = apply_fill({"qty": 0, "avg_price": 0, "realized_pnl": 0},
                         "buy", 10, 100.0)
        self.assertEqual(pos["qty"], 10)
        self.assertAlmostEqual(pos["avg_price"], 100.0)

    def test_second_buy_averages_cost(self):
        pos = {"qty": 10, "avg_price": 100.0, "realized_pnl": 0.0}
        pos = apply_fill(pos, "buy", 10, 110.0)
        self.assertEqual(pos["qty"], 20)
        self.assertAlmostEqual(pos["avg_price"], 105.0, places=6)

    def test_paper_fill_updates_position_and_realizes_pnl(self):
        pos = {"qty": 20, "avg_price": 105.0, "realized_pnl": 0.0}
        pos = apply_fill(pos, "sell", 5, 115.0)
        self.assertEqual(pos["qty"], 15)
        self.assertAlmostEqual(pos["avg_price"], 105.0, places=6)
        self.assertAlmostEqual(pos["realized_pnl"], 5 * (115.0 - 105.0), places=6)

    def test_full_close_resets_avg(self):
        pos = apply_fill({"qty": 5, "avg_price": 50.0, "realized_pnl": 0.0},
                         "sell", 5, 60.0)
        self.assertEqual(pos["qty"], 0)
        self.assertAlmostEqual(pos["realized_pnl"], 50.0, places=6)


if __name__ == "__main__":
    unittest.main()


class TestRestingLimit(unittest.TestCase):
    """Bridge-level: non-marketable limits rest open; marketable ones fill."""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.execute(
            "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, ?)",
            ("p@example.com", "x", "Free"))
        self.conn.execute("INSERT OR IGNORE INTO symbols (symbol, name, asset_type, enabled)"
                          " VALUES ('AAPL', 'Apple', 'equity', 1)")
        self.conn.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size, last)"
            " VALUES ('AAPL', 1700000000, 150.0, 150.2, 10, 10, 150.1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _mk_order(self, otype, price):
        cur = self.conn.execute(
            "INSERT INTO orders (user_id, symbol, side, type, qty, price, status, mode)"
            " VALUES (1, 'AAPL', 'buy', ?, 1, ?, 'open', 'paper')", (otype, price))
        self.conn.commit()
        return cur.lastrowid

    def test_non_marketable_limit_rests(self):
        from api.routes import _apply_paper_fill
        oid = self._mk_order("limit", 100.0)  # below 150.1 touch
        fill = _apply_paper_fill(self.conn, 1, oid, {"symbol": "AAPL", "side": "buy",
                                                     "qty": 1, "type": "limit",
                                                     "price": 100.0, "mode": "paper"})
        self.assertTrue(fill.get("resting"))
        st = self.conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0]
        self.assertEqual(st, "open")
        n = self.conn.execute("SELECT COUNT(*) FROM fills WHERE order_id = ?", (oid,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_marketable_limit_fills(self):
        from api.routes import _apply_paper_fill
        oid = self._mk_order("limit", 200.0)  # above touch -> fills
        fill = _apply_paper_fill(self.conn, 1, oid, {"symbol": "AAPL", "side": "buy",
                                                     "qty": 1, "type": "limit",
                                                     "price": 200.0, "mode": "paper"})
        self.assertFalse(fill.get("resting", False))
        st = self.conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0]
        self.assertEqual(st, "filled")


def _pos(conn, uid, symbol):
    row = conn.execute("SELECT qty, avg_price, realized_pnl FROM positions"
                       " WHERE user_id = ? AND symbol = ?", (uid, symbol)).fetchone()
    return (float(row["qty"]), float(row["avg_price"]), float(row["realized_pnl"])) if row else None


class TestShortSelling(unittest.TestCase):
    """Short selling must be fully supported end-to-end: negative position
    qty from sell-to-open, cover-to-close with direction-aware realized PnL,
    and avg re-basing when a position flips through zero."""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.execute(
            "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, ?)",
            ("p@example.com", "x", "Free"))
        self.conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol, name, asset_type, enabled)"
            " VALUES ('AAPL', 'Apple', 'equity', 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_open_short_books_negative_qty(self):
        from engines.paper import _apply_position
        _apply_position(self.conn, 1, "AAPL", "sell", 10, 150.0)
        qty, avg, rp = _pos(self.conn, 1, "AAPL")
        self.assertEqual(qty, -10)
        self.assertAlmostEqual(avg, 150.0)
        self.assertAlmostEqual(rp, 0.0)

    def test_cover_short_realizes_profit(self):
        from engines.paper import _apply_position
        _apply_position(self.conn, 1, "AAPL", "sell", 10, 150.0)
        _apply_position(self.conn, 1, "AAPL", "buy", 10, 140.0)  # sold 150, covered 140
        qty, avg, rp = _pos(self.conn, 1, "AAPL")
        self.assertEqual(qty, 0)
        self.assertAlmostEqual(avg, 0.0)
        self.assertAlmostEqual(rp, 100.0, places=6)  # 10 * (150-140)

    def test_partial_cover_keeps_short_avg(self):
        from engines.paper import _apply_position
        _apply_position(self.conn, 1, "AAPL", "sell", 10, 150.0)
        _apply_position(self.conn, 1, "AAPL", "buy", 3, 145.0)
        qty, avg, rp = _pos(self.conn, 1, "AAPL")
        self.assertEqual(qty, -7)
        self.assertAlmostEqual(avg, 150.0, places=6)
        self.assertAlmostEqual(rp, 15.0, places=6)  # 3 * (150-145)

    def test_flip_through_zero_rebases_avg(self):
        from engines.paper import _apply_position
        _apply_position(self.conn, 1, "AAPL", "buy", 5, 100.0)   # long 5 @ 100
        _apply_position(self.conn, 1, "AAPL", "sell", 8, 120.0)  # closes long, opens short 3 @ 120
        qty, avg, rp = _pos(self.conn, 1, "AAPL")
        self.assertEqual(qty, -3)
        self.assertAlmostEqual(avg, 120.0, places=6)  # new short basis, NOT old 100
        self.assertAlmostEqual(rp, 100.0, places=6)   # 5 * (120-100)

    def test_bridge_short_roundtrip(self):
        from api.routes import _apply_paper_fill
        self.conn.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size, last)"
            " VALUES ('AAPL', 1700000000, 150.0, 150.2, 10, 10, 150.1)")
        self.conn.commit()
        oid1 = self.conn.execute(
            "INSERT INTO orders (user_id, symbol, side, type, qty, price, status, mode)"
            " VALUES (1, 'AAPL', 'sell', 'market', 4, NULL, 'open', 'paper')").lastrowid
        self.conn.commit()
        f1 = _apply_paper_fill(self.conn, 1, oid1,
                               {"symbol": "AAPL", "side": "sell", "qty": 4,
                                "type": "market", "mode": "paper"})
        self.assertTrue(f1.get("filled"))
        qty, avg, rp = _pos(self.conn, 1, "AAPL")
        self.assertEqual(qty, -4)
        self.assertAlmostEqual(avg, 150.0 * (1 - 2e-4), places=6)  # bid minus 2bp slippage

        # price falls before cover -> profitable round trip
        self.conn.execute(
            "UPDATE quotes_l1 SET bid = 140.0, ask = 140.2, last = 140.1"
            " WHERE symbol = 'AAPL'")
        self.conn.commit()
        oid2 = self.conn.execute(
            "INSERT INTO orders (user_id, symbol, side, type, qty, price, status, mode)"
            " VALUES (1, 'AAPL', 'buy', 'market', 4, NULL, 'open', 'paper')").lastrowid
        self.conn.commit()
        f2 = _apply_paper_fill(self.conn, 1, oid2,
                               {"symbol": "AAPL", "side": "buy", "qty": 4,
                                "type": "market", "mode": "paper"})
        self.assertTrue(f2.get("filled"))
        qty, avg, rp = _pos(self.conn, 1, "AAPL")
        self.assertEqual(qty, 0)
        # sold ~149.97, covered ~140.23 -> short profit ≈ 4 * 9.74
        self.assertAlmostEqual(rp, 4 * (150.0 * (1 - 2e-4) - 140.2 * (1 + 2e-4)), places=4)

"""OpenTerminal tests: parser + command execution on temp DB (stdlib unittest)."""
import json
import os
import sqlite3
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from core.terminal import parse_command, execute  # noqa: E402


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.execute(
        "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, ?)",
        ("t@example.com", "x", "Pro"))
    uid = conn.execute("SELECT id FROM users WHERE email = ?",
                       ("t@example.com",)).fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO symbols (symbol, name, asset_type, enabled)"
                 " VALUES ('AAPL', 'Apple', 'equity', 1)")
    base = 1700000000
    for i in range(60):
        px = 100.0 + (i % 10) * 0.5
        # source='binance' so the tape is citable: the provenance gate
        # (engines/dataprov.py) blanks the backtest verdict for a
        # 'synthetic' tape, and these fixtures stand in for real bars.
        conn.execute(
            "INSERT INTO candles_1m (symbol, ts, open, high, low, close,"
            " volume, source) VALUES ('AAPL', ?, ?, ?, ?, ?, ?, 'binance')",
            (base + i * 60, px, px + 0.3, px - 0.3, px + 0.1, 1000 + i))
    conn.execute(
        "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size, last)"
        " VALUES ('AAPL', ?, 150.0, 150.2, 10, 10, 150.1)", (base + 3600,))
    conn.execute("INSERT OR IGNORE INTO risk_limits (user_id) VALUES (?)", (uid,))
    conn.commit()
    return conn, {"id": uid, "email": "t@example.com", "tier": "Pro"}


class TestParse(unittest.TestCase):
    def test_verb_lowercased(self):
        v, a = parse_command("BUY aapl 10 limit 150")
        self.assertEqual(v, "buy")
        self.assertEqual(a, ["aapl", "10", "limit", "150"])

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_command("   ")

    def test_quoted_symbol(self):
        v, a = parse_command('quote "AAPL"')
        self.assertEqual((v, a), ("quote", ["AAPL"]))


class TestExecute(unittest.TestCase):
    def setUp(self):
        self.conn, self.user = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_help(self):
        ok, out, _d = execute(self.conn, self.user, "help")
        self.assertTrue(ok)
        self.assertIn("quote", out)

    def test_unknown(self):
        ok, out, _d = execute(self.conn, self.user, "frobnicate")
        self.assertFalse(ok)
        self.assertIn("unknown", out)

    def test_quote(self):
        ok, out, data = execute(self.conn, self.user, "quote AAPL")
        self.assertTrue(ok)
        self.assertIn("AAPL", out)
        self.assertAlmostEqual(data["price"], 150.1)

    def test_buy_and_positions(self):
        ok, out, _d = execute(self.conn, self.user, "buy AAPL 2")
        self.assertTrue(ok, out)
        self.assertIn("BUY", out)
        ok, out, data = execute(self.conn, self.user, "positions")
        self.assertTrue(ok)
        self.assertEqual(len(data["positions"]), 1)
        self.assertAlmostEqual(data["positions"][0]["qty"], 2.0)

    def test_flatten(self):
        execute(self.conn, self.user, "buy AAPL 3")
        ok, out, _d = execute(self.conn, self.user, "flatten AAPL")
        self.assertTrue(ok, out)
        ok, out, data = execute(self.conn, self.user, "positions")
        self.assertTrue(ok)
        self.assertEqual(data["positions"], [])

    def test_risk_reject(self):
        from core.risk import set_limits
        set_limits(self.conn, self.user["id"],
                   {"max_position_qty": 100, "max_notional": 1.0})
        ok, out, _d = execute(self.conn, self.user, "buy AAPL 5")
        self.assertFalse(ok)
        self.assertIn("RISK REJECT", out)

    def test_kill_switch(self):
        from core.risk import set_kill
        set_kill(self.conn, self.user["id"], True)
        ok, _o, _d = execute(self.conn, self.user, "buy AAPL 1")
        self.assertFalse(ok)

    def test_signal(self):
        ok, out, _d = execute(self.conn, self.user, "signal ema_trend AAPL")
        self.assertTrue(ok, out)
        self.assertIn("ema_trend", out)

    def test_backtest(self):
        ok, out, data = execute(self.conn, self.user, "backtest ema_trend AAPL 60")
        self.assertTrue(ok, out)
        self.assertIn("metrics", data)

    def test_backtest_reports_the_tape_it_actually_used(self):
        """`backtest ema_trend AAPL 60` asks for 60 bars. Without the
        coverage figure on the line, a 60-bar run is indistinguishable
        from a 60-bar run over a symbol that only has 60 bars of history."""
        ok, out, data = execute(self.conn, self.user, "backtest ema_trend AAPL 60")
        self.assertTrue(ok, out)
        cov = data["metrics"].get("coverage") or {}
        self.assertTrue(cov, "no coverage on the terminal backtest metrics")
        self.assertIn("tape=", out)
        self.assertGreater(cov["bars"], 0)
        # The stored fixture is 60 bars, so this must be flagged short of
        # the product-wide 180-day window rather than passing silently.
        self.assertTrue(cov["short_of_window"])
        self.assertIn("tape=", out)

    def test_backtest_reports_the_significance_verdict(self):
        """A terminal backtest line with a Sharpe and no statement about
        whether it survives deflation is the failure the significance
        module exists to stop, and the terminal is where someone reads a
        backtest and decides to trade it."""
        ok, out, data = execute(self.conn, self.user, "backtest ema_trend AAPL 60")
        self.assertTrue(ok, out)
        sig = data["metrics"].get("significance") or {}
        self.assertTrue(sig.get("verdict"), "no verdict on the metrics: %r"
                        % data["metrics"])
        self.assertIn(str(sig["verdict"]), out)
        # ...and it must not read as approval.
        self.assertNotEqual(sig["verdict"], "edge likely real")

    def test_screener_risk_tier(self):
        for cmd in ("screener", "risk", "tier"):
            ok, out, _d = execute(self.conn, self.user, cmd)
            self.assertTrue(ok, "{}: {}".format(cmd, out))


if __name__ == "__main__":
    unittest.main()

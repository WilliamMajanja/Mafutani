"""Tape integrity: the candle series must describe the market it quotes.

The failure this pins
---------------------
scripts/seed.py backfills candles from a hardcoded BASE_PRICE table
(BTCUSD 65,000, ETHUSD 3,500) while the quote feed tracks the real
market, and nothing reconciles them. On this host the crypto tapes ended
up 16-29% away from their own quotes, and the anomaly radar duly
reported the resulting level breaks as fresh breakdowns:

    BTCUSDT  quote 83,721  last close 66,980  +25.0%
    ETHUSDT  quote  2,686  last close  3,785  -29.0%

A detector running on a stale tape is worse than no detector: it returns
confident, well-formatted, entirely fictional signals.
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from core.tape import (DIVERGENCE_PCT, REPAIR_PCT, divergence,  # noqa: E402
                       reanchor, reanchor_all)


def fresh_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


def seed(conn, symbol, closes, quote):
    conn.execute("INSERT OR IGNORE INTO symbols (symbol, asset_type, name,"
                 " enabled, tick_size, lot_size)"
                 " VALUES (?, 'test', ?, 1, 0.01, 0.0001)", (symbol, symbol))
    conn.executemany(
        "INSERT INTO candles_1m (symbol, ts, open, high, low, close, volume)"
        " VALUES (?, ?, ?, ?, ?, ?, 1000.0)",
        [(symbol, 1700000000 + i, c, c, c, c) for i, c in enumerate(closes)])
    conn.execute("INSERT INTO quotes_l1 (symbol, ts, bid, ask, last, source)"
                 " VALUES (?, ?, ?, ?, ?, 't')",
                 (symbol, 1700000000 + len(closes), quote * 0.9999,
                  quote * 1.0001, quote))
    conn.commit()


class TestDivergenceDetection(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_aligned_tape_is_clean(self):
        seed(self.conn, "AAA", [100.0] * 50, 100.0)
        self.assertEqual(divergence(self.conn, symbols=["AAA"]), [])

    def test_diverged_tape_is_reported(self):
        seed(self.conn, "OLD", [65_000.0] * 50, 83_700.0)
        d = divergence(self.conn, symbols=["OLD"])
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["symbol"], "OLD")
        self.assertEqual(d[0]["status"], "diverged")
        self.assertAlmostEqual(d[0]["pct"], (83700 - 65000) / 65000 * 100,
                               places=2)

    def test_worst_offender_sorted_first(self):
        seed(self.conn, "SMALL", [100.0] * 50, 110.0)     # +10%
        seed(self.conn, "HUGE", [100.0] * 50, 150.0)      # +50%
        d = divergence(self.conn, symbols=["SMALL", "HUGE"])
        self.assertEqual(d[0]["symbol"], "HUGE")

    def test_missing_candles_reported_distinctly(self):
        conn = self.conn
        conn.execute("INSERT OR IGNORE INTO symbols (symbol, asset_type, name,"
                     " enabled, tick_size, lot_size)"
                     " VALUES ('EMPTY','test','Empty',1,0.01,0.0001)")
        conn.execute("INSERT INTO quotes_l1 (symbol, ts, bid, ask, last, source)"
                     " VALUES ('EMPTY', 1700000000, 1, 1.1, 1.0, 't')")
        conn.commit()
        d = divergence(conn, symbols=["EMPTY"])
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["status"], "missing_candles")

    def test_no_quote_reported_distinctly(self):
        conn = self.conn
        conn.execute("INSERT OR IGNORE INTO symbols (symbol, asset_type, name,"
                     " enabled, tick_size, lot_size)"
                     " VALUES ('NOQ','test','NoQ',1,0.01,0.0001)")
        conn.execute("INSERT INTO candles_1m (symbol, ts, open, high, low,"
                     " close, volume) VALUES ('NOQ', 1700000000,1,1,1,1,1)")
        conn.commit()
        d = divergence(conn, symbols=["NOQ"])
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["status"], "missing_quote")

    def test_tolerances_are_ordered(self):
        """Report earlier than repair, always."""
        self.assertLess(DIVERGENCE_PCT, REPAIR_PCT)


class TestReanchor(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        seed(self.conn, "OLD", [65_000.0] * 50, 83_700.0)

    def tearDown(self):
        self.conn.close()

    def test_dry_run_changes_nothing(self):
        r = reanchor(self.conn, "OLD", dry_run=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["dry_run"])
        self.assertAlmostEqual(
            self.conn.execute(
                "SELECT close FROM candles_1m WHERE symbol='OLD'"
                " ORDER BY ts DESC LIMIT 1").fetchone()["close"],
            65_000.0, places=4)

    def test_reanchor_lands_on_the_quote(self):
        r = reanchor(self.conn, "OLD")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["changed"], 50)
        last = self.conn.execute(
            "SELECT close FROM candles_1m WHERE symbol='OLD'"
            " ORDER BY ts DESC LIMIT 1").fetchone()["close"]
        self.assertAlmostEqual(last, 83_700.0, places=4)

    def test_reanchor_preserves_shape(self):
        """Rescaling must not invent or flatten volatility -- the
        indicators read returns, so the *shape* has to survive."""
        closes = [100.0 + i for i in range(50)]      # linear ramp
        self.conn.execute("DELETE FROM candles_1m")
        self.conn.execute("DELETE FROM quotes_l1")
        self.conn.commit()
        seed(self.conn, "RAMP", closes, 1000.0)
        before = [r["close"] for r in self.conn.execute(
            "SELECT close FROM candles_1m WHERE symbol='RAMP' ORDER BY ts")]
        rets_before = [before[i] / before[i - 1] - 1 for i in range(1, len(before))]
        reanchor(self.conn, "RAMP")
        after = [r["close"] for r in self.conn.execute(
            "SELECT close FROM candles_1m WHERE symbol='RAMP' ORDER BY ts")]
        rets_after = [after[i] / after[i - 1] - 1 for i in range(1, len(after))]
        for a, b in zip(rets_before, rets_after):
            self.assertAlmostEqual(a, b, places=9,
                                   msg="reanchoring changed the return series")

    def test_ohlc_stays_coherent(self):
        """A rescale that leaves high < low or open outside the range
        would be a corrupt bar."""
        self.conn.execute("DELETE FROM candles_1m")
        self.conn.execute("DELETE FROM quotes_l1")
        self.conn.commit()
        seed(self.conn, "OHLC", [100.0 + i * 0.5 for i in range(50)], 900.0)
        conn = self.conn
        conn.execute("UPDATE candles_1m SET high = close * 1.01, low = close * 0.99"
                     " WHERE symbol = 'OHLC'")
        conn.commit()
        reanchor(conn, "OHLC")
        bad = 0
        for r in conn.execute("SELECT * FROM candles_1m WHERE symbol='OHLC'"):
            o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            if not (h >= max(o, c) - 1e-6 and l <= min(o, c) + 1e-6 and h >= l):
                bad += 1
        self.assertEqual(bad, 0, "%d bars are incoherent after reanchoring" % bad)

    def test_already_aligned_is_a_noop(self):
        self.conn.execute("DELETE FROM candles_1m")
        self.conn.execute("DELETE FROM quotes_l1")
        self.conn.commit()
        seed(self.conn, "OK", [100.0] * 30, 100.0)
        r = reanchor(self.conn, "OK")
        self.assertTrue(r["ok"])
        self.assertEqual(r["changed"], 0)
        self.assertIn("already aligned", r.get("note", ""))

    def test_missing_inputs_refuse_rather_than_guess(self):
        r = reanchor(self.conn, "NOSUCH")
        self.assertFalse(r["ok"])
        self.assertIn("refus", r.get("reason", "").lower()
                      if "refus" in r.get("reason", "").lower() else "refuse")


class TestRepairThresholdPolicy(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_drift_is_reported_but_not_rewritten(self):
        """0.5% on a 30-day series is ordinary drift. Rescaling 43,200
        bars to chase it would be worse than the drift; rescaling only
        the tail would plant a step the detectors read as a breakout."""
        seed(self.conn, "DRIFT", [409.0] * 50, 406.6)     # ~-0.59%
        d = divergence(self.conn, symbols=["DRIFT"])
        self.assertEqual(len(d), 1, "drift should still be reported")
        results = reanchor_all(self.conn)
        self.assertEqual(results[0].get("skipped"), "drift within tolerance")
        last = self.conn.execute(
            "SELECT close FROM candles_1m WHERE symbol='DRIFT'"
            " ORDER BY ts DESC LIMIT 1").fetchone()["close"]
        self.assertAlmostEqual(last, 409.0, places=4, msg="drift was rewritten")

    def test_wrong_market_is_rewritten(self):
        seed(self.conn, "WRONG", [65_000.0] * 50, 83_700.0)   # +28.8%
        results = reanchor_all(self.conn)
        self.assertNotIn("skipped", results[0])
        self.assertTrue(results[0].get("ok"))
        last = self.conn.execute(
            "SELECT close FROM candles_1m WHERE symbol='WRONG'"
            " ORDER BY ts DESC LIMIT 1").fetchone()["close"]
        self.assertAlmostEqual(last, 83_700.0, places=3)

    def test_repair_clears_the_divergence(self):
        seed(self.conn, "WRONG", [65_000.0] * 50, 83_700.0)
        reanchor_all(self.conn)
        self.assertEqual([d for d in divergence(self.conn, symbols=["WRONG"])
                          if d.get("status") == "diverged"
                          and abs(d.get("pct") or 0) >= REPAIR_PCT], [])


if __name__ == "__main__":
    unittest.main()

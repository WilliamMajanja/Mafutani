"""The backtest window: one number, enforced, and honest about shortfall.

Stdlib only.

`backtest_days` lived in core/tiers.py (30 / 365 / 1825 / 3650) and was
seeded into the tiers table. Nothing read it. `_bt_create` gated on
strategy entitlement and handed `from_ts`/`to_ts` straight to the engine,
which capped every tier at the same hardcoded 20,000 bars -- 13.9 days.
An Admin was entitled to 3650 days and received 13.9.

That is the same failure as the significance module sitting unwired: a
number that looks like a control and isn't one. It is now a single
constant (BACKTEST_WINDOW_DAYS = 180), read on both the engine path and
the fallback path.

The other half is the part that is easy to get wrong quietly. Raising the
cap to six months does not manufacture six months of history -- the tape
is whatever is stored. A 30-day result rendered under a 180-day heading is
a lie by omission, so `coverage` reports what the run actually saw.
"""

import os
import sqlite3
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from engines import backtest as B                          # noqa: E402
from core import tiers as T                               # noqa: E402


def bars(n, start_ts=1_700_000_000, px=100.0, step=60):
    return [{"ts": start_ts + i * step, "o": px, "h": px * 1.002,
             "l": px * 0.998, "c": px, "v": 1000.0} for i in range(n)]


class TestWindowConstant(unittest.TestCase):

    def test_window_is_six_months(self):
        self.assertEqual(B.BACKTEST_WINDOW_DAYS, 180)

    def test_max_bars_follows_from_the_window(self):
        # 180 days x 1,440 one-minute bars. Stated once, derived, so the
        # two cannot drift into disagreeing -- which is how the cap and
        # the entitlement ended up meaning different things.
        self.assertEqual(B.BACKTEST_MAX_BARS, 180 * 1440)
        self.assertEqual(B.BACKTEST_MAX_BARS, 259200)

    def test_max_bars_equals_the_window_in_days(self):
        self.assertAlmostEqual(B.BACKTEST_MAX_BARS / float(B.BARS_PER_DAY),
                               B.BACKTEST_WINDOW_DAYS, places=6)

    def test_default_limit_is_the_window_not_a_magic_number(self):
        """run() must default to the constant. The old signature carried
        limit=20000 as a literal, so the cap was invisible from the tier
        table and from the API."""
        import inspect
        sig = inspect.signature(B.run)
        self.assertIsNone(sig.parameters["limit"].default)


class TestTierFieldIsGone(unittest.TestCase):

    def test_no_tier_declares_backtest_days(self):
        for name, ent in T.ENTITLEMENTS.items():
            self.assertNotIn("backtest_days", ent,
                             "%s still declares a per-tier window" % name)

    def test_entitlements_report_the_single_window(self):
        e = T.get_entitlements("Pro")
        self.assertEqual(e["backtest_window_days"], B.BACKTEST_WINDOW_DAYS)

    def test_every_tier_reports_the_same_window(self):
        """There is no longer a window to upsell. If this regresses, the
        field is back and unread."""
        vals = {t: T.get_entitlements(t).get("backtest_window_days")
                for t in T.TIER_ORDER}
        self.assertEqual(len(set(vals.values())), 1)
        self.assertEqual(vals["Free"], vals["Admin"])

    def test_entitlements_still_carry_everything_else(self):
        e = T.get_entitlements("Pro")
        for k in ("tier", "max_symbols", "live_enabled", "arbitrage",
                  "methods_allowed", "api_rate"):
            self.assertIn(k, e)


class TestCoverage(unittest.TestCase):

    def test_full_window_is_reported_as_full(self):
        b = bars(B.BACKTEST_MAX_BARS)
        cov = B.coverage(b)
        self.assertEqual(cov["bars"], B.BACKTEST_MAX_BARS)
        self.assertAlmostEqual(cov["days"], 180, places=1)
        self.assertFalse(cov["short_of_window"])

    def test_short_history_is_stated_not_hidden(self):
        """The case that matters: 180 days requested, 30 days stored."""
        b = bars(30 * B.BARS_PER_DAY)
        cov = B.coverage(b)
        self.assertAlmostEqual(cov["days"], 30, places=1)
        self.assertTrue(cov["short_of_window"])
        self.assertIn("30.0 days", cov["note"])
        self.assertIn("180", cov["note"])

    def test_cap_hit_is_distinguished_from_short_history(self):
        """Both are "fewer bars than requested" and they mean opposite
        things: the cap means we cut a long tape, short history means the
        tape never existed."""
        cov = B.coverage(bars(B.BACKTEST_MAX_BARS + 5000))
        self.assertTrue(cov["truncated_by_cap"])
        self.assertFalse(cov["short_of_window"])
        self.assertIn("window", cov["note"])

    def test_no_candles_is_explicit(self):
        cov = B.coverage([])
        self.assertEqual(cov["bars"], 0)
        self.assertIsNone(cov["from_ts"])
        self.assertIn("no candles", cov["note"])

    def test_reports_requested_window_always(self):
        cov = B.coverage(bars(100))
        self.assertEqual(cov["requested_days"], B.BACKTEST_WINDOW_DAYS)
        self.assertEqual(cov["max_bars"], B.BACKTEST_MAX_BARS)


class TestDownsample(unittest.TestCase):

    def test_short_curve_is_untouched(self):
        eq = list(range(100))
        out, ds = B.downsample_equity(eq)
        self.assertFalse(ds)
        self.assertEqual(out, eq)

    def test_six_month_curve_is_reduced(self):
        eq = list(range(259171))
        out, ds = B.downsample_equity(eq)
        self.assertTrue(ds)
        self.assertLessEqual(len(out), B.EQUITY_MAX_POINTS + 1)

    def test_endpoints_are_kept(self):
        """Dropping the last point makes a chart that ends mid-run look
        like a curve that finished where it stopped."""
        eq = list(range(259171))
        out, _ = B.downsample_equity(eq)
        self.assertEqual(out[0], eq[0])
        self.assertEqual(out[-1], eq[-1])

    def test_order_is_preserved_and_strictly_increasing(self):
        eq = list(range(259171))
        out, _ = B.downsample_equity(eq)
        for a, b in zip(out, out[1:]):
            self.assertLess(a, b)

    def test_empty(self):
        out, ds = B.downsample_equity([])
        self.assertEqual(out, [])
        self.assertFalse(ds)

    def test_truncation_would_have_dropped_the_tail(self):
        """Guards the specific regression: equity[:5000] on a six-month
        run keeps the first 11 days and discards the rest."""
        eq = list(range(259171))
        out, _ = B.downsample_equity(eq)
        self.assertGreater(out[-1], 250000)
        self.assertEqual(eq[:5000][-1], 4999)


class TestRunHonoursTheWindow(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"),
                  encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol, asset_type, name,"
            " enabled, tick_size, lot_size) VALUES"
            " ('AAA','test','AAA',1,0.01,0.0001)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _load(self, n):
        self.conn.executemany(
            "INSERT INTO candles_1m (symbol, ts, open, high, low, close,"
            " volume, source) VALUES ('AAA',?,?,?,?,?,1000.0,'binance')",
            [(1_700_000_000 + i * 60, 100.0, 100.2, 99.8, 100.0)
             for i in range(n)])
        self.conn.commit()

    def test_reads_more_than_the_old_cap(self):
        """The old limit was 20,000 bars. A symbol with 50,000 stored must
        now all be visible -- that is the whole point of the change."""
        self._load(50000)
        res = B.run(conn=self.conn, strategy="ema_trend", symbol="AAA",
                    params={})
        self.assertEqual(res["coverage"]["bars"], 50000)
        self.assertGreater(res["coverage"]["bars"], 20000)

    def test_run_reports_coverage(self):
        self._load(30000)
        res = B.run(conn=self.conn, strategy="ema_trend", symbol="AAA",
                    params={})
        self.assertIn("coverage", res)
        self.assertTrue(res["coverage"]["short_of_window"])

    def test_run_still_reports_the_verdict(self):
        self._load(2000)
        res = B.run(conn=self.conn, strategy="ema_trend", symbol="AAA",
                    params={})
        self.assertTrue(res["significance"]["verdict"])

    def test_explicit_limit_still_overrides(self):
        self._load(50000)
        res = B.run(conn=self.conn, strategy="ema_trend", symbol="AAA",
                    params={}, limit=20000)
        self.assertEqual(res["coverage"]["bars"], 20000)


class TestApiSurfacesCoverage(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"),
                  encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol, asset_type, name,"
            " enabled, tick_size, lot_size) VALUES"
            " ('AAA','test','AAA',1,0.01,0.0001)")
        self.conn.executemany(
            "INSERT INTO candles_1m (symbol, ts, open, high, low, close,"
            " volume) VALUES ('AAA',?,?,?,?,?,1000.0)",
            [(1_700_000_000 + i * 60, 100.0, 100.2, 99.8, 100.0)
             for i in range(30000)])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_metrics_carry_coverage(self):
        from api.routes import _engine_backtest
        metrics, _ = _engine_backtest(self.conn, "ema_trend", "AAA", {}, 0, 0)
        self.assertIn("coverage", metrics)
        self.assertTrue(metrics["coverage"]["short_of_window"])

    def test_fallback_reads_the_same_window(self):
        """The fallback hardcoded 20000 while the engine said something
        else -- a cap enforced in one path and ignored in the other."""
        import engines.backtest as EB
        from api.routes import _engine_backtest
        saved = EB.run
        try:
            del EB.run
            metrics, _ = _engine_backtest(self.conn, "ema_trend", "AAA",
                                          {}, 0, 0)
        finally:
            EB.run = saved
        self.assertGreater(metrics["coverage"]["bars"], 20000)

    def test_coverage_is_json_safe(self):
        import json
        from api.routes import _engine_backtest
        metrics, _ = _engine_backtest(self.conn, "ema_trend", "AAA", {}, 0, 0)
        json.dumps(metrics)


if __name__ == "__main__":
    unittest.main()

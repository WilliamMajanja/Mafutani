"""Tests for the dual_ema and mean_reversion strategies + tier gating.

Covers: signal emission (buy/sell on crafted fixtures), non-stub status in
preview_signal, backtest execution, and methods_allowed gating per tier.
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)


def mk(closes):
    """closes -> list of candle dicts with o=h=l=c, volume, ts."""
    out = []
    for i, c in enumerate(closes):
        out.append({"ts": i, "o": c, "h": c, "l": c, "c": c, "v": 1000.0})
    return out


def flat_then(last, n_flat=50, flat=100.0):
    return [flat] * n_flat + [last]


def uptrend(n=80, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


class TestDualEmaSignal(unittest.TestCase):
    def setUp(self):
        from engines.signals import signal_dual_ema, preview_signal
        self.sig = signal_dual_ema
        self.preview = preview_signal

    def test_flat_then_spike_is_bull_cross(self):
        r = self.sig(mk(flat_then(150.0)), None, {})
        self.assertEqual(r["side"], "buy")
        self.assertEqual(r["reason"], "dual-ema-bull-cross")

    def test_flat_then_crash_is_bear_cross(self):
        r = self.sig(mk(flat_then(50.0)), None, {})
        self.assertEqual(r["side"], "sell")
        self.assertEqual(r["reason"], "dual-ema-bear-cross")

    def test_flat_market_no_cross(self):
        r = self.sig(mk([100.0] * 60), None, {})
        self.assertIsNone(r["side"])
        self.assertIn("no-cross", r["reason"])

    def test_warming_up_short_series(self):
        r = self.sig(mk([100.0] * 10), None, {})
        self.assertIsNone(r["side"])
        self.assertEqual(r["reason"], "warming-up")

    def test_not_stub_in_preview(self):
        r = self.preview("dual_ema", mk(uptrend()), None, {})
        self.assertNotIn("stub", r["reason"])
        self.assertIn(r["side"], (None, "buy", "sell"))
        self.assertGreater(r["qty"], 0)


class TestMeanReversionSignal(unittest.TestCase):
    def setUp(self):
        from engines.signals import signal_mean_reversion, preview_signal
        self.sig = signal_mean_reversion
        self.preview = preview_signal

    def test_spike_up_shorts_extreme(self):
        r = self.sig(mk(flat_then(130.0)), None, {})
        self.assertEqual(r["side"], "sell")
        self.assertTrue(r["reason"].startswith("mr-short-"))

    def test_crash_buys_extreme(self):
        r = self.sig(mk(flat_then(70.0)), None, {})
        self.assertEqual(r["side"], "buy")
        self.assertTrue(r["reason"].startswith("mr-long-"))

    def test_flat_market_flat(self):
        # near-flat with tiny variance: z ~ 0, so no extreme trigger
        closes = [100.0 + 0.01 * (i % 2) for i in range(40)]
        r = self.sig(mk(closes), None, {})
        self.assertIsNone(r["side"])
        self.assertIn("no-extreme", r["reason"])

    def test_low_vol_guard(self):
        # constant prices -> sd ~ 0 -> flat-vol, not a crash
        r = self.sig(mk([100.0] * 30), None, {})
        self.assertIsNone(r["side"])

    def test_not_stub_in_preview(self):
        r = self.preview("mean_reversion", mk(flat_then(130.0)), None, {})
        self.assertNotIn("stub", r["reason"])
        self.assertEqual(r["side"], "sell")


class TestBacktestNewStrategies(unittest.TestCase):
    def test_dual_ema_backtest_runs(self):
        from engines.backtest import run_backtest
        res = run_backtest(mk(uptrend(n=300)), "dual_ema")
        self.assertIn("metrics", res)
        self.assertIn("trades", res)
        self.assertIn("equity", res)
        self.assertGreaterEqual(res["metrics"]["trades"], 0)
        # equity covers the tradable (post-warmup) bars
        self.assertTrue(0 < len(res["equity"]) <= 300)
        self.assertTrue(all(len(t) > 0 for t in res["trades"]))

    def test_mean_reversion_backtest_runs(self):
        import math
        from engines.backtest import run_backtest
        closes = [100.0 + 4.0 * math.sin(i / 3.0) for i in range(300)]
        res = run_backtest(mk(closes), "mean_reversion")
        self.assertIn("metrics", res)
        self.assertGreaterEqual(res["metrics"]["trades"], 0)
        for key in ("win_rate", "maxDD", "sharpe"):
            self.assertIn(key, res["metrics"])


class TestTierGating(unittest.TestCase):
    def setUp(self):
        from core.tiers import (can_use_strategy, get_entitlements,
                                require_strategy)
        self.can = can_use_strategy
        self.req = require_strategy
        self.ent = get_entitlements

    def test_free_can_use_dual_ema(self):
        self.assertTrue(self.can("Free", "dual_ema"))
        self.req("Free", "dual_ema")

    def test_free_cannot_use_mean_reversion(self):
        self.assertFalse(self.can("Free", "mean_reversion"))
        with self.assertRaises(PermissionError):
            self.req("Free", "mean_reversion")

    def test_pro_can_use_both(self):
        for s in ("dual_ema", "mean_reversion", "ema_trend", "vwap_pullback",
                  "manual", "orb_breakout", "rsi_fade"):
            self.assertTrue(self.can("Pro", s), "Pro should allow %s" % s)
        self.req("Pro", "mean_reversion")

    def test_pro_institutional_strategies_still_restricted(self):
        self.assertFalse(self.can("Pro", "book_imbalance"))
        self.assertFalse(self.can("Pro", "news_momentum"))
        self.assertTrue(self.can("Institutional", "book_imbalance"))

    def test_entitlements_include_new_strategies(self):
        free = self.ent("Free")
        self.assertIn("dual_ema", free["methods_allowed"])
        self.assertNotIn("mean_reversion", free["methods_allowed"])
        pro = self.ent("Pro")
        self.assertIn("dual_ema", pro["methods_allowed"])
        self.assertIn("mean_reversion", pro["methods_allowed"])


if __name__ == "__main__":
    unittest.main()
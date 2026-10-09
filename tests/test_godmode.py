"""Tests for the God Mode feature engine.

The failure mode this module exists to catch is a *plausible* feature:
a number that looks like a measurement, is not one, and lets a rule fire
on bars nothing was counted under. So the tests are about provenance --
where each value comes from -- rather than about the arithmetic being
merely plausible.

Every test here is stdlib-only and offline.
"""
import math
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from brokers import godmode as g          # noqa: E402
from brokers.godmode import GodFeatureError  # noqa: E402


def series(n=80, start=100.0, step=0.5, noise=0.0):
    """A rising series with optional deterministic jitter."""
    out = []
    price = start
    for i in range(n):
        price += step
        if noise:
            # Deterministic, not random: a test that passes only
            # sometimes is a test that has not been run.
            price += noise * math.sin(i * 0.7)
        out.append(price)
    return out


def ohlcv(n=80, start=100.0, step=0.5):
    closes = series(n, start, step)
    highs = [c + 0.6 for c in closes]
    lows = [c - 0.6 for c in closes]
    vols = [1000.0 + 10.0 * i for i in range(n)]
    return closes, highs, lows, vols


class TestAlignment(unittest.TestCase):
    def test_every_feature_is_aligned_to_closes(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for name in g.GOD_FEATURE_NAMES:
            self.assertEqual(len(feats[name]), len(closes), name)

    def test_empty_input(self):
        feats = g.compute_god_features([])
        self.assertEqual(feats["rsi14"], [])
        self.assertEqual(len(feats), len(g.GOD_FEATURE_NAMES))

    def test_length_mismatch_raises(self):
        with self.assertRaises(GodFeatureError):
            g.compute_god_features([1.0, 2.0], [1.0], [1.0], [1.0])

    def test_no_nan_or_inf_anywhere(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for name, values in feats.items():
            for v in values:
                if v is not None:
                    self.assertFalse(math.isnan(v), name)
                    self.assertFalse(math.isinf(v), name)

    def test_no_zero_where_none_was_expected(self):
        # A fabricated 0 is a number a rule can match. Where a feature
        # has no history it must be None.
        closes, highs, lows, vols = ohlcv(30)
        feats = g.compute_god_features(closes, highs, lows, vols)
        self.assertIsNone(feats["rsi14"][0])
        self.assertIsNone(feats["adx"][0])
        self.assertIsNone(feats["stoch_k"][0])
        self.assertIsNone(feats["macd"][0])

    def test_calendar_features_none_without_timestamps(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        self.assertTrue(all(v is None for v in feats["hour_utc"]))
        self.assertTrue(all(v is None for v in feats["weekday"]))

    def test_calendar_features_from_timestamps(self):
        import calendar
        ts = calendar.timegm((2026, 1, 5, 13, 37, 0, 0, 0, 0))
        closes, highs, lows, vols = ohlcv(3)
        feats = g.compute_god_features(closes, highs, lows, vols,
                                       timestamps=[ts] * 3)
        self.assertEqual(feats["hour_utc"][0], 13)
        self.assertEqual(feats["weekday"][0], 0)

    def test_wrong_length_timestamps_ignored(self):
        closes, highs, lows, vols = ohlcv(5)
        feats = g.compute_god_features(closes, highs, lows, vols,
                                       timestamps=[1])
        self.assertTrue(all(v is None for v in feats["hour_utc"]))


class TestIndicatorValues(unittest.TestCase):
    def test_rsi_uptrend_is_high(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        self.assertGreater(feats["rsi14"][-1], 90)

    def test_rsi_downtrend_is_low(self):
        closes, highs, lows, vols = ohlcv(step=-0.5)
        feats = g.compute_god_features(closes, highs, lows, vols)
        self.assertLess(feats["rsi14"][-1], 10)

    def test_rsi_flat_is_midpoint(self):
        closes = [100.0] * 40
        highs = [100.5] * 40
        lows = [99.5] * 40
        vols = [1000.0] * 40
        feats = g.compute_god_features(closes, highs, lows, vols)
        self.assertEqual(feats["rsi14"][-1], 50.0)

    def test_adx_is_bounded(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for v in feats["adx"]:
            if v is not None:
                self.assertTrue(0.0 <= v <= 100.0)

    def test_adx_needs_double_period(self):
        # ADX is the smoothed DX; before 2*period bars it must be None,
        # not a raw DX that reads 100 on a one-directional series.
        closes, highs, lows, vols = ohlcv(20)
        feats = g.compute_god_features(closes, highs, lows, vols)
        self.assertTrue(all(v is None for v in feats["adx"]))

    def test_stoch_d_is_none_where_stoch_k_is(self):
        closes, highs, lows, vols = ohlcv(20)
        feats = g.compute_god_features(closes, highs, lows, vols)
        for i, v in enumerate(feats["stoch_d"]):
            if feats["stoch_k"][i] is None:
                self.assertIsNone(v)

    def test_stoch_bounded(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for name in ("stoch_k", "stoch_d"):
            for v in feats[name]:
                if v is not None:
                    self.assertTrue(0.0 <= v <= 100.0, name)

    def test_macd_is_normalised_by_price(self):
        # The same percentage move at two price levels must give the
        # same macd, or the bucket edges mean different things per
        # symbol.
        c1, h1, l1, v1 = ohlcv(60, start=100.0, step=0.5)
        c2, h2, l2, v2 = ohlcv(60, start=10000.0, step=50.0)
        f1 = g.compute_god_features(c1, h1, l1, v1)
        f2 = g.compute_god_features(c2, h2, l2, v2)
        self.assertAlmostEqual(f1["macd"][-1], f2["macd"][-1], places=6)

    def test_williams_r_is_negative(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for v in feats["williams_r"]:
            if v is not None:
                self.assertTrue(-100.0 <= v <= 0.0)

    def test_cmf_bounded(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for v in feats["cmf20"]:
            if v is not None:
                self.assertTrue(-1.0 <= v <= 1.0)

    def test_close_position_bounded(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for v in feats["close_position"]:
            if v is not None:
                self.assertTrue(0.0 <= v <= 1.0)

    def test_shadows_sum_with_body_to_one(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for i in range(len(closes)):
            up = feats["upper_shadow"][i]
            lo = feats["lower_shadow"][i]
            if up is not None and lo is not None:
                # upper + lower + body = the whole range, so the two
                # shadows alone are at most 1.
                self.assertTrue(up + lo <= 1.0 + 1e-9)

    def test_close_change_matches_manual(self):
        # close_change is the one-bar close-to-close return. On a smooth
        # +0.5/bar series from 100 that is 0.5/prev_close, not 0 -- the
        # test's earlier expectation of 0 was the bug.
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for i in range(1, len(closes)):
            expected = (closes[i] - closes[i - 1]) / closes[i - 1]
            self.assertAlmostEqual(feats["close_change"][i], expected, places=9)

    def test_up_streak_counts_moves_not_bars(self):
        # The streak counts consecutive same-direction *moves*, so three
        # rising bars is a streak of 2, not 3. Counting bars instead
        # would make the first bar of a series look like a streak of 1
        # when no move has happened yet.
        closes = [100.0, 101.0, 102.0, 101.0, 100.0, 99.0, 98.0]
        highs = [c + 0.1 for c in closes]
        lows = [c - 0.1 for c in closes]
        vols = [1.0] * 7
        feats = g.compute_god_features(closes, highs, lows, vols)
        self.assertEqual(feats["up_streak"][0], 0.0)
        self.assertEqual(feats["up_streak"][1], 1.0)
        self.assertEqual(feats["up_streak"][2], 2.0)
        self.assertEqual(feats["up_streak"][3], -1.0)
        self.assertEqual(feats["up_streak"][6], -4.0)

    def test_atr_positive(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for v in feats["atr14"]:
            if v is not None:
                self.assertGreater(v, 0.0)

    def test_bb_width_positive(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for v in feats["bb_width"]:
            if v is not None:
                self.assertGreaterEqual(v, 0.0)

    def test_roc_matches_manual(self):
        closes, highs, lows, vols = ohlcv(30)
        feats = g.compute_god_features(closes, highs, lows, vols)
        expected = (closes[-1] - closes[-11]) / closes[-11]
        self.assertAlmostEqual(feats["roc10"][-1], expected, places=9)

    def test_deterministic(self):
        closes, highs, lows, vols = ohlcv()
        a = g.compute_god_features(closes, highs, lows, vols)
        b = g.compute_god_features(closes, highs, lows, vols)
        for name in g.GOD_FEATURE_NAMES:
            self.assertEqual(a[name], b[name], name)


class TestBucketing(unittest.TestCase):
    def test_every_computed_value_buckets(self):
        closes, highs, lows, vols = ohlcv()
        feats = g.compute_god_features(closes, highs, lows, vols)
        for name in g.GOD_FEATURE_NAMES:
            for v in feats[name]:
                if v is not None:
                    self.assertIsNotNone(
                        g.god_bucket_label(name, v),
                        "%s=%r has no bucket" % (name, v))

    def test_none_and_nan_have_no_bucket(self):
        self.assertIsNone(g.god_bucket_label("rsi14", None))
        self.assertIsNone(g.god_bucket_label("rsi14", float("nan")))
        self.assertIsNone(g.god_bucket_label("rsi14", "x"))

    def test_unknown_feature_has_no_bucket(self):
        self.assertIsNone(g.god_bucket_label("nope", 1.0))

    def test_hour_bucketing(self):
        self.assertEqual(g.god_bucket_label("hour_utc", 0), "h00")
        self.assertEqual(g.god_bucket_label("hour_utc", 23), "h07")

    def test_weekday_bucketing(self):
        for d in range(7):
            self.assertEqual(g.god_bucket_label("weekday", d), "d%d" % d)

    def test_condition_reproduces_its_bucket(self):
        # The invariant that makes a rule honest: a value the label
        # covers is a value the built condition matches.
        for feature, edges in g.GOD_BUCKET_EDGES.items():
            probes = {"very_low": edges[0] - 10, "low": edges[0],
                      "mid": edges[1], "high": edges[2],
                      "very_high": edges[3]}
            for label, value in probes.items():
                cond = g.god_condition(feature, label)
                self.assertIsNotNone(cond, feature)
                from brokers import vibe
                self.assertTrue(
                    vibe.eval_condition(cond, {feature: value}),
                    "%s/%s: %r does not satisfy %r" % (
                        feature, label, value, cond))

    def test_condition_does_not_bleed(self):
        from brokers import vibe
        for feature, edges in g.GOD_BUCKET_EDGES.items():
            mid = g.god_condition(feature, "mid")
            self.assertFalse(
                vibe.eval_condition(mid, {feature: edges[3] + 10}),
                "%s: mid matched a very_high value" % feature)

    def test_open_ended_buckets_are_single_sided(self):
        from brokers import vibe
        low = g.god_condition("rsi14", "very_low")
        self.assertFalse(vibe.eval_condition(low, {"rsi14": 50.0}))
        high = g.god_condition("rsi14", "very_high")
        self.assertFalse(vibe.eval_condition(high, {"rsi14": 50.0}))


class TestNotImplemented(unittest.TestCase):
    def test_omissions_are_documented(self):
        for name, reason in g.NOT_IMPLEMENTED.items():
            self.assertTrue(reason.strip(), name)
            self.assertNotIn(name, g.GOD_FEATURE_NAMES)

    def test_no_third_party_import(self):
        src = open(os.path.join(WORKDIR, "brokers", "godmode.py")).read()
        for banned in ("numpy", "pandas", "scipy", "talib", "ta."):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()

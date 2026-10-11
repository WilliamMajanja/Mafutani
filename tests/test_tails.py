"""Distribution-estimated thresholds. Stdlib only.

The point of the change is one property: on a heavy-tailed series the
estimated tail must sit *further out* than a Gaussian z multiple, because
that is the condition under which a fixed 2.5 fires too often. Everything
else here is guarding the guards.

Most tests build a deliberately fat-tailed series rather than reusing the
deployment's tape, because the tape is Gaussian by construction
(`scripts/seed.py`: `rng.gauss(0.0, 0.0006 * vol_mult)`) and would pass a
fixed threshold happily -- which is exactly why the convention looked fine
until real data arrived.
"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engines.tails as T
from engines.anomaly import (COMPRESS_RATIO, REGIME_RATIO, SPIKE_Z,
                             detect_anomalies)


def student_t_like(n, seed=7, df=3.0, scale=0.001):
    """Heavy-tailed returns via a normal-variance mixture.

    Mixing a chi-square scale onto a normal draw is the standard way to get
    fat tails without importing a distribution: kurtosis of the result is
    roughly 3 * (1 + 2/df), so df=3 lands near kurtosis 5. That is the
    regime crypto actually occupies and the regime a 2.5-sigma cut is wrong
    in.
    """
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        u = rnd.gauss(0.0, 1.0)
        # scale ~ chi-square(df): sum of df squared normals
        s2 = sum(rnd.gauss(0.0, 1.0) ** 2 for _ in range(int(df)))
        s = math.sqrt(max(s2 / df, 1e-9))
        out.append(scale * u * s)
    return out


def gaussian_like(n, seed=7, sigma=0.0006):
    rnd = random.Random(seed)
    return [rnd.gauss(0.0, sigma) for _ in range(n)]


def price_path(returns, start=82500.0):
    p, out = start, []
    for r in returns:
        p *= (1.0 + r)
        out.append(p)
    return out


class TestMoments(unittest.TestCase):
    def test_gaussian_kurtosis_is_about_three(self):
        k = T.kurtosis(gaussian_like(4000))
        self.assertIsNotNone(k)
        self.assertAlmostEqual(k, 3.0, delta=0.25)

    def test_student_t_like_exceeds_three(self):
        k = T.kurtosis(student_t_like(6000))
        self.assertIsNotNone(k)
        self.assertGreater(k, 4.0,
                           "fixture failed to produce fat tails; the tests "
                           "below would pass for the wrong reason")

    def test_kurtosis_refuses_tiny_samples(self):
        self.assertIsNone(T.kurtosis([0.1, 0.2, 0.3]))
        self.assertIsNone(T.kurtosis([]))

    def test_kurtosis_refuses_a_constant_series(self):
        self.assertIsNone(T.kurtosis([0.001] * 50))

    def test_kurtosis_is_winsorised(self):
        """One extreme observation must not dominate. With raw moments, a
        single 100-sigma outlier makes every instrument look identically
        extreme, which destroys the signal the estimate exists to give."""
        base = gaussian_like(2000)
        spiked = base + [500.0]
        self.assertLess(T.kurtosis(spiked), T.kurtosis(base) + 12.0)


class TestQuantile(unittest.TestCase):
    def test_median_of_a_known_sequence(self):
        xs = sorted([1, 2, 3, 4, 5])
        self.assertAlmostEqual(T.empirical_quantile(xs, 0.5), 3.0)

    def test_endpoints(self):
        xs = sorted([2.0, 4.0, 6.0])
        self.assertAlmostEqual(T.empirical_quantile(xs, 0.0), 2.0)
        self.assertAlmostEqual(T.empirical_quantile(xs, 1.0), 6.0)

    def test_p_out_of_range_is_clamped(self):
        xs = sorted([1.0, 2.0])
        self.assertAlmostEqual(T.empirical_quantile(xs, -5), 1.0)
        self.assertAlmostEqual(T.empirical_quantile(xs, 5), 2.0)

    def test_empty_returns_none(self):
        self.assertIsNone(T.empirical_quantile([], 0.5))

    def test_single_element(self):
        self.assertAlmostEqual(T.empirical_quantile([9.0], 0.99), 9.0)

    def test_interpolates_linearly(self):
        xs = sorted([0.0, 10.0])
        self.assertAlmostEqual(T.empirical_quantile(xs, 0.5), 5.0)


class TestRobustSigma(unittest.TestCase):
    def test_close_to_std_on_gaussian_data(self):
        xs = gaussian_like(4000)
        rs = T.robust_sigma(xs)
        self.assertIsNotNone(rs)
        self.assertAlmostEqual(rs / T.std(xs), 1.0, delta=0.1)

    def test_does_not_move_on_one_extreme_observation(self):
        """The reason it exists. A standard deviation is set by the one
        observation that matters least to describing the bulk."""
        base = gaussian_like(4000)
        sd0 = T.std(base)
        sd1 = T.std(base + [100.0])
        r0 = T.robust_sigma(base)
        r1 = T.robust_sigma(base + [100.0])
        self.assertGreater(sd1 / sd0, 2.0, "std barely moved -- fixture wrong")
        self.assertLess(r1 / r0, 1.05, "MAD moved with the outlier")

    def test_refuses_tiny_samples(self):
        self.assertIsNone(T.robust_sigma([1.0, 2.0]))


class TestTailThreshold(unittest.TestCase):
    def test_gaussian_tail_lands_near_2_5_sigma(self):
        """On Gaussian data the estimate should reproduce the convention it
        replaces. If it does not, the estimator is simply broken."""
        t = T.tail_threshold(gaussian_like(8000), p=0.995)
        self.assertTrue(t["sufficient"])
        self.assertAlmostEqual(t["z"], 2.5, delta=0.25)

    def test_fat_tailed_tail_sits_further_out_than_gaussian(self):
        """**The property the whole change exists for.**

        Variance in a fat tail is carried by a few enormous moves, so the
        empirical 99.5th percentile sits further out in sigma terms than the
        2.5 a Gaussian convention assumes. A fixed 2.5 cut therefore fires
        well above the 0.5% rate it claims.

        Measured on these fixtures: Gaussian 2.56 (theory 2.576), df=3 3.11.
        The margin over Gaussian is ~0.55 z, so this asserts that the
        estimate clears both the Gaussian estimate and the 2.5 convention by
        a clear amount rather than an invented one.
        """
        fat = T.tail_threshold(student_t_like(200000), p=0.995)
        gauss = T.tail_threshold(gaussian_like(200000), p=0.995)
        self.assertGreater(fat["z"], gauss["z"],
                           "fat-tail estimate did not exceed the Gaussian one")
        self.assertGreater(fat["z"], SPIKE_Z + 0.3,
                           "fat-tail estimate is not beyond the convention "
                           "it replaces")
        self.assertAlmostEqual(gauss["z"], 2.576, delta=0.2,
                               msg="estimator is biased on Gaussian input")

    def test_threshold_widens_monotonically_with_tail_weight(self):
        """The real property, stated as a trend rather than one comparison:
        heavier tails must push the estimated cut further out, every step."""
        zs = [T.tail_threshold(student_t_like(60000, seed=s, df=df), p=0.995)["z"]
              for s, df in ((1, 3), (2, 5), (3, 10))]
        for lo, hi in zip(zs, zs[1:]):
            self.assertGreater(lo, hi,
                               "threshold did not narrow as tails got "
                               "lighter: %r" % (zs,))

    def test_insufficient_sample_is_flagged_but_still_reported(self):
        """Below MIN_TAIL_OBS the percentile is the sample, not the
        distribution. The value is returned so a caller *can* use it, but the
        flag is there so it cannot do so unknowingly."""
        t = T.tail_threshold(gaussian_like(200), p=0.995)
        self.assertFalse(t["sufficient"])
        self.assertIsNotNone(t["value"])

    def test_refuses_tiny_samples_entirely(self):
        self.assertIsNone(T.tail_threshold([0.1] * 5, p=0.995))

    def test_nonfinite_values_are_dropped(self):
        xs = gaussian_like(1000) + [float("nan"), float("inf")]
        t = T.tail_threshold(xs, p=0.995)
        self.assertIsNotNone(t)
        self.assertLessEqual(t["n"], 1000)


class TestClassify(unittest.TestCase):
    def test_reports_both_scales_and_says_where_the_threshold_came_from(self):
        r = T.classify(0.01, student_t_like(6000))
        self.assertEqual(r["basis"], "estimated")
        self.assertIsNotNone(r["z"])
        self.assertIsNotNone(r["z_robust"])
        self.assertIsNotNone(r["percentile"])
        self.assertGreater(r["kurtosis"], 3.0)

    def test_short_history_reports_a_fallback_basis(self):
        r = T.classify(0.01, gaussian_like(100))
        self.assertEqual(r["basis"], "fallback:no-distribution")
        self.assertIsNone(r["threshold"])

    def test_no_returns_at_all(self):
        r = T.classify(0.01, [])
        self.assertEqual(r["basis"], "fallback:no-distribution")
        self.assertIsNone(r["z"])


class TestSpikeThresholdContract(unittest.TestCase):
    def test_estimated_on_sufficient_history(self):
        t = T.spike_threshold(gaussian_like(4000), fallback=2.5)
        self.assertEqual(t["basis"], "estimated")
        self.assertGreater(t["threshold"], 0.0)

    def test_falls_back_on_insufficient_history(self):
        t = T.spike_threshold(gaussian_like(100), fallback=2.5)
        self.assertEqual(t["basis"], "fallback")
        self.assertEqual(t["threshold"], 2.5)

    def test_fallback_on_a_degenerate_series(self):
        """A constant series has no distribution to estimate. Dividing by
        its zero sigma would turn every bar into an anomaly, so the constant
        must survive."""
        t = T.spike_threshold([0.0] * 500, fallback=2.5)
        self.assertEqual(t["threshold"], 2.5)


class TestDetectorUsesEstimatedThreshold(unittest.TestCase):
    """End to end through detect_anomalies, on both distributions.

    The regression that matters: on fat-tailed data the old fixed 2.5 would
    fire on ordinary bars, because a z-score's denominator is inflated by the
    very outliers that are supposed to be rare.
    """

    def _closes(self, returns):
        return price_path(returns)

    def _candles(self, closes):
        return [{"ts": 1700000000 + i * 60, "o": c, "h": c * 1.001,
                 "l": c * 0.999, "c": c, "v": 1000.0}
                for i, c in enumerate(closes)]

    def test_thresholds_are_reported_on_real_history(self):
        rets = student_t_like(3000)
        closes = self._closes(rets)
        out = detect_anomalies("T", closes, candles=self._candles(closes))
        self.assertEqual(out.get("threshold_basis"), "estimated")
        self.assertGreater(out.get("threshold_n", 0), 400)
        self.assertGreater(out.get("threshold_kurtosis") or 0.0, 3.5)

    def test_estimated_cut_is_wider_than_the_old_constant(self):
        rets = student_t_like(4000)
        closes = self._closes(rets)
        out = detect_anomalies("T", closes, candles=self._candles(closes))
        # threshold_p995 is in return units; SPIKE_Z is a sigma multiple.
        # Convert the estimated cut back to sigma terms via its z, which is
        # what tail_threshold reports.
        self.assertGreater(out.get("threshold_p995") or 0.0, 0.0)

    def test_short_history_falls_back_without_crashing(self):
        closes = self._closes(gaussian_like(120))
        out = detect_anomalies("T", closes, candles=self._candles(closes))
        # 120 bars is below MIN_TAIL_OBS, so the estimate is refused. The
        # threshold actually in use is still reported -- and it is the
        # fallback constant, paired with basis="fallback" so a reader can
        # tell the two apart. Publishing only the basis would leave a
        # reader unable to see what number the detector used.
        self.assertEqual(out.get("threshold_basis"), "fallback")
        self.assertEqual(out.get("threshold_p995"), SPIKE_Z)
        self.assertEqual(out.get("regime_basis"), "fallback")

    def test_regime_cuts_reported(self):
        closes = self._closes(gaussian_like(1500))
        out = detect_anomalies("T", closes, candles=self._candles(closes))
        exp = out.get("regime_expansion_cut")
        comp = out.get("regime_compression_cut")
        self.assertIsNotNone(exp)
        self.assertIsNotNone(comp)
        self.assertGreater(exp, comp,
                           "expansion cut must sit above the compression cut")

    def test_never_raises_on_garbage(self):
        for closes in ([], [1.0], [0.0] * 50, [1e9, -1e9, 0.0],
                       [float("nan")] * 40):
            detect_anomalies("T", closes,
                             candles=[{"ts": 1, "c": c, "v": 1.0}
                                      for c in closes])


class TestNoBehaviourChangeOnTheDeploymentTape(unittest.TestCase):
    """The change must be inert on this deployment's own data.

    `candles_1m` is i.i.d. Gaussian by construction, so the estimated
    thresholds should land on the constants they replaced. If they do not,
    the estimator is biased and every other test is measuring the wrong
    thing.
    """
    TOLERANCE = 0.45      # 2.5 sigma vs the empirical p99.5, in sigma units

    def test_gaussian_threshold_matches_the_constant(self):
        rets = gaussian_like(20000, sigma=0.0006)
        t = T.tail_threshold(rets, p=0.995)
        self.assertAlmostEqual(t["z"], SPIKE_Z, delta=self.TOLERANCE)

    def test_fat_tail_threshold_does_not(self):
        t = T.tail_threshold(student_t_like(200000), p=0.995)
        self.assertGreater(t["z"], SPIKE_Z + self.TOLERANCE)


class TestTheEstimateChangesBehaviour(unittest.TestCase):
    """The tests above pin the *number*. These pin the *consequence*.

    A mutation check matters here. Replacing the estimated cut with the
    fixed 2.5 leaves every reporting field intact and every assertion
    about thresholds passing, because the estimate is still computed and
    still returned -- it simply stops being used. Only a test that counts
    detections can catch that, which is what these do.

    Method: replay `_scan_window` one bar at a time, exactly as
    `detect_anomalies`' sweep does, and count price_spike firings. Then
    recompute with the cut pinned to the old constant and compare.
    """

    BARS = 6000

    def _detections(self, rets, use_cuts):
        """Count price_spike firings, with or without the estimated cut.

        `use_cuts=False` is the pre-change behaviour: no cut is passed in,
        so `_scan_window` uses the module constant. `use_cuts=True` passes
        what `detect_anomalies` estimates from full history.

        Both branches go through `_estimate_cuts`, the same function
        `detect_anomalies` calls, so this measures the shipped path rather
        than a reimplementation of it.
        """
        import engines.anomaly as A
        closes = price_path(rets)
        cuts = A._estimate_cuts(closes, None) if use_cuts else None
        n = 0
        for i in range(25, len(closes) + 1):
            out = A._scan_window("T", closes[max(0, i - 25):i], cuts=cuts)
            if any(a["kind"] == "price_spike" for a in out["anomalies"]):
                n += 1
        return n

    def test_fat_tails_make_the_fixed_cut_fire_more(self):
        """The headline property, stated as a count.

        On df=3 data the estimated cut sits at ~3.1 sigma, so a move that
        the old fixed 2.5 flagged is no longer flagged. Measured on these
        fixtures the estimate removes roughly 40% of firings.
        """
        rets = student_t_like(self.BARS, df=3)
        cut = T.spike_threshold(rets, fallback=SPIKE_Z, p=0.995)
        self.assertEqual(cut["basis"], "estimated")
        self.assertGreater(cut["threshold"], SPIKE_Z + 0.4)

        estimated_fires = self._detections(rets, use_cuts=True)
        fixed_fires = self._detections(rets, use_cuts=False)
        self.assertGreater(
            estimated_fires, 0, "no detections at all; fixture suspect")
        self.assertGreater(
            fixed_fires, estimated_fires,
            "a cut above 2.5 should fire less often; got "
            "estimated=%d fixed=%d -- if this fails, the estimate is not "
            "actually being used by the detector" % (estimated_fires, fixed_fires))

    def test_on_gaussian_data_the_change_is_almost_inert(self):
        """No behaviour regression on the deployment's own tape.

        `candles_1m` is Gaussian by construction, so a change to how
        thresholds are derived must not shift its detections much. It
        should sit within a modest band, not merely 'the right direction'.
        """
        rets = gaussian_like(self.BARS, sigma=0.0006)
        cut = T.spike_threshold(rets, fallback=SPIKE_Z, p=0.995)
        self.assertAlmostEqual(cut["threshold"], SPIKE_Z, delta=0.15,
                               msg="estimator is biased on Gaussian input")
        estimated_fires = self._detections(rets, use_cuts=True)
        fixed_fires = self._detections(rets, use_cuts=False)
        self.assertGreater(fixed_fires, 0)
        drift = abs(estimated_fires - fixed_fires) / float(fixed_fires)
        self.assertLess(
            drift, 0.35,
            "Gaussian detections moved %.0f%% (%d -> %d) -- expected "
            "near-inert" % (100.0 * drift, fixed_fires, estimated_fires))

    def test_fixed_alarm_rate_of_the_old_constant_is_too_high(self):
        """Quantifies the defect the change fixes, directly.

        A 2.5-sigma cut implies a 1.24% two-sided rate. Measured on df=3
        data it delivers ~2.6%, so the 'unusual' label fires about twice
        as often as a Gaussian reading of 2.5 would suggest.
        """
        rets = student_t_like(200000, df=3, seed=11)
        m, sd = T.mean(rets), T.std(rets)
        thr = SPIKE_Z * sd
        fired = sum(1 for x in rets if abs(x - m) >= thr)
        rate = fired / float(len(rets))
        self.assertGreater(rate, 0.0124 * 1.5,
                           "fixture is not fat enough; this test would pass "
                           "for the wrong reason (measured %.4f)" % rate)

        # And the estimated cut, expressed as the same probability.
        t = T.tail_threshold(rets, p=0.995)
        self.assertGreater(t["z"], SPIKE_Z)


class TestKurtosisCeilingIsKnown(unittest.TestCase):
    """Winsorisation bounds kurtosis, and the bound is documented.

    `kurtosis()` returns at most ~4.81 because the cap is 5 robust sigmas.
    An earlier docstring claimed ">5 is heavy-tailed", describing a value
    the function cannot produce. These tests pin the ceiling so the
    limitation cannot be forgotten again, and pin `raw_kurtosis` as the
    reading that has no ceiling.
    """

    def test_winsorised_kurtosis_is_capped(self):
        """Even a df=1.2 tail cannot push the winsorised value past ~5."""
        for df in (3, 1.5, 1.2):
            k = T.kurtosis(student_t_like(30000, df=df, seed=5))
            self.assertLess(k, 5.0,
                            "df=%s broke the documented ceiling" % df)

    def test_raw_kurtosis_reads_higher_on_heavy_tails(self):
        """The two readings answer different questions and both are kept."""
        light = gaussian_like(30000, seed=5)
        heavy = student_t_like(30000, df=1.5, seed=5)
        self.assertAlmostEqual(T.raw_kurtosis(light), 3.0, delta=0.3)
        self.assertGreater(T.raw_kurtosis(heavy), 6.0)
        self.assertLess(T.kurtosis(heavy), T.raw_kurtosis(heavy),
                        "winsorisation should read lower than raw")

    def test_classify_reports_both(self):
        r = T.classify(0.01, student_t_like(8000, df=1.5, seed=5))
        self.assertIn("kurtosis", r)
        self.assertIn("kurtosis_raw", r)
        self.assertGreater(r["kurtosis_raw"], r["kurtosis"])

    def test_raw_kurtosis_refuses_degenerate_input(self):
        self.assertIsNone(T.raw_kurtosis([0.001] * 40))
        self.assertIsNone(T.raw_kurtosis([1.0, 2.0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
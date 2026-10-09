"""Significance testing: does a backtest have an edge, or did we get lucky?

Stdlib only.

The weight here is on the *negative* cases. Every test below exists
because the failure it pins is one where the module would confidently
endorse a strategy that has no edge -- which is strictly worse than
refusing, because the number looks like evidence.

The bug that shaped this file: the statistical tests were originally fed
an *annualised* Sharpe while the null threshold was per-period. That
compared two different units and reported 300 samples of pure noise as
DSR 1.00, "edge likely real". `test_pure_noise_never_claims_an_edge` is
the direct regression for it.
"""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines import significance as sig  # noqa: E402


def _noise(n=300, seed=11, mu=0.0, sd=1.0):
    rnd = random.Random(seed)
    return [rnd.gauss(mu, sd) for _ in range(n)]


class TestSharpe(unittest.TestCase):

    def test_none_for_flat_series(self):
        # Zero dispersion has an undefined Sharpe, not an infinite one.
        self.assertIsNone(sig.sharpe([1.0] * 50))

    def test_none_for_single_observation(self):
        self.assertIsNone(sig.sharpe([1.0]))

    def test_per_period_by_default(self):
        xs = _noise(200, seed=5, mu=0.5, sd=1.0)
        per = sig.sharpe(xs)
        ann = sig.sharpe(xs, 252)
        self.assertIsNotNone(per)
        self.assertAlmostEqual(ann / per, math.sqrt(252), places=6)

    def test_annualisation_only_when_asked(self):
        xs = _noise(200, seed=6)
        self.assertAlmostEqual(sig.sharpe(xs, 252),
                               sig.sharpe(xs) * math.sqrt(252), places=9)


class TestPureNoise(unittest.TestCase):
    """The regression: noise must never be endorsed."""

    def test_pure_noise_never_claims_an_edge(self):
        # 300 draws from N(0,1) is the canonical "no edge" series.
        rep = sig.edge_report(_noise(300), cost_bps=0, n_trials=1)
        self.assertNotEqual(rep["verdict"], "edge likely real")
        self.assertLess(rep["dsr"], 0.95)

    def test_more_trials_make_noise_less_impressive(self):
        # The single most important behaviour in the module. If this is
        # flat in n_trials, the deflation is decorative.
        xs = _noise(300)
        dsrs = [sig.edge_report(xs, cost_bps=0, n_trials=t)["dsr"]
                for t in (1, 10, 100, 500)]
        for a, b in zip(dsrs, dsrs[1:]):
            self.assertLess(b, a, "DSR must fall as trials accumulate")
        self.assertLess(dsrs[-1], 0.1)

    def test_noise_with_a_lucky_streak_is_still_noise(self):
        # A right-skewed, high-kurtosis series whose sample Sharpe is
        # genuinely decent -- and which is still just noise.
        rnd = random.Random(99)
        xs = [abs(rnd.gauss(0, 1)) for _ in range(300)]
        rep = sig.edge_report(xs, cost_bps=0, n_trials=500)
        self.assertNotEqual(rep["verdict"], "edge likely real")
        if rep["dsr"] is not None:
            self.assertLess(rep["dsr"], 0.95)

    def test_pathological_skew_makes_the_test_undefined_not_wrong(self):
        # With skew ~1.6 the PSR denominator can go negative, which makes
        # the statistic undefined. Refusing ("not evaluable") is correct:
        # inventing a number there is precisely the failure mode.
        rnd = random.Random(99)
        xs = [abs(rnd.gauss(0, 1)) for _ in range(300)]
        rep = sig.edge_report(xs, cost_bps=0, n_trials=1)
        if rep["verdict"] == "not evaluable":
            self.assertTrue(rep["reasons"])
            self.assertIsNone(rep["dsr"])


class TestRealEdge(unittest.TestCase):

    def _edge(self, n=400, mu=30.0, sd=50.0, seed=3):
        rnd = random.Random(seed)
        return [rnd.gauss(mu, sd) for _ in range(n)]

    def test_genuine_edge_survives_many_trials(self):
        # Deflation must not be so punitive that it rejects real skill.
        rep = sig.edge_report(self._edge(), cost_bps=10, n_trials=500)
        self.assertGreater(rep["dsr"], 0.95)
        self.assertEqual(rep["verdict"], "edge likely real")

    def test_costs_destroy_a_grossly_profitable_strategy(self):
        xs = self._edge()
        cheap = sig.edge_report(xs, cost_bps=0, n_trials=500)
        dear = sig.edge_report(xs, cost_bps=60, n_trials=500)
        # Same gross, opposite conclusions.
        self.assertEqual(cheap["gross_sharpe"], dear["gross_sharpe"])
        self.assertGreater(cheap["net_sharpe"], dear["net_sharpe"])
        self.assertEqual(dear["verdict"], "no edge after costs")
        self.assertLess(dear["net_sharpe"], 0)

    def test_gross_and_net_are_both_reported(self):
        # A gross Sharpe alone is how a backtest talks someone into a
        # trade the venue would never fill.
        rep = sig.edge_report(self._edge(), cost_bps=20, n_trials=1)
        self.assertIsNotNone(rep["gross_sharpe"])
        self.assertIsNotNone(rep["net_sharpe"])
        self.assertLess(rep["net_sharpe"], rep["gross_sharpe"])


class TestRefusals(unittest.TestCase):

    def test_too_few_trades_refuses(self):
        rep = sig.edge_report(_noise(8), cost_bps=0, n_trials=1)
        self.assertEqual(rep["verdict"], "insufficient data")
        self.assertIsNone(rep["dsr"])
        self.assertTrue(rep["reasons"])

    def test_exactly_at_the_threshold_is_evaluated(self):
        rep = sig.edge_report(_noise(sig.MIN_OBSERVATIONS), cost_bps=0,
                              n_trials=1)
        self.assertNotEqual(rep["verdict"], "insufficient data")

    def test_zero_dispersion_refuses(self):
        rep = sig.edge_report([5.0] * 100, cost_bps=0, n_trials=1)
        self.assertIsNotNone(rep["verdict"])
        self.assertNotEqual(rep["verdict"], "edge likely real")

    def test_negative_expectancy_beats_any_sharpe(self):
        # Costs alone must dominate: a high Sharpe on a losing-expectancy
        # series is still untradeable.
        xs = [10.0 + (-1 if i % 2 else 1) * 40.0 for i in range(200)]
        rep = sig.edge_report(xs, cost_bps=100, n_trials=1)
        self.assertEqual(rep["verdict"], "no edge after costs")
        self.assertLess(rep["expectancy_bps"], 0)


class TestMaths(unittest.TestCase):

    def test_euler_gamma(self):
        self.assertAlmostEqual(sig.EULER_GAMMA, 0.5772156649, places=8)

    def test_expected_max_grows_with_trials(self):
        vals = [sig.expected_max_sharpe(1.0, n)
                for n in (1, 5, 25, 100, 1000)]
        for a, b in zip(vals, vals[1:]):
            self.assertLess(a, b)

    def test_expected_max_of_one_trial_is_zero(self):
        self.assertAlmostEqual(sig.expected_max_sharpe(1.0, 1), 0.0)

    def test_expected_max_none_for_zero_trials(self):
        self.assertIsNone(sig.expected_max_sharpe(1.0, 0))

    def test_psr_none_below_min_observations(self):
        self.assertIsNone(sig.probabilistic_sharpe(1.0, 10))

    def test_psr_monotonic_in_sharpe(self):
        vals = [sig.probabilistic_sharpe(s, 300) for s in (0.0, 0.05, 0.2)]
        for a, b in zip(vals, vals[1:]):
            self.assertLess(a, b)

    def test_deflation_never_exceeds_psr(self):
        # Deflating against a positive threshold can only lower it.
        psr = sig.probabilistic_sharpe(0.3, 300)
        dsr = sig.deflated_sharpe(0.3, 300, n_trials=100)
        self.assertLessEqual(dsr, psr)

    def test_net_returns_subtract_cost_per_trade(self):
        out = sig.net_returns([10.0, 10.0], cost_bps=4)
        self.assertEqual(out, [6.0, 6.0])

    def test_skew_kurtosis_of_normal_is_near_zero(self):
        sk, ku = sig._skew_kurtosis(_noise(4000, seed=2))
        self.assertLess(abs(sk), 0.2)
        self.assertLess(abs(ku), 0.4)

    def test_fat_tails_report_positive_excess_kurtosis(self):
        rnd = random.Random(4)
        xs = [rnd.gauss(0, 1) for _ in range(3000)]
        xs = xs + [12.0] * 3          # a few huge outliers
        _sk, ku = sig._skew_kurtosis(xs)
        self.assertGreater(ku, 0.5)


class TestTrialCounting(unittest.TestCase):

    def test_counts_strategies_times_grid(self):
        self.assertEqual(sig.count_trials(["a", "b", "c"], 4), 12)

    def test_never_zero(self):
        self.assertGreaterEqual(sig.count_trials([], 1), 1)

    def test_real_registry_is_non_trivial(self):
        from engines.signals import STRATEGIES
        n = sig.count_trials(STRATEGIES, 4)
        self.assertGreater(n, 10)


if __name__ == "__main__":
    unittest.main()

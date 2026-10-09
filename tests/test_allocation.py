"""Capital allocation: the sizing engine that is allowed to say no.

The properties under test are all refusals. A sizing engine is easy to
write so that it always produces a number; the value is entirely in the
cases where the correct number is zero.

  * a non-positive net edge produces no position
  * trading costs are subtracted in the right units, and can erase an edge
  * a small sample is scaled toward no-trade, not toward a half trade
  * correlated holdings are penalised and concentration is flagged
  * a drawdown de-risks and then halts
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from engines.allocation import (MAX_KELLY_FRACTION, allocate_book,  # noqa: E402
                                concentration_limit, confidence,
                                diversification_penalty,
                                drawdown_state, edge_per_unit_risk,
                                kelly_fraction, profit_loss_ratio,
                                risk_of_ruin, size_position, stdev,
                                win_rate)


def legs(pnls, notional=10_000.0):
    return [{"pnl": p, "notional": notional} for p in pnls]


class TestKelly(unittest.TestCase):
    def test_known_value(self):
        # p=0.6, b=2 -> f* = 0.6 - 0.4/2 = 0.4
        self.assertAlmostEqual(kelly_fraction(0.6, 2.0, cap=1.0), 0.4, places=6)

    def test_capped_at_quarter_kelly(self):
        for p, b in ((0.9, 5.0), (0.7, 3.0), (0.99, 10.0)):
            self.assertLessEqual(kelly_fraction(p, b), MAX_KELLY_FRACTION)

    def test_negative_edge_is_zero_not_negative(self):
        self.assertEqual(kelly_fraction(0.4, 1.0), 0.0)
        self.assertEqual(kelly_fraction(0.5, 1.0), 0.0)

    def test_degenerate_inputs(self):
        for p, b in ((0.0, 1.0), (1.0, 1.0), (0.5, 0.0), (0.5, -1.0)):
            self.assertEqual(kelly_fraction(p, b), 0.0)


class TestCostsAreSubtractedInTheRightUnits(unittest.TestCase):
    """Regression: costs used to be a fraction subtracted from a currency
    mean, making the term ~10,000x too small and the gate inert. A cost
    gate that cannot change its answer is not a gate."""

    def setUp(self):
        self.trades = legs([100.0] * 12 + [-50.0] * 8)

    def test_edge_falls_as_costs_rise(self):
        e0 = edge_per_unit_risk({}, self.trades, costs_bps=0)
        e10 = edge_per_unit_risk({}, self.trades, costs_bps=10)
        e60 = edge_per_unit_risk({}, self.trades, costs_bps=60)
        self.assertGreater(e0, e10)
        self.assertGreater(e10, e60)

    def test_costs_can_erase_a_positive_edge(self):
        e = edge_per_unit_risk({}, self.trades, costs_bps=60)
        self.assertLess(e, 0.0,
                        "60bps on 10k notional is 60 per trade against a "
                        "40 gross mean; the edge must go negative")
        s = size_position(100_000, 0.6, 2.0, 20, e, costs_bps=60)
        self.assertEqual(s["notional"], 0.0)
        self.assertIn("no validated edge", s["reason"])

    def test_magnitude_is_actually_material(self):
        """Guards the exact unit bug: a 10,000x error would show up as a
        negligible change in edge."""
        e0 = edge_per_unit_risk({}, self.trades, costs_bps=0)
        e25 = edge_per_unit_risk({}, self.trades, costs_bps=25)
        self.assertGreater(e0 - e25, 0.1,
                           "cost term is too small to matter -- units are wrong")

    def test_notional_scales_the_cost(self):
        """The same bps costs far more on a larger trade."""
        small = edge_per_unit_risk({}, legs([100.0] * 12 + [-50.0] * 8, 1_000.0),
                                   costs_bps=20)
        large = edge_per_unit_risk({}, legs([100.0] * 12 + [-50.0] * 8, 1_000_000.0),
                                   costs_bps=20)
        self.assertLess(large, small)

    def test_no_notional_information_is_not_treated_as_free(self):
        bare = [{"pnl": 100.0}, {"pnl": -50.0}]
        self.assertEqual(edge_per_unit_risk({}, bare, costs_bps=5), 0.0,
                         "with no notional the cost cannot be computed; "
                         "returning 0 (no edge) is the safe reading")


class TestConfidenceGatesSmallSamples(unittest.TestCase):
    def test_scales_from_zero(self):
        self.assertEqual(confidence(0), 0.0)
        self.assertEqual(confidence(5), 5 / 30.0)
        self.assertEqual(confidence(30), 1.0)
        self.assertEqual(confidence(10_000), 1.0)

    def test_small_sample_does_not_produce_a_full_size_trade(self):
        big = size_position(100_000, 0.6, 2.0, 400, edge=0.5)
        small = size_position(100_000, 0.6, 2.0, 8, edge=0.5)
        self.assertLess(small["notional"], big["notional"] / 3.0)
        self.assertGreater(small["notional"], 0.0)


class TestPositionSizingGates(unittest.TestCase):
    def test_no_equity_gives_no_position(self):
        s = size_position(0, 0.7, 3.0, 500, edge=1.0)
        self.assertEqual(s["notional"], 0.0)

    def test_no_edge_gives_no_position(self):
        s = size_position(100_000, 0.7, 3.0, 500, edge=0.0)
        self.assertEqual(s["notional"], 0.0)
        self.assertIn("no validated edge", s["reason"])

    def test_correlation_penalty_shrinks_the_trade(self):
        clean = size_position(100_000, 0.6, 2.0, 200, edge=0.5, book_corr=0.0)
        duped = size_position(100_000, 0.6, 2.0, 200, edge=0.5, book_corr=0.95)
        self.assertLess(duped["notional"], clean["notional"])

    def test_fraction_never_exceeds_the_cap(self):
        s = size_position(100_000, 0.9, 9.0, 10_000, edge=5.0,
                          max_fraction=0.25)
        self.assertLessEqual(s["fraction"], 0.25)

    def test_every_refusal_explains_itself(self):
        for kwargs in ({}, {"edge": 0.0}, {"win_p": 0.0},
                       {"n_samples": 0, "edge": 0.0}):
            args = {"equity": 100_000, "win_p": 0.6, "ratio": 2.0,
                    "n_samples": 200, "edge": 0.5}
            args.update(kwargs)
            s = size_position(**args)
            self.assertTrue(s.get("reason"), "a refusal with no reason: %r" % s)


class TestDiversification(unittest.TestCase):
    def test_penalty_only_above_threshold(self):
        self.assertEqual(diversification_penalty(0.0), 0.0)
        self.assertEqual(diversification_penalty(0.6), 0.0)
        self.assertGreater(diversification_penalty(0.95), 0.8)

    def test_penalty_is_bounded(self):
        self.assertLessEqual(diversification_penalty(1.0), 1.0)

    def test_concentration_flags_a_single_dominant_name(self):
        r = concentration_limit({"A": 80, "B": 10, "C": 10}, max_single=0.25)
        self.assertFalse(r["ok"])
        self.assertEqual(r["breaches"][0]["symbol"], "A")

    def test_balanced_book_passes(self):
        # Genuinely balanced: no name above 25% of the book. (A 30/30/40
        # book is *not* balanced under a 25% cap, whatever it looks like.)
        r = concentration_limit({"A": 25, "B": 25, "C": 25, "D": 25},
                                 max_single=0.25)
        self.assertTrue(r["ok"], r["breaches"])

    def test_three_names_at_30_each_breach_a_25_cap(self):
        r = concentration_limit({"A": 30, "B": 30, "C": 40}, max_single=0.25)
        self.assertFalse(r["ok"], "30% of the book exceeds a 25% cap")

    def test_empty_book_is_not_a_breach(self):
        self.assertTrue(concentration_limit({})["ok"])


class TestDrawdownOverlays(unittest.TestCase):
    def test_normal(self):
        d = drawdown_state([100, 105, 110, 108])
        self.assertEqual(d["state"], "normal")
        self.assertEqual(d["scale"], 1.0)

    def test_derisk_at_ten_percent(self):
        d = drawdown_state([100, 88])
        self.assertEqual(d["state"], "de-risked")
        self.assertEqual(d["scale"], 0.5)

    def test_halt_at_twenty_percent(self):
        d = drawdown_state([100, 70])
        self.assertEqual(d["state"], "halted")
        self.assertEqual(d["scale"], 0.0)

    def test_empty_curve_is_unknown_not_normal(self):
        self.assertEqual(drawdown_state([])["state"], "unknown")

    def test_drawdown_from_a_higher_peak(self):
        d = drawdown_state([100, 200, 150])
        self.assertAlmostEqual(d["dd"], 0.25, places=4)


class TestWinRateAndRatio(unittest.TestCase):
    def test_breakeven_counts_as_a_loss(self):
        wp, n = win_rate([10, 0, -5, 0, 20])
        self.assertEqual(n, 5)
        self.assertAlmostEqual(wp, 0.4, places=6)

    def test_ratio_is_mean_win_over_mean_loss(self):
        self.assertAlmostEqual(profit_loss_ratio([10, 10, -5, -5]), 2.0, places=6)

    def test_no_losses_is_unbounded_not_zero(self):
        self.assertEqual(profit_loss_ratio([5, 5, 5]), float("inf"))

    def test_no_wins_is_zero(self):
        self.assertEqual(profit_loss_ratio([-5, -5]), 0.0)

    def test_empty(self):
        self.assertEqual(win_rate([]), (0.0, 0))


class TestAllocateBook(unittest.TestCase):
    def test_deploys_nothing_without_an_edge(self):
        book = allocate_book(100_000, [
            {"symbol": "A", "win_rate": 0.6, "ratio": 2.0, "n_samples": 200,
             "edge": 0.0},
            {"symbol": "B", "win_rate": 0.4, "ratio": 1.0, "n_samples": 200,
             "edge": -0.5},
        ])
        self.assertEqual(book["invested"], 0.0)
        self.assertEqual(book["cash"], 100_000)
        self.assertEqual(book["deployed_pct"], 0.0)

    def test_refusals_are_listed_not_hidden(self):
        book = allocate_book(100_000, [
            {"symbol": "A", "win_rate": 0.6, "ratio": 2.0, "n_samples": 200,
             "edge": 0.0}])
        self.assertEqual(len(book["positions"]), 1)
        self.assertEqual(book["positions"][0]["notional"], 0.0)
        self.assertTrue(book["positions"][0]["reason"])

    def test_deploys_when_there_is_a_real_edge(self):
        book = allocate_book(100_000, [
            {"symbol": "A", "win_rate": 0.6, "ratio": 2.0, "n_samples": 300,
             "edge": 0.6, "asset_vol_pct": 1.0}])
        self.assertGreater(book["invested"], 0.0)
        self.assertLess(book["invested"], book["equity"])
        self.assertEqual(book["cash"], book["equity"] - book["invested"])

    def test_drawdown_shrinks_then_halts_deployment(self):
        args = [{"symbol": "A", "win_rate": 0.6, "ratio": 2.0,
                 "n_samples": 300, "edge": 0.6, "asset_vol_pct": 1.0}]
        healthy = allocate_book(100_000, args)
        derisked = allocate_book(100_000, args, equity_curve=[100, 88])  # -12%
        halted = allocate_book(100_000, args, equity_curve=[100, 60])    # -40%
        self.assertEqual(derisked["drawdown"]["state"], "de-risked")
        self.assertEqual(halted["drawdown"]["state"], "halted")
        self.assertGreater(healthy["invested"], derisked["invested"])
        self.assertGreater(derisked["invested"], halted["invested"])
        self.assertEqual(halted["invested"], 0.0)

    def test_cash_never_goes_negative(self):
        book = allocate_book(1_000, [
            {"symbol": "A", "win_rate": 0.9, "ratio": 9.0, "n_samples": 500,
             "edge": 5.0, "asset_vol_pct": 1.0}], max_fraction=0.25)
        self.assertGreaterEqual(book["cash"], 0.0)
        self.assertLessEqual(book["invested"], book["equity"])

    def test_empty_candidate_list(self):
        book = allocate_book(100_000, [])
        self.assertEqual(book["invested"], 0.0)
        self.assertEqual(book["positions"], [])


class TestRiskOfRuin(unittest.TestCase):
    def test_bad_edges_are_maximally_ruinous(self):
        self.assertEqual(risk_of_ruin(0.0, 2.0, 100), 1.0)
        self.assertEqual(risk_of_ruin(0.5, 0.0, 100), 1.0)

    def test_better_edge_ruins_less(self):
        weak = risk_of_ruin(0.45, 1.0, 50)
        strong = risk_of_ruin(0.60, 1.5, 50)
        self.assertLessEqual(strong, weak)
        for v in (weak, strong):
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_output(self):
        cands = [{"symbol": "A", "win_rate": 0.6, "ratio": 2.0,
                  "n_samples": 300, "edge": 0.6, "asset_vol_pct": 1.0},
                 {"symbol": "B", "win_rate": 0.55, "ratio": 1.5,
                  "n_samples": 120, "edge": 0.3, "asset_vol_pct": 2.0}]
        a = allocate_book(100_000, cands)
        b = allocate_book(100_000, cands)
        self.assertEqual(a["positions"], b["positions"])
        self.assertEqual(a["invested"], b["invested"])


if __name__ == "__main__":
    unittest.main()

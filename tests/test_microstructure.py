"""Order-flow imbalance tests. Stdlib only.

The recursion is checked against the definition's meaning rather than
against hand-computed numbers, because two of the six cases were initially
"verified" with the wrong sign by hand and passed anyway. What is asserted
here is that positive OFI means buying pressure at the touch, which is
Cont et al.'s own statement of the sign convention:

  positive iff buy-limit arrivals at the bid, sell-limit cancellations at
  the ask, and buy market orders outweigh the mirror image.

Both directions are pinned, plus the cases that must yield no number at all.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.microstructure import (depth, event_ofi, fit_slope, mid,
                                    normalize, ofi_series,
                                    ofi_series_with_prices, ofi_state)


def b(bid, ask, bs=10.0, as_=10.0):
    return {"bid": bid, "ask": ask, "bid_size": bs, "ask_size": as_}


class TestRecursion(unittest.TestCase):
    """One test per branch of dW and dV."""

    def test_depth_added_at_an_unchanged_bid_is_buying(self):
        # bid unchanged, size 10 -> 15: buyers adding to the queue.
        self.assertEqual(event_ofi(b(82500, 82502, 10, 10),
                                   b(82500, 82502, 15, 10)), 5.0)

    def test_bid_rising_adds_the_whole_new_queue(self):
        # A higher bid means the previous level-1 queue was consumed; the
        # whole new queue is buy pressure.
        self.assertEqual(event_ofi(b(82500, 82502, 10, 10),
                                   b(82501, 82502, 7, 10)), 7.0)

    def test_ask_falling_is_selling_pressure(self):
        # Ask down = sellers hitting the bid. Negative. An earlier draft of
        # this test expected +4 and would have passed an implementation that
        # inverted the sign; the sign convention is the thing worth pinning.
        self.assertEqual(event_ofi(b(82500, 82502, 10, 10),
                                   b(82500, 82501, 10, 4)), -4.0)

    def test_bid_falling_removes_the_old_queue(self):
        # Bid down = either the queue was consumed by sellers or buyers
        # cancelled it. Either way the old size leaves: -r_prev.
        self.assertEqual(event_ofi(b(82500, 82502, 10, 10),
                                   b(82499, 82502, 3, 10)), -10.0)

    def test_ask_rising_is_buying_pressure(self):
        # Ask up = buyers lifting offers, or a sell limit being cancelled.
        # Both are positive by the paper's definition.
        self.assertEqual(event_ofi(b(82500, 82502, 10, 10),
                                   b(82500, 82503, 10, 13)), 10.0)

    def test_size_removed_at_an_unchanged_ask_is_selling(self):
        # ask unchanged, size 13 -> 8: sellers pulling.
        self.assertEqual(event_ofi(b(82500, 82502, 10, 13),
                                   b(82500, 82502, 10, 8)), 5.0)

    def test_both_sides_moving_nets_out(self):
        # +10 from the bid, +4 from the ask, so 10 - 4 = 6.
        self.assertEqual(event_ofi(b(82500, 82502, 0, 0),
                                   b(82501, 82501, 10, 4)), 6.0)


class TestUnusableTransitionsYieldNoNumber(unittest.TestCase):
    """A book we cannot read has no direction.

    Encoding an unreadable transition as zero would be indistinguishable
    from "no flow occurred", and would bias every aggregate downward.
    """

    def test_crossed_next_book(self):
        self.assertIsNone(event_ofi(b(82500, 82502), b(82502, 82500)))

    def test_crossed_previous_book(self):
        self.assertIsNone(event_ofi(b(82502, 82500), b(82501, 82503)))

    def test_zero_or_negative_price(self):
        self.assertIsNone(event_ofi(b(82500, 82502), b(0, 0)))
        self.assertIsNone(event_ofi(b(82500, 82502), b(-1, 5)))

    def test_negative_size_is_refused(self):
        self.assertIsNone(event_ofi(b(82500, 82502, 10, 10),
                                    b(82500, 82502, -5, 10)))

    def test_missing_state(self):
        self.assertIsNone(event_ofi(None, b(82500, 82502)))
        self.assertIsNone(event_ofi(b(82500, 82502), None))
        self.assertIsNone(event_ofi({}, {}))

    def test_missing_sizes_read_as_zero_not_crash(self):
        """A book with no sizes is real data, not an error. It yields a
        number, because the price changes are still informative."""
        e = event_ofi({"bid": 82500, "ask": 82502},
                      {"bid": 82501, "ask": 82502})
        self.assertEqual(e, 0.0)


class TestSeries(unittest.TestCase):
    BOOKS = [b(100.00, 100.02, 10, 10),
             b(100.01, 100.03, 12, 10),
             b(100.02, 100.04, 14, 10)]

    def test_length_is_one_fewer_than_books(self):
        s = ofi_series(self.BOOKS)
        self.assertEqual(len(s), len(self.BOOKS) - 1)

    def test_pairs_carry_prices_with_flow(self):
        pairs = ofi_series_with_prices(self.BOOKS)
        self.assertEqual(len(pairs), 2)
        for e, dp in pairs:
            self.assertIsNotNone(e)
            self.assertIsNotNone(dp)
        # Books are 100.00/100.02 -> 100.01/100.03 -> 100.02/100.04, so each
        # step moves the mid by 0.01, not 0.02: mid = (bid + ask) / 2.
        self.assertAlmostEqual(pairs[0][1], 0.01, places=9)
        self.assertAlmostEqual(pairs[1][1], 0.01, places=9)

    def test_unreadable_pair_is_none_not_zero(self):
        books = [b(100.00, 100.02, 10, 10), b(100.05, 100.01, 10, 10)]
        pairs = ofi_series_with_prices(books)
        self.assertEqual(pairs[0][0], None)
        self.assertEqual(pairs[0][1], None)

    def test_empty_input(self):
        self.assertEqual(ofi_series([]), [])
        self.assertEqual(ofi_series(None), [])
        self.assertEqual(ofi_series_with_prices([]), [])

    def test_single_book_has_no_transitions(self):
        self.assertEqual(ofi_series([self.BOOKS[0]]), [])


class TestNormalisation(unittest.TestCase):
    """The slope in Cont et al. is inversely proportional to depth, so a raw
    OFI is not comparable across instruments of different size."""

    def test_divides_by_resting_size(self):
        self.assertAlmostEqual(normalize(10.0, 20.0), 0.5)

    def test_same_ofi_different_depth_different_norm(self):
        thin = normalize(100.0, 10.0)
        deep = normalize(100.0, 1000.0)
        self.assertGreater(thin, deep)

    def test_zero_depth_returns_none_not_infinity(self):
        self.assertIsNone(normalize(5.0, 0.0))
        self.assertIsNone(normalize(5.0, -1.0))

    def test_depth_uses_latest_book(self):
        books = [b(100, 100.1, 5, 5), b(100, 100.1, 50, 50)]
        self.assertEqual(depth(books), 100.0)


class TestSlopeFit(unittest.TestCase):
    """Cont et al. eq. (7): regress the change in mid on contemporaneous OFI."""

    def _pairs(self, beta=2.0, n=40, noise_seed=0):
        # Deterministic: mid change = beta * OFI exactly.
        return [(1.0 * (i + 1), beta * 1.0 * (i + 1)) for i in range(n)]

    def test_recovers_a_known_slope(self):
        f = fit_slope(self._pairs(beta=2.0))
        self.assertTrue(f["ok"])
        self.assertAlmostEqual(f["beta"], 2.0, places=6)
        self.assertAlmostEqual(f["r2"], 1.0, places=6)
        self.assertAlmostEqual(f["alpha"], 0.0, places=6)

    def test_perfect_fit_has_infinite_t_stat(self):
        """Zero residual variance means the t-stat is unbounded. Reported as
        None rather than a huge number, because a fitted line through
        noiseless points is a synthetic input, not a finding."""
        f = fit_slope(self._pairs())
        self.assertIsNone(f["beta_t"])

    def test_flat_ofi_has_no_slope(self):
        pairs = [(1.0, 0.5)] * 10
        f = fit_slope(pairs)
        self.assertFalse(f["ok"])
        self.assertIsNone(f["beta"])
        self.assertIn("never moved", f["reason"])

    def test_too_few_transitions(self):
        f = fit_slope([(1.0, 1.0), (2.0, 2.0)])
        self.assertFalse(f["ok"])
        self.assertIsNone(f["beta"])
        self.assertIn("at least 3", f["reason"])

    def test_none_entries_are_skipped_not_treated_as_zero(self):
        pairs = [(1.0, 1.0), (None, None), (2.0, 2.0), (3.0, 3.0)]
        f = fit_slope(pairs)
        self.assertTrue(f["ok"])
        self.assertEqual(f["n"], 3)

    def test_empty(self):
        f = fit_slope([])
        self.assertFalse(f["ok"])


class TestStateAndDepthGate(unittest.TestCase):
    """`ofi_state` is the function a signal calls, and it has to be willing to
    say "not enough history". This deployment stores polled top-of-book
    snapshots, so a short book is the normal case, not an edge case."""

    def _chain(self, n, start=100.0, step=0.01, depth=10.0):
        return [b(start + i * step, start + i * step + 0.02, depth, depth)
                for i in range(n)]

    def test_reports_insufficient_history_on_a_short_book(self):
        st = ofi_state(self._chain(5), min_transitions=20)
        self.assertFalse(st["ok"])
        self.assertFalse(st["sufficient_history"])
        self.assertIn("fit to noise", st["reason"])
        # but it still reports what it measured
        self.assertIsNotNone(st["ofi"])

    def test_reports_ok_on_a_long_book(self):
        st = ofi_state(self._chain(80), min_transitions=20)
        self.assertTrue(st["ok"])
        self.assertTrue(st["sufficient_history"])
        self.assertEqual(st["transitions_used"], 60)

    def test_single_book(self):
        st = ofi_state([b(100, 100.1)])
        self.assertFalse(st["ok"])
        self.assertIn("at least 2", st["reason"])

    def test_all_unreadable(self):
        st = ofi_state([b(100, 100.1), b(100.5, 100.0)], min_transitions=2)
        self.assertFalse(st["ok"])
        self.assertIn("no readable", st["reason"])

    def test_normalised_ofi_absent_without_depth(self):
        books = [b(100.0, 100.02, 0, 0) for _ in range(40)]
        st = ofi_state(books, min_transitions=5)
        # depth is zero, so normalisation must refuse rather than divide
        self.assertIsNone(st["ofi_norm"])

    def test_mid_is_reported(self):
        st = ofi_state(self._chain(40), min_transitions=5)
        self.assertAlmostEqual(st["mid"], 100.0 + 39 * 0.01 + 0.01, places=9)


class TestCatalogueSync(unittest.TestCase):
    """`book_imbalance` moved stub -> wired, and the database has to follow.

    schema.sql seeds the catalogue with INSERT OR IGNORE, so an existing
    database keeps its original row forever. When the engine gained real
    logic the database kept advertising "stub", and /api/strategies reported
    a method that did not exist. data/db._migrate now syncs mode on boot.
    """

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "db.py")
        with open(path, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_migration_syncs_mode_but_not_params(self):
        self.assertIn("UPDATE strategies SET mode", self.src)
        # params_json must not be synced: those are the user's defaults and
        # overwriting them on every boot would discard configuration.
        self.assertNotIn("params_json =", self.src.split(
            "UPDATE strategies SET mode")[1][:400])

    def test_engine_registers_it_as_wired(self):
        from engines.signals import STRATEGIES, _DISPATCH
        by_name = {s["name"]: s for s in STRATEGIES}
        self.assertEqual(by_name["book_imbalance"]["mode"], "wired")
        self.assertIn("book_imbalance", _DISPATCH)

    def test_it_stays_institutional_only(self):
        from core.tiers import can_use_strategy
        for tier in ("Free", "Pro"):
            self.assertFalse(can_use_strategy(tier, "book_imbalance"),
                             "%s can run a microstructure signal" % tier)
        for tier in ("Institutional", "Admin"):
            self.assertTrue(can_use_strategy(tier, "book_imbalance"))

    def test_signal_refuses_without_book_history(self):
        """The failure mode this exists to prevent: substituting a mid-price
        proxy for the order book and calling it OFI."""
        from engines.signals import preview_signal
        cd = [{"ts": i, "o": 100, "h": 100.1, "l": 99.9, "c": 100, "v": 1}
              for i in range(60)]
        q = {"bid": 99.99, "ask": 100.01, "last": 100.0}
        out = preview_signal("book_imbalance", cd, q, {})
        self.assertIsNone(out["side"])
        self.assertEqual(out["reason"], "no-book-history")

    def test_signal_refuses_on_a_short_book(self):
        from engines.signals import preview_signal
        cd = [{"ts": i, "o": 100, "h": 100.1, "l": 99.9, "c": 100, "v": 1}
              for i in range(60)]
        q = {"bid": 99.99, "ask": 100.01, "last": 100.0}
        short = [b(100.0 + i * 0.01, 100.02 + i * 0.01) for i in range(8)]
        out = preview_signal("book_imbalance", cd, q, {}, book=short)
        self.assertIsNone(out["side"])
        self.assertIn("fit to noise", out["reason"])

    def test_other_strategies_ignore_the_new_argument(self):
        """Passing book= must not disturb strategies that have no such
        parameter -- and must not swallow a real TypeError."""
        from engines.signals import preview_signal
        cd = [{"ts": i, "o": 100, "h": 100.1, "l": 99.9, "c": 100, "v": 1}
              for i in range(60)]
        q = {"bid": 99.99, "ask": 100.01, "last": 100.0}
        book = [b(100.0 + i * 0.01, 100.02 + i * 0.01) for i in range(40)]
        for kind, params in (("ema_trend", {"fast": 9, "slow": 21}),
                             ("dual_ema", {"fast": 9, "slow": 21}),
                             ("mean_reversion", {"period": 20, "entry_z": 2.0}),
                             ("vwap_bands", {"lookback": 20})):
            plain = preview_signal(kind, cd, q, dict(params))
            with_book = preview_signal(kind, cd, q, dict(params), book=book)
            self.assertEqual(plain["reason"], with_book["reason"],
                             "%s changed when book= was passed" % kind)


class TestProvenance(unittest.TestCase):
    """The module must not claim more than it did.

    These are string checks on purpose: the failure mode being guarded
    against is a comment that quietly upgrades "measured on 50 S&P 500
    stocks" into "works on crypto".
    """

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "engines", "microstructure.py")
        with open(path, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_names_the_paper_and_journal(self):
        self.assertIn("Cont", self.src)
        self.assertIn("Kukanov", self.src)
        self.assertIn("Stoikov", self.src)
        self.assertIn("1011.6402", self.src)

    def test_states_the_original_sample(self):
        self.assertIn("S&P 500", self.src,
                      "the sample it was measured on must be stated")

    def test_says_it_is_not_verified_on_crypto(self):
        low = self.src.lower()
        self.assertTrue("not claimed" in low or "not a law" in low)
        self.assertIn("crypto", low)

    def test_records_the_r_squared_and_significance(self):
        self.assertIn("98%", self.src)
        self.assertIn("65%", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
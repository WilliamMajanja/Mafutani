"""fx_cross book sanity, and the side pairing that depends on the operation.

Two defects, found in sequence on live kraken data.

1. The side pairing. `fx_cross` combined both legs on the *same* side --
   `ask1/ask2` and `bid1/bid2`. A cross trades opposite ways on its two legs
   for `div`, and the same way for `mul`. The same-side rule reported a
   tradeable FX dislocation on EURUSD (+82.7 bps gross, +5.11 net) when both
   real directions lost ~134 bps.

2. No book sanity check. The venue's own book was priced against whatever
   width it arrived with. USDJPY came in at a 155.8 bps spread with its ask
   1.68% above the last trade -- the ask side had nothing near the market --
   and EURJPY was broken the same way mirrored at 90.4 bps. Every other FX
   pair on the same venue was under 2 bps.

Defect 2 is why the USDJPY fixtures below no longer reach the pricing code at
all: the guard refuses them first. That is the point. The tests that check
*arithmetic* use realistically tight books so they actually reach it.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engines import arbitrage  # noqa: E402


def _book(bid, ask, venue="kraken"):
    return {"bid": bid, "ask": ask, "venue": venue,
            "bid_size": 100.0, "ask_size": 100.0, "age_s": 0}


# The live USDJPY book, verbatim: ask side empty near the market.
WIDE_B, WIDE_A = 158.510, 160.999
# Realistic kraken EURJPY, for arithmetic tests.
TIGHT = {"EURJPY": (177.790, 177.800), "USDJPY": (158.030, 158.040)}


def _usdjpy_case(bid, ask):
    """EURUSD via EURJPY / USDJPY, both legs from the given books."""
    return arbitrage.fx_cross(
        "EURUSD", _book(1.11583, 1.11589),
        [dict(_book(177.790, 177.800), symbol="EURJPY"),
         dict(_book(bid, ask), symbol="USDJPY")])


class TestWideBooksAreRefused(unittest.TestCase):
    """A book too wide to cross must not produce a number at all.

    Before this guard the scanner happily priced against USDJPY's empty ask
    side and reported an opportunity. Refusing is the honest answer: there
    was no ask to lift.
    """

    def test_live_usdjpy_book_is_refused(self):
        out = _usdjpy_case(WIDE_B, WIDE_A)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["edge_bps"],
                          "a 155.8 bps book was priced as if executable")
        self.assertIsNone(out.get("gross_bps"))

    def test_refusal_names_the_leg_and_its_width(self):
        out = _usdjpy_case(WIDE_B, WIDE_A)
        self.assertIn("too wide", out["reason"])
        self.assertIn("USDJPY", out["reason"])
        self.assertIn("155", out["reason"])

    def test_wide_books_are_reported_structurally(self):
        out = _usdjpy_case(WIDE_B, WIDE_A)
        self.assertTrue(out.get("wide_books"))
        leg = out["wide_books"][0]
        self.assertIn("USDJPY", leg["leg"])
        self.assertGreater(leg["spread_bps"], 100)

    def test_a_tight_book_is_not_refused(self):
        """The guard must not fire on real books or it is just an outage."""
        out = _usdjpy_case(*TIGHT["USDJPY"])
        self.assertTrue(out["ok"], out["reason"])
        self.assertIsNone(out.get("wide_books"))

    def test_threshold_sits_between_the_two_regimes(self):
        """Measured: healthy kraken FX runs 0.09-1.72 bps, the broken ones
        90+. The cap has to separate those, not bisect the healthy range."""
        self.assertLess(1.72, arbitrage.MAX_BOOK_SPREAD_BPS)
        self.assertGreater(90.0, arbitrage.MAX_BOOK_SPREAD_BPS)

    def test_a_wide_direct_is_refused_too(self):
        """A wide leg beside a tight direct is how a phantom appears."""
        out = arbitrage.fx_cross(
            "EURUSD", _book(1.1100, 1.1200),          # ~90 bps direct
            [dict(_book(177.790, 177.800), symbol="EURJPY"),
             dict(_book(158.030, 158.040), symbol="USDJPY")])
        self.assertFalse(out["ok"])
        self.assertIn("too wide", out["reason"])


class TestSpreadMeasurement(unittest.TestCase):
    def test_intra_book_width(self):
        self.assertAlmostEqual(arbitrage.book_spread_bps(_book(100.0, 100.01)),
                               1.0, places=4)

    def test_live_usdjpy_width(self):
        self.assertAlmostEqual(
            arbitrage.book_spread_bps(_book(WIDE_B, WIDE_A)), 155.8, places=1)

    def test_crossed_book_is_unmeasurable(self):
        self.assertIsNone(arbitrage.book_spread_bps(_book(101.0, 100.0)))

    def test_zero_sided_book_is_unmeasurable(self):
        self.assertIsNone(arbitrage.book_spread_bps(_book(0.0, 100.0)))
        self.assertIsNone(arbitrage.book_spread_bps({}))


class TestSidePairing(unittest.TestCase):
    """Arithmetic, on books tight enough to reach it.

    `div`: long buys leg 1 and sells leg 2 -- opposite sides.
    `mul`: long buys both legs -- same side, because one leg supplies what
    the other needs.

    Getting this uniformly wrong in either direction prices one operation on
    a position nobody can take.
    """

    def _cross(self, direct, l1, l2, sym1, sym2):
        return arbitrage.fx_cross(direct, _book(*l1),
                                  [dict(_book(*l2), symbol=sym1),
                                   dict(_book(*l2[::-1]), symbol=sym2)])

    def test_division_uses_opposite_sides(self):
        out = arbitrage.fx_cross(
            "EURUSD", _book(1.11583, 1.11589),
            [dict(_book(177.790, 177.800), symbol="EURJPY"),
             dict(_book(158.030, 158.040), symbol="USDJPY")])
        self.assertEqual(out["op"], "div")
        self.assertAlmostEqual(out["cross_buy"], 177.800 / 158.030, places=6)
        self.assertAlmostEqual(out["cross_sell"], 177.790 / 158.040, places=6)

    def test_multiplication_uses_the_same_side(self):
        out = arbitrage.fx_cross(
            "EURUSD", _book(1.11583, 1.11589),
            [dict(_book(0.85000, 0.85010), symbol="EURGBP"),
             dict(_book(1.31100, 1.31110), symbol="GBPUSD")])
        self.assertEqual(out["op"], "mul")
        self.assertAlmostEqual(out["cross_buy"], 0.85010 * 1.31110, places=6)
        self.assertAlmostEqual(out["cross_sell"], 0.85000 * 1.31100, places=6)

    def test_the_two_operations_differ(self):
        """The regression this whole fix turned on."""
        div = arbitrage.fx_cross(
            "EURUSD", _book(1.11583, 1.11589),
            [dict(_book(177.790, 177.800), symbol="EURJPY"),
             dict(_book(158.030, 158.040), symbol="USDJPY")])
        mul = arbitrage.fx_cross(
            "EURUSD", _book(1.11583, 1.11589),
            [dict(_book(0.85000, 0.85010), symbol="EURGBP"),
             dict(_book(1.31100, 1.31110), symbol="GBPUSD")])
        self.assertNotAlmostEqual(div["cross_buy"], mul["cross_buy"], places=3)


class TestThePhantomIsGone(unittest.TestCase):
    """The measured case that started this, on books that pass the guard."""

    def test_consistent_books_report_no_edge(self):
        b1, a1 = 177.790, 177.800
        b2, a2 = 158.030, 158.040
        mid = ((b1 + a1) / 2.0) / ((b2 + a2) / 2.0)
        direct = mid * 1.0002      # direct slightly rich, as live books are
        out = arbitrage.fx_cross("EURUSD", _book(direct * 0.99999, direct),
                                 [dict(_book(b1, a1), symbol="EURJPY"),
                                  dict(_book(b2, a2), symbol="USDJPY")])
        self.assertTrue(out["ok"], out["reason"])
        self.assertLess(out["edge_bps"], 0)
        self.assertNotEqual(out["reason"], "cross is dislocated")

    def test_both_directions_lose(self):
        b1, a1, b2, a2 = 177.790, 177.800, 158.030, 158.040
        long_cross, short_cross = a1 / b2, b1 / a2
        direct = ((b1 + a1) / 2.0) / ((b2 + a2) / 2.0)
        a = (direct - long_cross) / long_cross * 10000.0
        b = (short_cross - direct) / direct * 10000.0
        self.assertLess(a, 0)
        self.assertLess(b, 0)


class TestCrossVenueBookSanity(unittest.TestCase):
    """The same guard on the model that found the original edge."""

    def test_wide_venue_book_is_refused(self):
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.00, 100.01, "binance"),
            "kraken": _book(158.510, 160.999, "kraken"),
        })
        self.assertFalse(out["ok"])
        self.assertIsNone(out["edge_bps"])
        self.assertIn("too wide", out["reason"])

    def test_refusal_names_the_offending_venue(self):
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.00, 100.01, "binance"),
            "kraken": _book(158.510, 160.999, "kraken"),
        })
        self.assertIn("kraken", out["reason"])

    def test_healthy_venues_still_price(self):
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.00, 100.01, "binance"),
            "kraken": _book(100.15, 100.16, "kraken"),
        })
        self.assertTrue(out["ok"], out["reason"])
        self.assertGreater(out["gross_bps"], 0)

    def test_a_genuine_cross_venue_gap_is_not_mistaken_for_a_wide_book(self):
        """The guard is intra-book width. A real dislocation between two
        tight books must still be found -- conflating the two would silence
        the model entirely."""
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.00, 100.01, "binance"),
            "kraken": _book(105.00, 105.01, "kraken"),
        })
        self.assertTrue(out["ok"], out["reason"])
        self.assertGreater(out["gross_bps"], 490,
                           "a genuine 5%% cross-venue gap was suppressed")


class TestConfiguredCrossesStillBehave(unittest.TestCase):
    """All four shipped crosses, priced with internally consistent books."""

    CASES = [
        ("EURUSD", "EURGBP", "GBPUSD", (1.11583, 1.11589, 0.85110, 0.85115, 1.31102, 1.31108)),
        ("EURUSD", "EURJPY", "USDJPY", (1.11583, 1.11589, 175.43, 175.51, 157.20, 157.30)),
        ("EURGBP", "EURUSD", "GBPUSD", (0.85110, 0.85115, 1.11583, 1.11589, 1.31102, 1.31108)),
        ("EURJPY", "EURUSD", "USDJPY", (175.43, 175.51, 1.11583, 1.11589, 157.20, 157.30)),
    ]

    def _price(self, case):
        direct, l1, l2, (db, da, b1, a1, b2, a2) = case
        return direct, arbitrage.fx_cross(
            direct, _book(db, da),
            [dict(_book(b1, a1), symbol=l1), dict(_book(b2, a2), symbol=l2)])

    def test_every_configured_cross_is_covered(self):
        configured = {(d, a, b) for d, (a, b) in arbitrage.FX_CROSSES}
        self.assertEqual(configured, {(d, a, b) for d, a, b, _ in self.CASES})

    def test_all_price_without_an_edge(self):
        for direct, out in map(self._price, self.CASES):
            self.assertTrue(out.get("ok"), "%s: %s" % (direct, out.get("reason")))
            self.assertLess(out["edge_bps"], 0, "%s reported an edge" % direct)


class TestRefusalsStillHold(unittest.TestCase):
    def test_non_composing_legs_refused(self):
        out = arbitrage.fx_cross(
            "EURGBP", _book(0.85110, 0.85115),
            [dict(_book(177.790, 177.800), symbol="EURJPY"),
             dict(_book(158.030, 158.040), symbol="USDJPY")])
        self.assertFalse(out["ok"])
        self.assertIn("does not compose", out["reason"])

    def test_synthetic_leg_refused(self):
        out = arbitrage.fx_cross(
            "EURUSD", _book(1.11583, 1.11589, "synthetic"),
            [dict(_book(177.790, 177.800), symbol="EURJPY"),
             dict(_book(158.030, 158.040), symbol="USDJPY")])
        self.assertFalse(out["ok"])
        self.assertIn("synthetic", out["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Venue attribution: does a cross-venue gap survive dropping a venue?

A cross-venue gap is a claim about two venues, which means it can be an
artefact of one of them. Flowerday, Gandal, Halaburda, Olson & Ardel,
"Bitcoin arbitrage: The role of an exchange" (Quantitative Finance and
Economics 10(3):624-648, 2026) find that ~80% of hours with measured
cross-exchange dispersion include Bitfinex, and that excluding it drops the
mispricing/volatility correlation from 0.86 to 0.59 and the restricted mean
episode duration from 11.1 to 3.6 minutes -- one venue carrying most of the
apparent signal.

These tests pin the three shapes that matter: one venue driving the gap, the
gap being dispersion within the book, and no gap at all. The third is the
one that is easy to get wrong, because a naive implementation reports
"robust" when there was nothing to be robust about.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.arbitrage import cross_venue, scan_crypto, venue_attribution


def book(bid, ask, bs=5.0, as_=5.0):
    return {"bid": bid, "ask": ask, "bid_size": bs, "ask_size": as_}


class TestOneVenueDrivesTheGap(unittest.TestCase):
    """The Flowerday case, reproduced. This is the test that matters."""

    BOOKS = {
        "venue1":  book(82500.0, 82502.0, 5, 5),
        "coinbase": book(82501.0, 82503.0, 4, 4),
        "kraken":   book(83500.0, 83510.0, 1, 1),
    }

    def setUp(self):
        self.attr = venue_attribution("BTCUSD", dict(self.BOOKS))

    def test_the_base_case_looks_profitable(self):
        """Establishes what is being caught: before attribution this gap
        reads as a genuine 36 bps profit after fees."""
        base = cross_venue("BTCUSD", dict(self.BOOKS))
        self.assertGreater(base["edge_bps"], 0)
        self.assertEqual(base["sell_venue"], "kraken")

    def test_attribution_names_the_venue(self):
        self.assertTrue(self.attr["testable"])
        self.assertFalse(self.attr["robust"])
        self.assertEqual(self.attr["driver"], "kraken")
        self.assertTrue(self.attr["driver_is_venue"])

    def test_dropping_the_driver_leaves_no_gap(self):
        kraken = [r for r in self.attr["venues"] if r["dropped"] == "kraken"][0]
        self.assertTrue(kraken["collapses"])
        self.assertIsNone(kraken["gross_bps"],
                          "a gap survived removing the venue that caused it")

    def test_dropping_an_innocent_venue_changes_nothing(self):
        """Attribution must not accuse the wrong venue. Venue1 and Coinbase
        agree, so removing either leaves the kraken gap intact."""
        for venue in ("venue1", "coinbase"):
            row = [r for r in self.attr["venues"] if r["dropped"] == venue][0]
            self.assertFalse(row["collapses"], "%s wrongly blamed" % venue)
            self.assertGreater(row["retained_fraction"], 0.9)

    def test_reason_names_the_venue(self):
        self.assertIn("kraken", self.attr["reason"])


class TestGapIsTheBooksOwnDispersion(unittest.TestCase):
    """Symmetric outlier either side: no single venue is 'the driver'."""

    BOOKS = {
        "venue1":  book(82500.0, 82502.0),
        "coinbase": book(82580.0, 82582.0),
        "kraken":   book(82540.0, 82542.0),
    }

    def test_two_venues_collapse_so_no_single_driver(self):
        attr = venue_attribution("BTCUSD", dict(self.BOOKS))
        self.assertFalse(attr["robust"])
        self.assertIsNone(attr["driver"],
                          "named a driver when both extremes collapse")
        self.assertFalse(attr["driver_is_venue"])
        collapsed = [r["dropped"] for r in attr["venues"] if r["collapses"]]
        self.assertEqual(len(collapsed), 2)


class TestNoGapToAttribute(unittest.TestCase):
    """The vacuous-verdict trap.

    An early version reported robust=True whenever no dropout collapsed,
    which on a set of venues that all agree means "the gap survived every
    dropout" -- said about a gap that does not exist. A robustness verdict
    about a non-existent effect is not a weak result, it is a fabricated
    one.
    """

    AGREE = {
        "venue1":  book(82500.0, 82502.0),
        "coinbase": book(82501.0, 82503.0),
        "kraken":   book(82499.0, 82501.5),
    }

    def test_no_gap_means_no_verdict(self):
        attr = venue_attribution("BTCUSD", dict(self.AGREE))
        self.assertTrue(attr["testable"])
        self.assertIsNone(attr["robust"])
        self.assertIn("no dislocation to attribute", attr["reason"])

    def test_base_cross_venue_has_no_edge_either(self):
        base = cross_venue("BTCUSD", dict(self.AGREE))
        self.assertIsNone(base["gross_bps"])


class TestTooFewVenues(unittest.TestCase):
    """Attribution needs three venues. With two, removing either leaves one
    book and nothing to compare, so a verdict would be meaningless."""

    def test_two_venues_is_untestable_not_robust(self):
        books = {"venue1": book(82500.0, 82502.0),
                 "kraken": book(82501.0, 82503.0)}
        attr = venue_attribution("BTCUSD", books)
        self.assertFalse(attr["testable"])
        self.assertIsNone(attr["robust"])
        self.assertEqual(attr["venue_count"], 2)
        self.assertIn("3+ venues", attr["reason"])

    def test_one_venue_is_untestable(self):
        attr = venue_attribution("BTCUSD", {"venue1": book(1.0, 1.1)})
        self.assertFalse(attr["testable"])
        self.assertIsNone(attr["robust"])

    def test_no_books_does_not_raise(self):
        attr = venue_attribution("BTCUSD", {})
        self.assertFalse(attr["testable"])
        self.assertIsNone(attr["robust"])


class TestUnusableBooksAreIgnored(unittest.TestCase):
    """A crossed or zero book is not a venue. Counting one would push a
    two-venue book over the three-venue threshold and produce a verdict
    from a test that never really ran."""

    def test_crossed_book_does_not_count_as_a_venue(self):
        books = {
            "venue1":  book(82500.0, 82502.0),
            "coinbase": book(82501.0, 82503.0),
            "kraken":   book(82510.0, 82500.0),      # ask < bid: crossed
        }
        attr = venue_attribution("BTCUSD", books)
        self.assertEqual(attr["venue_count"], 2)
        self.assertFalse(attr["testable"])

    def test_zero_priced_book_does_not_count(self):
        books = {
            "venue1":  book(82500.0, 82502.0),
            "coinbase": book(82501.0, 82503.0),
            "kraken":   book(0.0, 0.0),
        }
        attr = venue_attribution("BTCUSD", books)
        self.assertEqual(attr["venue_count"], 2)
        self.assertFalse(attr["testable"])


class TestRetentionComparisonIsOnGross(unittest.TestCase):
    """Each dropout is scored on gross dislocation, not net edge.

    Net edge is already negative after ~36-70 bps of fees in almost every
    case, so comparing net edges would show every dropout as unchanged and
    hide precisely the collapse being looked for.
    """

    def test_gross_is_reported_when_net_is_negative(self):
        attr = venue_attribution("BTCUSD", TestGapIsTheBooksOwnDispersion.BOOKS)
        for row in attr["venues"]:
            self.assertIsNotNone(row["gross_bps"])
            # net is deeply negative for all of these
            self.assertTrue(row["edge_bps"] is None or row["edge_bps"] <= 0)
        self.assertTrue(any(r["collapses"] for r in attr["venues"]))


class TestScanIntegration(unittest.TestCase):
    def test_attribute_defaults_off(self):
        rows = scan_crypto(["BTCUSD"],
                           {"BTCUSD": {"venue1": book(1.0, 1.1),
                                       "kraken": book(1.2, 1.3)}})
        self.assertEqual(len(rows), 1)
        self.assertNotIn("attribution", rows[0])

    def test_attribute_on_adds_the_block(self):
        rows = scan_crypto(["BTCUSD"],
                           {"BTCUSD": {"venue1": book(1.0, 1.1),
                                       "coinbase": book(1.05, 1.15),
                                       "kraken": book(1.2, 1.3)}},
                           attribute=True)
        self.assertIn("attribution", rows[0])
        self.assertTrue(rows[0]["attribution"]["testable"])

    def test_single_venue_symbol_is_skipped(self):
        rows = scan_crypto(["BTCUSD"], {"BTCUSD": {"venue1": book(1.0, 1.1)}},
                           attribute=True)
        self.assertEqual(rows, [])


class TestAttributionNeverClaimsAnEdge(unittest.TestCase):
    """Attribution is diagnostic. It must not manufacture or remove an
    opportunity -- cross_venue owns that judgement and stays the single
    source of truth for `ok` and `edge_bps`."""

    def test_edge_is_identical_with_and_without_attribution(self):
        books = {"BTCUSD": TestOneVenueDrivesTheGap.BOOKS}
        plain = scan_crypto(["BTCUSD"], books)[0]
        with_attr = scan_crypto(["BTCUSD"], books, attribute=True)[0]
        self.assertEqual(plain["edge_bps"], with_attr["edge_bps"])
        self.assertEqual(plain["ok"], with_attr["ok"])
        self.assertEqual(plain["gross_bps"], with_attr["gross_bps"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
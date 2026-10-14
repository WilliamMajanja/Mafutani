"""Arbitrage engine, venue registry and scanner tests. Stdlib only.

The bias in these tests is toward *refusal*: the models must decline to
price an edge far more often than they produce one. A scanner that
confidently reports opportunities that the fees already ate is worse
than one that reports nothing, so the negative cases carry the weight.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import venuebooks  # noqa: E402
from engines import arbitrage  # noqa: E402


def _book(bid, ask, venue="binance", bid_size=1.0, ask_size=1.0):
    return {"bid": bid, "ask": ask, "venue": venue,
            "bid_size": bid_size, "ask_size": ask_size}


def _leg(symbol, bid, ask, venue="kraken", bid_size=1.0, ask_size=1.0):
    """An FX leg: a book that also names itself.

    fx_cross derives multiply-vs-divide from the currency codes, so an
    unlabelled leg cannot be priced at all.
    """
    row = _book(bid, ask, venue, bid_size, ask_size)
    row["symbol"] = symbol
    return row


class TestCrossVenue(unittest.TestCase):

    def test_prices_edge_net_of_two_legs(self):
        # 12 bps gross, ~14 bps of fees (10 + a cheap venue).
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.00, 100.01, "binance"),
            "kraken": _book(100.15, 100.16, "kraken"),
        })
        self.assertTrue(out["ok"])
        self.assertEqual(out["buy_venue"], "binance")
        self.assertEqual(out["sell_venue"], "kraken")
        # gross 15 bps, cost 36 bps -> negative. Must be reported as such.
        self.assertLess(out["edge_bps"], 0)
        self.assertEqual(out["gross_bps"] - out["cost_bps"], out["edge_bps"])

    def test_profitable_when_spread_beats_fees(self):
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.00, 100.01, "binance"),
            "kraken": _book(105.00, 105.01, "kraken"),
        })
        self.assertTrue(out["ok"])
        self.assertGreater(out["edge_bps"], 0)

    def test_refuses_single_venue(self):
        out = arbitrage.cross_venue("BTCUSD", {"binance": _book(99, 100)})
        self.assertFalse(out["ok"])
        self.assertIsNone(out["edge_bps"])
        self.assertIn("2+ live venues", out["reason"])

    def test_refuses_when_no_dislocation(self):
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(99.9, 100.0),
            "kraken": _book(99.8, 99.95),
        })
        self.assertFalse(out["ok"])
        self.assertIn("no dislocation", out["reason"])

    def test_never_same_venue_both_legs(self):
        # Two venues, but the cheapest ask and dearest bid are the same
        # book. Paying a venue its own spread is not an arbitrage.
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.0, 100.0),
            "kraken": _book(100.0, 100.0),
        })
        self.assertFalse(out["ok"])
        self.assertIn("only one venue is live", out["reason"])

    def test_ignores_crossed_books(self):
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(101.0, 100.0),      # ask < bid: garbage
            "kraken": _book(100.00, 100.01),
        })
        self.assertFalse(out["ok"])

    def test_notional_capped_by_thinner_book(self):
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.00, 100.01, "binance", ask_size=2.0),
            "kraken": _book(105.00, 105.01, "kraken", bid_size=0.5),
        })
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["max_size"], 0.5, places=6)

    def test_explicit_size_caps_notional(self):
        out = arbitrage.cross_venue("BTCUSD", {
            "binance": _book(100.00, 100.01, "binance", ask_size=5.0),
            "kraken": _book(105.00, 105.01, "kraken", bid_size=5.0),
        }, size=1.0)
        self.assertTrue(out["ok"])
        self.assertLessEqual(out["notional"], 1.0)


class TestTriangular(unittest.TestCase):

    def test_profitable_cycle_reports_factor(self):
        books = {
            "BTCUSDT": {"bid": 65000.0, "ask": 65010.0},
            "ETHUSDT": {"bid": 3500.0, "ask": 3510.0},
            "USDT": {"bid": 0.9990, "ask": 1.0010},
        }
        out = arbitrage.triangular(("BTCUSDT", "ETHUSDT", "USDT"), books,
                                   venue="binance")
        self.assertTrue(out["ok"], out["reason"])
        self.assertEqual(len(out["legs"]), 3)
        self.assertIsNotNone(out["profit_factor"])

    def test_bare_books_need_a_venue_to_be_costed(self):
        # A book with no venue cannot be priced: charging it zero would
        # invent a free three-leg cycle out of nowhere.
        books = {"A": {"bid": 1.0, "ask": 1.0},
                 "B": {"bid": 1.0, "ask": 1.0},
                 "C": {"bid": 1.0, "ask": 1.0}}
        out = arbitrage.triangular(("A", "B", "C"), books)
        self.assertFalse(out["ok"])
        self.assertIn("synthetic", out["reason"])

    def test_nested_books_carry_their_own_venue(self):
        books = {
            "A": {"binance": {"bid": 1.0, "ask": 1.0}},
            "B": {"coinbase": {"bid": 1.1, "ask": 1.1}},
            "C": {"binance": {"bid": 1.0, "ask": 1.0}},
        }
        out = arbitrage.triangular(("A", "B", "C"), books)
        self.assertTrue(out["ok"], out["reason"])
        self.assertEqual(out["legs"][0]["venue"], "binance")
        self.assertEqual(out["legs"][1]["venue"], "coinbase")

    def test_refuses_duplicate_symbols(self):
        out = arbitrage.triangular(("A", "A", "B"), {"A": {"bid": 1, "ask": 1},
                                                      "B": {"bid": 1, "ask": 1}})
        self.assertFalse(out["ok"])
        self.assertIn("3 distinct", out["reason"])

    def test_refuses_missing_book(self):
        out = arbitrage.triangular(("A", "B", "C"),
                                   {"A": {"bid": 1, "ask": 1}})
        self.assertFalse(out["ok"])
        self.assertIsNotNone(out["reason"])

    def test_refuses_synthetic_leg(self):
        books = {"A": {"bid": 1.0, "ask": 1.0, "venue": "synthetic"},
                 "B": {"bid": 1.0, "ask": 1.0, "venue": "binance"},
                 "C": {"bid": 1.0, "ask": 1.0, "venue": "binance"}}
        out = arbitrage.triangular(("A", "B", "C"), books)
        self.assertFalse(out["ok"])
        self.assertIn("synthetic", out["reason"])

    def test_flat_market_is_not_profitable(self):
        # Identical books around 1.0: the cycle must not invent an edge.
        books = {"A": {"bid": 1.0, "ask": 1.0},
                 "B": {"bid": 1.0, "ask": 1.0},
                 "C": {"bid": 1.0, "ask": 1.0}}
        out = arbitrage.triangular(("A", "B", "C"), books, venue="binance")
        self.assertTrue(out["ok"], out["reason"])
        self.assertLessEqual(out["edge_bps"], 0)


class TestFxCross(unittest.TestCase):
    # Legs must carry their symbol: the multiply-vs-divide decision is
    # derived from currency codes, so an unlabelled leg cannot be priced.

    def test_consistent_cross_has_no_edge(self):
        # EURGBP x GBPUSD == EURUSD exactly (0.85 * 1.27 = 1.0795). Any
        # edge here would be an artefact of rounding, so this pins that
        # a perfectly consistent cross prices as *not* profitable.
        out = arbitrage.fx_cross("EURUSD", _book(1.0795, 1.0795, "kraken"),
                                 [_leg("EURGBP", 0.8500, 0.8500),
                                  _leg("GBPUSD", 1.2700, 1.2700)])
        self.assertTrue(out["ok"], out["reason"])
        self.assertEqual(out["op"], "mul")
        self.assertLessEqual(out["edge_bps"], 0)

    def test_cost_is_pure_taker_fees(self):
        # Every FX venue is now tradeable, so cost is exactly the three
        # legs' taker fees and nothing else. Verified live: kraken's real
        # books clear no special treatment.
        out = arbitrage.fx_cross("EURUSD", _book(1.1000, 1.1000, "kraken"),
                                 [_leg("EURGBP", 0.8500, 0.8500),
                                  _leg("GBPUSD", 1.2700, 1.2700)])
        self.assertTrue(out["ok"], out["reason"])
        self.assertEqual(out["cost_bps"], 3 * venuebooks.fee_bps("kraken"))

    def test_synthetic_direct_book_refused(self):
        # The direct leg is a venue too; a synthetic direct against real
        # legs prices an edge against a number that does not exist.
        out = arbitrage.fx_cross("EURUSD", _book(1.10, 1.10, "synthetic"),
                                 [_leg("EURGBP", 0.85, 0.85),
                                  _leg("GBPUSD", 1.27, 1.27)])
        self.assertFalse(out["ok"])
        self.assertIn("synthetic", out["reason"])

    def test_dislocated_cross_reports_direction(self):
        # Direct at 1.10 against a synthetic cross near 1.08: ~185 bps
        # gross, well clear of three legs of execution cost.
        out = arbitrage.fx_cross("EURUSD", _book(1.1000, 1.1000, "kraken"),
                                 [_leg("EURGBP", 0.8500, 0.8500),
                                  _leg("GBPUSD", 1.2700, 1.2700)])
        self.assertTrue(out["ok"], out["reason"])
        self.assertGreater(out["edge_bps"], 0)
        self.assertEqual(out["direction"], "buy_cross_sell_direct")

    def test_reverse_direction_detected(self):
        # Cross dear, direct cheap -> the profitable trade flips.
        out = arbitrage.fx_cross("EURUSD", _book(1.0000, 1.0000, "kraken"),
                                 [_leg("EURGBP", 0.8500, 0.8500),
                                  _leg("GBPUSD", 1.2700, 1.2700)])
        self.assertTrue(out["ok"], out["reason"])
        self.assertEqual(out["direction"], "buy_direct_sell_cross")

    def test_requires_two_legs(self):
        out = arbitrage.fx_cross("EURUSD", _book(1.08, 1.08),
                                 [_leg("EURGBP", 0.85, 0.85)])
        self.assertFalse(out["ok"])

    def test_refuses_synthetic_legs(self):
        out = arbitrage.fx_cross("EURUSD", _book(1.08, 1.08, "kraken"),
                                 [_leg("EURGBP", 0.85, 0.85, "synthetic"),
                                  _leg("GBPUSD", 1.27, 1.27)])
        self.assertFalse(out["ok"])
        self.assertIn("synthetic", out["reason"])

    def test_unlabelled_leg_refused(self):
        # No symbol means no currency arithmetic, so no honest price.
        out = arbitrage.fx_cross("EURUSD", _book(1.08, 1.08, "kraken"),
                                 [_book(0.85, 0.85, "kraken"),
                                  _book(1.27, 1.27, "kraken")])
        self.assertFalse(out["ok"])
        self.assertIn("does not compose", out["reason"])


class TestSpread(unittest.TestCase):

    def test_ratio_spread_priced(self):
        out = arbitrage.spread("XAUUSD", _book(2400.0, 2401.0, "kraken"),
                               "XAGUSD", _book(28.4, 28.5, "kraken"),
                               label="Gold / Silver")
        self.assertTrue(out["ok"])
        self.assertIsNotNone(out["value"])

    def test_zscore_from_history(self):
        hist = {"XAUUSD": [2400.0 + i for i in range(60)],
                "XAGUSD": [28.0 + i * 0.1 for i in range(60)]}
        out = arbitrage.spread("XAUUSD", _book(2400.0, 2401.0, "kraken"),
                               "XAGUSD", _book(28.4, 28.5, "kraken"),
                               history=hist)
        self.assertTrue(out["ok"])
        self.assertIsNotNone(out["z_score"])

    def test_short_history_yields_no_zscore(self):
        hist = {"XAUUSD": [2400.0, 2401.0], "XAGUSD": [28.0, 28.1]}
        out = arbitrage.spread("XAUUSD", _book(2400.0, 2401.0, "kraken"),
                               "XAGUSD", _book(28.4, 28.5, "kraken"),
                               history=hist)
        self.assertIsNone(out["z_score"])

    def test_difference_kind_for_cracks(self):
        out = arbitrage.spread("BRENT", _book(85.0, 85.2, "kraken"),
                               "WTI", _book(80.0, 80.2, "kraken"),
                               kind="difference", label="Brent - WTI crack")
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["value"], 4.8, places=3)


class TestKrakenParser(unittest.TestCase):
    """The two response quirks kraken actually has."""

    def _json(self, payload):
        import json
        return json.dumps(payload)

    def test_resolves_venue_side_keyed_result(self):
        # EURUSD comes back as ZEURZUSD, EURGBP as plain EURGBP.
        from data import feeds
        raw = self._json({"error": [], "result": {"ZEURZUSD": {
            "a": ["1.12312", "4699", "4699.000"],
            "b": ["1.12309", "71", "71.000"],
            "c": ["1.12309", "95.05", "95.050"]}}})
        q = feeds._parse_kraken_ticker(raw, "EURUSD")
        self.assertAlmostEqual(q["bid"], 1.12309, places=5)
        self.assertAlmostEqual(q["ask"], 1.12312, places=5)
        self.assertAlmostEqual(q["last"], 1.12309, places=5)
        # Lot sizes come from index 2, not the whole-lot count at index 1.
        self.assertAlmostEqual(q["bid_size"], 71.0, places=3)

    def test_unknown_pair_raises_not_silent_empty_book(self):
        # kraken returns HTTP 200 with an error array. A parser that
        # only checked for a missing key would report an absent venue as
        # a venue quoting nothing.
        from data import feeds
        raw = self._json({"error": ["EQuery:Unknown asset pair"],
                          "result": {}})
        with self.assertRaises(ValueError) as ctx:
            feeds._parse_kraken_ticker(raw, "GBPJPY")
        self.assertIn("GBPJPY", str(ctx.exception))

    def test_one_sided_book_refused(self):
        from data import feeds
        raw = self._json({"error": [], "result": {"X": {
            "a": ["1.0", "1", "1"], "b": [], "c": ["1.0", "1", "1"]}}})
        with self.assertRaises(ValueError):
            feeds._parse_kraken_ticker(raw, "X")

    def test_ambiguous_multi_result_refused(self):
        # Two books back and neither is the pair we asked for. Taking
        # result[0] here would price a scan off an unrelated instrument.
        from data import feeds
        raw = self._json({"error": [], "result": {
            "X": {"a": ["1", "1", "1"], "b": ["1", "1", "1"]},
            "Y": {"a": ["2", "1", "1"], "b": ["2", "1", "1"]}}})
        with self.assertRaises(ValueError):
            feeds._parse_kraken_ticker(raw, "Z")


class TestExpectancy(unittest.TestCase):
    """Expectancy, not just edge width.

    From SSRN 2695405 (speculative futures under mean reversion): a
    spread can be wide, mean-reverting, and still lose money once costs and
    a stop are applied. These tests pin that the model refuses rather than
    producing an optimistic number it cannot support.
    """

    def _mr_series(self, n=200, half_life=20.0, seed=7):
        """A genuinely mean-reverting log-ratio series."""
        import math
        import random
        rng = random.Random(seed)
        theta = math.log(2.0) / half_life
        sigma = 0.01
        lvl, out = 0.0, []
        for _ in range(n):
            lvl += -theta * lvl + sigma * rng.gauss(0, 1)
            out.append(math.exp(lvl))
        return out

    def test_half_life_is_recovered(self):
        from engines.arbitrage import _ar1_rho, _half_life_from_rho
        import math
        s = self._mr_series(half_life=25.0)
        diffs = [s[i] - s[i - 1] for i in range(1, len(s))]
        rho = _ar1_rho(diffs)
        hl = _half_life_from_rho(rho)
        self.assertIsNotNone(hl)
        # Recovered half-life should be the right order of magnitude.
        self.assertGreater(hl, 5.0)
        self.assertLess(hl, 120.0)

    def test_trending_series_has_no_half_life(self):
        from engines.arbitrage import _ar1_rho, _half_life_from_rho
        trending = [1.0 + i * 0.01 for i in range(200)]
        diffs = [trending[i] - trending[i - 1] for i in range(1, len(trending))]
        rho = _ar1_rho(diffs)
        self.assertIsNone(_half_life_from_rho(rho))

    def test_too_short_history_refuses(self):
        from engines.arbitrage import _expectancy
        out = _expectancy([1.0] * 5, [1.0] * 5, 1.0, 10.0)
        self.assertIsNone(out["expectancy_bps"])
        self.assertIn("30", out["note"])

    def test_non_reverting_series_refuses(self):
        from engines.arbitrage import _expectancy
        a = [1.0 + i * 0.01 for i in range(200)]
        b = [1.0] * 200
        out = _expectancy(a, b, 1.0, 10.0)
        self.assertIsNone(out["expectancy_bps"])
        self.assertIn("mean-revert", out["note"])

    def test_expectancy_present_for_reverting_series(self):
        from engines.arbitrage import _expectancy
        a = self._mr_series()
        b = [1.0] * len(a)
        # Displace the current ratio to 2 sigma so there is a trade.
        import math
        out = _expectancy(a, b, math.exp(0.02), 10.0)
        self.assertIsNotNone(out["expectancy_bps"])
        self.assertIsNotNone(out["half_life"])
        self.assertIn(out["verdict"],
                      ("viable", "marginal", "not viable after costs and stop"))
        self.assertGreaterEqual(out["win_prob"], 0.0)
        self.assertLessEqual(out["win_prob"], 1.0)

    def test_costs_reduce_expectancy(self):
        from engines.arbitrage import _expectancy
        import math
        a = self._mr_series()
        b = [1.0] * len(a)
        cheap = _expectancy(a, b, math.exp(0.02), 5.0)
        dear = _expectancy(a, b, math.exp(0.02), 500.0)
        self.assertGreater(cheap["expectancy_bps"], dear["expectancy_bps"])

    def test_spread_exposes_expectancy(self):
        a = self._mr_series()
        b = [x * 84.0 for x in a]           # a stable gold/silver-like ratio
        out = arbitrage.spread(
            "XAUUSD", _book(2400.0, 2401.0, "kraken"),
            "XAGUSD", _book(28.4, 28.5, "kraken"),
            history={"XAUUSD": a, "XAGUSD": b})
        self.assertTrue(out["ok"], out["reason"])
        self.assertIn("expectancy", out)


class TestVenueRegistry(unittest.TestCase):

    def test_crypto_has_two_real_venues(self):
        for sym in ("BTCUSD", "ETHUSD", "BTCUSDT", "ETHUSDT"):
            self.assertGreaterEqual(len(venuebooks.venues_for(sym)), 2, sym)

    def test_fx_has_one_venue_gold_has_two(self):
        # FX arbitrage comes from the cross rate, not venue disagreement:
        # one tradeable book per pair. Gold is the exception -- Binance
        # and Kraken both publish XAUTUSD.
        for sym in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURGBP"):
            self.assertEqual(len(venuebooks.venues_for(sym)), 1, sym)
        self.assertEqual(len(venuebooks.venues_for("XAUUSD")), 2)

    def test_unknown_symbol_has_no_venues(self):
        self.assertEqual(venuebooks.venues_for("NOSUCH"), ())
        self.assertEqual(venuebooks.venues_for(None), ())

    def test_venue_lookup_is_case_insensitive(self):
        self.assertEqual(venuebooks.pair_for("btcusd", "BINANCE"), "BTCUSDT")
        self.assertIsNone(venuebooks.pair_for("BTCUSD", "kraken"))

    def test_unknown_venue_is_expensive_not_free(self):
        # An unpriced fee would silently inflate every edge.
        self.assertGreaterEqual(venuebooks.fee_bps("who-knows"),
                                venuebooks.DEFAULT_FEE_BPS)

    def test_unknown_venue_counts_as_synthetic(self):
        self.assertTrue(venuebooks.is_synthetic("who-knows"))
        self.assertTrue(venuebooks.is_synthetic(""))
        self.assertFalse(venuebooks.is_synthetic("binance"))

    def test_every_route_pair_resolves(self):
        for sym, routes in venuebooks.VENUE_ROUTES.items():
            self.assertTrue(routes, sym)
            for venue, pair in routes:
                self.assertTrue(venue and pair, sym)
                self.assertEqual(venuebooks.pair_for(sym, venue), pair)

    def test_kraken_is_not_synthetic(self):
        # Regression: kraken carried a 26 bps fee but no entry in the
        # routes table, and known_venues() read the routes table -- so a
        # real, priced venue was reported synthetic and refused by the
        # scanner. A venue's identity must not depend on which symbols
        # it currently carries.
        self.assertIn("kraken", venuebooks.known_venues())
        self.assertFalse(venuebooks.is_synthetic("kraken"))
        self.assertEqual(venuebooks.fee_bps("kraken"), 26.0)

    def test_every_priced_venue_is_known(self):
        # The fee table and known_venues() must never disagree again.
        for venue in venuebooks.VENUE_FEES_BPS:
            if venue == "synthetic":
                continue
            self.assertFalse(venuebooks.is_synthetic(venue), venue)

    def test_routed_venue_is_never_synthetic(self):
        for sym, routes in venuebooks.VENUE_ROUTES.items():
            for venue, _pair in routes:
                self.assertFalse(venuebooks.is_synthetic(venue),
                                 "{}@{}".format(sym, venue))

    def test_dead_stooq_is_gone(self):
        # Every stooq quote endpoint 404s now, including the pre-existing
        # EquityPoller's own URL form. It must not reappear in the
        # registry masquerading as a working source.
        self.assertNotIn("stooq", venuebooks.known_venues())
        self.assertTrue(venuebooks.is_synthetic("stooq"))
        for routes in venuebooks.VENUE_ROUTES.values():
            for venue, _pair in routes:
                self.assertNotEqual(venue, "stooq")

    def test_gold_has_two_real_venues(self):
        # The one genuinely tradeable commodity cross-venue pair.
        self.assertEqual(set(venuebooks.venue_names("XAUUSD")),
                         {"binance", "kraken"})
        self.assertFalse(venuebooks.is_synthetic_only("XAUUSD"))

    def test_other_commodities_are_declared_synthetic_only(self):
        for sym in ("XAGUSD", "BRENT", "WTI", "CORN"):
            self.assertTrue(venuebooks.is_synthetic_only(sym), sym)

    def test_reference_penalty_machinery_is_gone(self):
        # It existed only to compensate for stooq's zero-width mid-price
        # books. Against real two-sided books it would only understate
        # edges, so it was deleted rather than kept as a knob.
        for gone in ("REFERENCE_VENUES", "REFERENCE_PENALTY_BPS",
                     "is_reference"):
            self.assertFalse(hasattr(venuebooks, gone), gone)
        self.assertFalse(hasattr(arbitrage, "_execution_penalty_bps"))
        self.assertFalse(hasattr(arbitrage, "REFERENCE_PENALTY_BPS"))

    def test_every_configured_cross_actually_composes(self):
        # Regression: fx_cross used to multiply unconditionally, so
        # triangles like `EURGBP = EURJPY x USDJPY` were priced as if
        # they were identities. On live data that reported
        # +324,420,004 bps -- a fabricated opportunity. Code review missed
        # it because each symbol looked plausible alone; only currency
        # arithmetic exposes it, so every configured cross is now checked
        # against the real rates it claims to reproduce.
        mid = {
            "EURUSD": 1.12357, "GBPUSD": 1.31872, "EURGBP": 0.85200,
            "EURJPY": 177.793, "USDJPY": 158.030, "AUDUSD": 0.69220,
            "AUDJPY": 109.434, "USDCHF": 0.83216, "USDCAD": 1.42440,
        }
        for direct, (l1, l2) in arbitrage.FX_CROSSES:
            self.assertIsNotNone(
                arbitrage._compose_op(direct, l1, l2),
                "{} does not compose from {} and {}".format(direct, l1, l2))
            op = arbitrage._compose_op(direct, l1, l2)
            got = (mid[l1] * mid[l2]) if op == "mul" else (mid[l1] / mid[l2])
            # A real market is never exact, but these must agree to well
            # inside any plausible spread. 0.5% catches a wrong operator
            # while tolerating genuine cross drift.
            self.assertAlmostEqual(got / mid[direct], 1.0, delta=0.005,
                                   msg="{} vs {} {} {}: {:.6f} vs {:.6f}"
                                       .format(direct, l1, op, l2, got,
                                               mid[direct]))

    def test_non_composing_legs_are_refused_not_priced(self):
        # Same bases, wrong pair set: multiplying these is meaningless.
        out = arbitrage.fx_cross("EURGBP", _book(0.85, 0.85, "kraken"),
                                 [dict(_book(177.0, 177.0, "kraken"), symbol="EURJPY"),
                                  dict(_book(158.0, 158.0, "kraken"), symbol="USDJPY")])
        self.assertFalse(out["ok"])
        self.assertIn("does not compose", out["reason"])

    def test_division_cross_is_priced_correctly(self):
        # EURUSD = EURJPY / USDJPY. Multiplying these would give ~28,000
        # and a ~2,400,000% "edge".
        out = arbitrage.fx_cross("EURUSD", _book(1.1235, 1.1236, "kraken"),
                                 [dict(_book(177.79, 177.80, "kraken"), symbol="EURJPY"),
                                  dict(_book(158.03, 158.04, "kraken"), symbol="USDJPY")])
        self.assertTrue(out["ok"], out["reason"])
        self.assertEqual(out["op"], "div")
        # ask1/bid2 and bid1/ask2 -- opposite sides, because a cross is
        # traded opposite ways. This assertion previously read ask1/ask2,
        # which is the untradeable price, and passed against the defect.
        self.assertAlmostEqual(out["cross_buy"], 177.80 / 158.03, places=5)
        self.assertAlmostEqual(out["cross_sell"], 177.79 / 158.04, places=5)
        self.assertLess(out["edge_bps"], 0)

    def test_fx_crosses_only_name_routed_pairs(self):
        # GBPJPY is one of the most liquid crosses in the world and
        # kraken rejects it. Every leg of every configured cross must
        # resolve to a real route, or the scanner queues a scan that can
        # never price anything.
        for direct, (a, b) in arbitrage.FX_CROSSES:
            for leg in (direct, a, b):
                self.assertTrue(venuebooks.venues_for(leg),
                                "{} in {} cross".format(leg, direct))
            self.assertEqual(len({direct, a, b}), 3)


class TestScanners(unittest.TestCase):

    def test_scan_triangles_enumerates_cycles(self):
        books = {"A": {"binance": _book(100.0, 100.1)},
                 "B": {"binance": _book(10.0, 10.1)},
                 "C": {"binance": _book(1.0, 1.1)}}
        out = arbitrage.scan_triangles(["A", "B", "C"], books)
        # One cycle per unordered triple, deduplicated by rotation.
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["legs"]), 3)

    def test_scan_triangles_needs_three_symbols(self):
        books = {"A": {"binance": _book(1.0, 1.0)},
                 "B": {"binance": _book(1.0, 1.0)}}
        self.assertEqual(arbitrage.scan_triangles(["A", "B"], books), [])

    def test_scan_triangles_respects_cap(self):
        syms = ["S%d" % i for i in range(8)]
        books = {s: {"binance": _book(1.0 + i, 1.1 + i)}
                 for i, s in enumerate(syms)}
        out = arbitrage.scan_triangles(syms, books)
        self.assertLessEqual(len(out), 24)

    def test_scan_crypto_skips_single_venue_symbols(self):
        books = {"BTCUSD": {"binance": _book(99, 100), "coinbase": _book(101, 102)},
                 "EURUSD": {"kraken": _book(1.08, 1.08)}}
        out = arbitrage.scan_crypto(["BTCUSD", "EURUSD"], books)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["symbol"], "BTCUSD")

    def test_scan_fx_skips_incomplete_triangles(self):
        out = arbitrage.scan_fx({"EURUSD": {"kraken": _book(1.08, 1.08)}})
        self.assertEqual(out, [])

    def test_scan_fx_prices_complete_triangle(self):
        books = {"EURUSD": {"kraken": _book(1.0799, 1.0801)},
                 "EURGBP": {"kraken": _book(0.8500, 0.8501)},
                 "GBPUSD": {"kraken": _book(1.2700, 1.2701)}}
        out = arbitrage.scan_fx(books)
        self.assertTrue(out)
        self.assertEqual(out[0]["direct_symbol"], "EURUSD")

    def test_scan_commodity_skips_missing_leg(self):
        out = arbitrage.scan_commodity({"XAUUSD": {"kraken": _book(2400, 2401)}})
        self.assertEqual(out, [])

    def test_flat_crypto_book_elects_no_opportunity(self):
        books = {"A": {"binance": _book(100.0, 100.0),
                       "coinbase": _book(100.0, 100.0)}}
        out = arbitrage.scan_crypto(["A"], books)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["edge_bps"])


class TestScannerIntegration(unittest.TestCase):
    """End-to-end through api.routes: books in, tiered payload out."""

    def _db(self):
        """A real schema-backed DB, so venue_quotes exists."""
        from data.db import init_db
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return init_db(path), path

    def _seed_books(self, conn, rows):
        now = int(__import__("time").time())
        conn.executemany(
            "INSERT OR IGNORE INTO venue_quotes"
            " (symbol, venue, ts, bid, ask, bid_size, ask_size, last, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(s, v, now, b, a, 1.0, 1.0, (b + a) / 2.0, v) for s, v, b, a in rows])
        conn.commit()

    def test_scan_uses_two_venues(self):
        from api.routes import _engine_arbitrage
        from data import fxmarkets as _fxm
        conn, path = self._db()
        try:
            _fxm.install(conn)
            self._seed_books(conn, [("BTCUSD", "binance", 100.00, 100.01),
                                    ("BTCUSD", "coinbase", 105.00, 105.01)])
            out = _engine_arbitrage(conn, {"tier": "Pro"}, asset_class="crypto")
            cross = [r for r in out["opportunities"] if r["model"] == "cross_venue"]
            self.assertTrue(cross)
            btc = [r for r in cross if r["symbol"] == "BTCUSD"][0]
            self.assertTrue(btc["ok"])
            self.assertGreater(btc["edge_bps"], 0)
        finally:
            conn.close()
            os.unlink(path)

    def test_stale_books_kept_but_flagged(self):
        # Deliberate change from the old behaviour, which dropped stale
        # rows entirely.

        # The exchange-default literature finds dislocations *persist* on
        # venues traders distrust, because the arbitrageurs who would
        # close them hesitate. A gap still present after the freshness
        # window is that signal -- not noise. Dropping it erased the one
        # case worth looking at, and left the UI unable to tell "cannot
        # price this now" from "there is nothing here".
        from api.routes import _arbitrage_books
        conn, path = self._db()
        try:
            old = int(__import__("time").time()) - 10_000
            for venue, bid, ask in (("binance", 100.00, 100.01),
                                    ("coinbase", 105.00, 105.01)):
                conn.execute(
                    "INSERT OR IGNORE INTO venue_quotes"
                    " (symbol, venue, ts, bid, ask, source)"
                    " VALUES ('BTCUSD', ?, ?, ?, ?, ?)",
                    (venue, old, bid, ask, venue))
            conn.commit()
            books = _arbitrage_books(conn, allow_live=False)
            self.assertIn("BTCUSD", books)
            self.assertEqual(len(books["BTCUSD"]), 2)
            for venue, book in books["BTCUSD"].items():
                self.assertTrue(book.get("stale"), venue)
                self.assertGreater(book.get("age_s", 0), 0)
        finally:
            conn.close()
            os.unlink(path)

    def test_persistent_gap_is_not_priced_executable(self):
        # Kept and visible, but never sold as an edge someone can trade.
        from engines.arbitrage import cross_venue
        out = cross_venue("BTCUSD", {
            "binance": dict(_book(100.00, 100.01), age_s=900, stale=True),
            "coinbase": dict(_book(105.00, 105.01), age_s=900, stale=True),
        })
        self.assertFalse(out["ok"])
        self.assertIsNone(out["edge_bps"])
        self.assertTrue(out["persistent_gap"])
        self.assertGreater(out["observed_gap_bps"], 0)
        self.assertIn("persisted", out["reason"])

    def test_scan_works_with_no_stored_books(self):
        # The regression that made arbitrage read as broken: the scanner
        # used to depend entirely on venue_quotes, so a fresh deployment
        # reported "no opportunities" when the truth was "never looked".
        from api import routes as _r
        conn, path = self._db()
        try:
            stored = _r._arbitrage_books(conn, allow_live=False)
            self.assertEqual(stored, {})
            # allow_live=True must reach the venues rather than return {}.
            live = _r._arbitrage_books(conn, allow_live=True)
            self.assertIsInstance(live, dict)
            for sym, node in live.items():
                self.assertTrue(node, sym)
                for venue, book in node.items():
                    self.assertFalse(venuebooks.is_synthetic(venue),
                                     "{}@{}".format(sym, venue))
        finally:
            conn.close()
            os.unlink(path)

    def test_negative_edges_are_never_counted_profitable(self):
        # Regression: an earlier version gated `profitable` on the
        # caller-supplied threshold being absent-or-met, so with no
        # threshold every priceable row counted as net-positive. A table
        # of losing trades was labelled "2 net-positive".
        from api.routes import _engine_arbitrage
        conn, path = self._db()
        try:
            # A real 2-bps gross dislocation, well inside the 70 bps of
            # two legs of fees: priceable, and a loss.
            self._seed_books(conn, [("BTCUSD", "binance", 99.98, 100.0),
                                    ("BTCUSD", "coinbase", 100.02, 100.05)])
            out = _engine_arbitrage(conn, {"tier": "Pro"}, asset_class="crypto")
            self.assertGreater(out["priced_count"], 0)
            self.assertEqual(out["profitable_count"], 0)
            for r in out["profitable"]:
                self.assertGreater(r["edge_bps"], 0)
        finally:
            conn.close()
            os.unlink(path)

    def test_threshold_narrows_never_widens(self):
        from api.routes import _engine_arbitrage
        conn, path = self._db()
        try:
            self._seed_books(conn, [("BTCUSD", "binance", 100.00, 100.01),
                                    ("BTCUSD", "coinbase", 105.00, 105.01)])
            wide = _engine_arbitrage(conn, {"tier": "Pro"}, asset_class="crypto")
            narrow = _engine_arbitrage(conn, {"tier": "Pro"},
                                       asset_class="crypto", min_edge_bps=10_000)
            self.assertGreater(wide["profitable_count"], 0)
            self.assertEqual(narrow["profitable_count"], 0)
        finally:
            conn.close()
            os.unlink(path)

    def test_models_do_not_cross_asset_classes(self):
        # A crypto "cycle" through EURUSD is arithmetically computable
        # and structurally meaningless. Models run per class.
        from api.routes import _engine_arbitrage, _arbitrage_partition
        conn, path = self._db()
        try:
            from data import fxmarkets as _fxm
            _fxm.install(conn)
            self._seed_books(conn, [("BTCUSD", "binance", 100.0, 100.1),
                                    ("BTCUSD", "coinbase", 100.2, 100.3),
                                    ("EURUSD", "kraken", 1.08, 1.08),
                                    ("EURGBP", "kraken", 0.85, 0.85),
                                    ("GBPUSD", "kraken", 1.27, 1.27)])
            from api.routes import _arbitrage_books
            parts = _arbitrage_partition(conn, _arbitrage_books(conn))
            self.assertIn("BTCUSD", parts["crypto"])
            self.assertIn("EURUSD", parts["fx"])
            self.assertNotIn("EURUSD", parts["crypto"])
            out = _engine_arbitrage(conn, {"tier": "Pro"}, asset_class="crypto")
            syms = {s for r in out["opportunities"]
                    for s in (r.get("symbols") or [])}
            self.assertFalse(syms & {"EURUSD", "EURGBP", "GBPUSD"})
        finally:
            conn.close()
            os.unlink(path)

    def test_invalid_asset_class_reports_error(self):
        from api.routes import _engine_arbitrage
        conn, path = self._db()
        try:
            out = _engine_arbitrage(conn, {"tier": "Pro"}, asset_class="bananas")
            self.assertIn("error", out)
        finally:
            conn.close()
            os.unlink(path)


class TestTierGate(unittest.TestCase):

    def test_free_lacks_arbitrage(self):
        from core.tiers import has_entitlement, require_entitlement
        self.assertFalse(has_entitlement("Free", "arbitrage"))
        with self.assertRaises(PermissionError):
            require_entitlement("Free", "arbitrage")

    def test_pro_and_above_have_it(self):
        from core.tiers import has_entitlement
        for tier in ("Pro", "Institutional", "Admin"):
            self.assertTrue(has_entitlement(tier, "arbitrage"), tier)

    def test_entitlements_payload_includes_flag(self):
        from core.tiers import get_entitlements
        self.assertIn("arbitrage", get_entitlements("Pro"))
        self.assertFalse(get_entitlements("Free")["arbitrage"])


class TestFxRegistry(unittest.TestCase):

    def test_cross_triangles_are_fully_specified(self):
        for direct, (l1, l2) in arbitrage.FX_CROSSES:
            self.assertNotEqual(direct, l1)
            self.assertNotEqual(direct, l2)
            self.assertNotEqual(l1, l2)
            for leg in (direct, l1, l2):
                self.assertTrue(venuebooks.venues_for(leg), leg)

    def test_pair_split(self):
        from data.fxmarkets import split_pair
        self.assertEqual(split_pair("EURUSD"), ("EUR", "USD"))
        self.assertEqual(split_pair("TOOLONG"), (None, None))

    def test_decimals_follow_tick_size(self):
        from data.fxmarkets import decimals_for
        self.assertEqual(decimals_for("EURUSD"), 5)
        self.assertEqual(decimals_for("USDJPY"), 3)

    def test_install_is_idempotent(self):
        from data import fxmarkets as _fxm
        from data.db import init_db
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = init_db(path)
        try:
            _fxm.install(conn)
            _fxm.install(conn)
            n = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE asset_type = 'fx'").fetchone()[0]
            self.assertEqual(n, len(_fxm.FX_SYMBOLS))
        finally:
            conn.close()
            os.unlink(path)

    def test_install_does_not_clobber_operator_edits(self):
        # INSERT OR IGNORE means a disabled symbol survives a boot
        # migration. An operator who turned a pair off must not have it
        # silently switched back on by a restart.
        from data import fxmarkets as _fxm
        from data.db import init_db
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = init_db(path)
        try:
            _fxm.install(conn)
            conn.execute("UPDATE symbols SET enabled = 0 WHERE symbol = 'EURUSD'")
            conn.commit()
            _fxm.install(conn)
            row = conn.execute(
                "SELECT enabled FROM symbols WHERE symbol = 'EURUSD'").fetchone()
            self.assertEqual(row[0], 0)
        finally:
            conn.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

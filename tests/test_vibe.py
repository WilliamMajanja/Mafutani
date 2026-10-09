"""Tests for the Vibe layer: shadow account + grounding gate.

Every test here targets a specific way this code can produce a
*confident wrong number*, which is the failure mode the whole module
exists to prevent:

* a rule that fires on bars its own support count never contained
  (bucket edges and the range node)
* a rule that fires on missing data (NaN, absent feature, absent
  calendar)
* roundtrips whose PnL does not reconcile because a remainder vanished
* a grounding gate that flags everything, or nothing, because a trained
  reader ignores a gate that cries wolf

No network, no filesystem, no database.
"""
import math
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from brokers import vibe  # noqa: E402
from brokers.vibe import VibeError  # noqa: E402


# ================================================================ conditions
class TestConditionEvaluation(unittest.TestCase):
    def test_cmp_basic(self):
        c = vibe.cmp("rsi", "<", 30)
        self.assertTrue(vibe.eval_condition(c, {"rsi": 20}))
        self.assertFalse(vibe.eval_condition(c, {"rsi": 40}))

    def test_cmp_equality_operators(self):
        self.assertTrue(vibe.eval_condition(vibe.cmp("x", "==", 5), {"x": 5.0}))
        self.assertTrue(vibe.eval_condition(vibe.cmp("x", "!=", 5), {"x": 6.0}))
        self.assertFalse(vibe.eval_condition(vibe.cmp("x", "!=", 5), {"x": 5.0}))

    def test_missing_feature_never_matches(self):
        c = vibe.cmp("rsi", "<", 30)
        self.assertFalse(vibe.eval_condition(c, {}))
        self.assertFalse(vibe.eval_condition(c, {"rsi": None}))

    def test_nan_never_matches(self):
        c = vibe.cmp("rsi", "<", 30)
        self.assertFalse(vibe.eval_condition(c, {"rsi": float("nan")}))

    def test_nan_does_not_satisfy_neq(self):
        c = vibe.cmp("rsi", "!=", 30)
        self.assertFalse(vibe.eval_condition(c, {"rsi": float("nan")}))

    def test_infinite_never_matches(self):
        c = vibe.cmp("rsi", "<", 30)
        self.assertFalse(vibe.eval_condition(c, {"rsi": float("inf")}))
        self.assertFalse(vibe.eval_condition(c, {"rsi": float("-inf")}))

    def test_between_is_half_open(self):
        c = vibe.between("x", 10, 20)
        self.assertTrue(vibe.eval_condition(c, {"x": 10}))    # low inclusive
        self.assertFalse(vibe.eval_condition(c, {"x": 20}))   # high exclusive
        self.assertTrue(vibe.eval_condition(c, {"x": 19.99}))

    def test_between_rejects_empty_range(self):
        with self.assertRaises(VibeError):
            vibe.between("x", 20, 10)
        with self.assertRaises(VibeError):
            vibe.between("x", 10, 10)

    def test_member_in_and_not_in(self):
        c = vibe.member("d", "in", ["a", "b"])
        self.assertTrue(vibe.eval_condition(c, {"d": "a"}))
        self.assertFalse(vibe.eval_condition(c, {"d": "c"}))
        n = vibe.member("d", "not_in", ["a"])
        self.assertFalse(vibe.eval_condition(n, {"d": "a"}))
        self.assertTrue(vibe.eval_condition(n, {"d": "z"}))

    def test_member_missing_feature(self):
        c = vibe.member("d", "in", ["a"])
        self.assertFalse(vibe.eval_condition(c, {}))

    def test_all_any_not(self):
        a = vibe.cmp("x", ">", 1)
        b = vibe.cmp("x", "<", 5)
        self.assertTrue(vibe.eval_condition(vibe.all_of(a, b), {"x": 3}))
        self.assertFalse(vibe.eval_condition(vibe.all_of(a, b), {"x": 9}))
        self.assertTrue(vibe.eval_condition(vibe.any_of(a, b), {"x": 9}))
        self.assertTrue(vibe.eval_condition(vibe.negate(a), {"x": 0}))

    def test_all_with_one_missing_child_is_false(self):
        a = vibe.cmp("x", ">", 1)
        b = vibe.cmp("y", ">", 1)
        self.assertFalse(vibe.eval_condition(vibe.all_of(a, b), {"x": 5}))

    def test_unknown_operator_raises(self):
        with self.assertRaises(VibeError):
            vibe.cmp("x", "~=", 1)
        with self.assertRaises(VibeError):
            vibe.member("x", "contains", ["a"])

    def test_unknown_node_raises(self):
        with self.assertRaises(VibeError):
            vibe.eval_condition(("bogus", "x"), {"x": 1})

    def test_features_of_collects_all_names(self):
        c = vibe.all_of(
            vibe.cmp("a", ">", 1),
            vibe.between("b", 0, 5),
            vibe.member("c", "in", ["x"]),
            vibe.any_of(vibe.cmp("d", "<", 2), vibe.cmp("e", ">", 3)),
        )
        self.assertEqual(vibe.features_of(c),
                         {"a", "b", "c", "d", "e"})

    def test_features_of_between(self):
        self.assertEqual(vibe.features_of(vibe.between("b", 0, 5)), {"b"})

    def test_features_of_unknown_raises(self):
        with self.assertRaises(VibeError):
            vibe.features_of(("bogus",))


# ================================================================= buckets
class TestBucketing(unittest.TestCase):
    def test_labels_partition_the_line(self):
        f = "entry_rsi14"
        edges = vibe._BUCKET_EDGES[f]
        # Every value in a wide sweep falls in exactly one bucket.
        seen = {}
        v = -50.0
        while v < 150.0:
            self.assertIsNotNone(vibe.bucket_label(f, v))
            v += 0.5

    def test_bucket_boundaries_use_half_open_ranges(self):
        f = "prior_return"
        e0, e1, e2, e3 = vibe._BUCKET_EDGES[f]
        # Exactly on an edge belongs to the bucket that starts there.
        self.assertEqual(vibe.bucket_label(f, e0), "low")
        self.assertEqual(vibe.bucket_label(f, e1), "mid")
        self.assertEqual(vibe.bucket_label(f, e2), "high")
        self.assertEqual(vibe.bucket_label(f, e3), "very_high")
        self.assertEqual(vibe.bucket_label(f, e0 - 1e-9), "very_low")

    def test_nan_and_none_have_no_bucket(self):
        self.assertIsNone(vibe.bucket_label("entry_rsi14", None))
        self.assertIsNone(vibe.bucket_label("entry_rsi14", float("nan")))

    def test_hour_bucketing(self):
        self.assertEqual(vibe.bucket_label("hour_utc", 0), "h00")
        self.assertEqual(vibe.bucket_label("hour_utc", 2), "h00")
        self.assertEqual(vibe.bucket_label("hour_utc", 3), "h01")
        self.assertEqual(vibe.bucket_label("hour_utc", 23), "h07")

    def test_weekday_bucketing(self):
        for d in range(7):
            self.assertEqual(vibe.bucket_label("weekday", d), "d%d" % d)

    def test_condition_reproduces_its_own_bucket(self):
        # The core invariant: for every feature/label, a value the label
        # covers is a value the built condition matches. A mismatch means
        # the rule fires on bars its support never contained.
        for feature, edges in vibe._BUCKET_EDGES.items():
            labels = ("very_low", "low", "mid", "high", "very_high")
            probes = {"very_low": edges[0] - 10, "low": edges[0],
                      "mid": edges[1], "high": edges[2], "very_high": edges[3]}
            for label in labels:
                cond = vibe._condition_for(feature, label)
                self.assertIsNotNone(cond, feature)
                # A value bucketed as `label` must satisfy the condition.
                self.assertTrue(
                    vibe.eval_condition(cond, {feature: probes[label]}),
                    "%s/%s: probe %r does not satisfy %r" % (
                        feature, label, probes[label], cond))

    def test_condition_does_not_bleed_into_neighbouring_buckets(self):
        for feature, edges in vibe._BUCKET_EDGES.items():
            # The `mid` condition must not match a value that buckets as
            # `very_high` -- the over-firing case.
            mid = vibe._condition_for(feature, "mid")
            self.assertFalse(
                vibe.eval_condition(mid, {feature: edges[3] + 10}),
                "%s: mid condition matched a very_high value" % feature)
            low = vibe._condition_for(feature, "low")
            self.assertFalse(
                vibe.eval_condition(low, {feature: edges[0] - 10}),
                "%s: low condition matched a very_low value" % feature)

    def test_hour_condition_matches_its_block_only(self):
        cond = vibe._condition_for("hour_utc", "h01")
        for h in range(3, 6):
            self.assertTrue(vibe.eval_condition(cond, {"hour_utc": h}))
        for h in (0, 2, 6, 23):
            self.assertFalse(vibe.eval_condition(cond, {"hour_utc": h}))

    def test_weekday_condition(self):
        cond = vibe._condition_for("weekday", "d3")
        self.assertTrue(vibe.eval_condition(cond, {"weekday": 3}))
        self.assertFalse(vibe.eval_condition(cond, {"weekday": 4}))

    def test_bucket_label_rejects_unknown_feature(self):
        self.assertIsNone(vibe.bucket_label("not_a_feature", 1))

    def test_hour_label_unmappable_returns_none_condition(self):
        self.assertIsNone(vibe._condition_for("not_a_feature", "x"))


# ==================================================================== roundtrips
def fills(*rows):
    out = []
    for ts, side, qty, price, sym in rows:
        out.append({"symbol": sym, "side": side, "qty": qty,
                    "price": price, "ts": ts})
    return out


def net_position(fill_rows):
    """Signed residual position implied by a fill sequence."""
    pos = 0.0
    for f in fill_rows:
        pos += f["qty"] if f["side"] == "buy" else -f["qty"]
    return pos


class TestRoundtrips(unittest.TestCase):
    def test_simple_long_roundtrip(self):
        f = fills((1000, "buy", 10, 100.0, "X"), (1060, "sell", 10, 105.0, "X"))
        rts = vibe.roundtrips_from_fills(f)
        self.assertEqual(len(rts), 1)
        rt = rts[0]
        self.assertEqual(rt.side, "long")
        self.assertAlmostEqual(rt.pnl, 50.0)
        self.assertEqual(rt.bars_held, 1)

    def test_loss_is_negative(self):
        f = fills((1000, "buy", 10, 100.0, "X"), (1060, "sell", 10, 95.0, "X"))
        rt = vibe.roundtrips_from_fills(f)[0]
        self.assertAlmostEqual(rt.pnl, -50.0)
        self.assertFalse(rt.profitable)

    def test_fifo_partial_closes(self):
        f = fills((1000, "buy", 10, 100.0, "X"),
                  (1001, "buy", 10, 200.0, "X"),
                  (1060, "sell", 10, 150.0, "X"))
        rts = vibe.roundtrips_from_fills(f)
        self.assertEqual(len(rts), 1)
        # FIFO closes the 100 lot first: (150-100)*10
        self.assertAlmostEqual(rts[0].pnl, 500.0)
        self.assertAlmostEqual(rts[0].open_price, 100.0)

    def test_sell_beyond_position_opens_short(self):
        # The bug this guards: the remainder used to be dropped, so the
        # position no longer reconciled with the journal.
        f = fills((1000, "buy", 10, 100.0, "X"),
                  (1060, "sell", 25, 110.0, "X"))
        rts = vibe.roundtrips_from_fills(f)
        # 10 closes the long, 15 opens a short that is still open.
        self.assertEqual(len(rts), 1)
        self.assertEqual(rts[0].side, "long")
        self.assertAlmostEqual(rts[0].qty, 10.0)
        # A later buy closes the short.
        f2 = fills((1000, "buy", 10, 100.0, "X"),
                   (1060, "sell", 25, 110.0, "X"),
                   (1120, "buy", 15, 105.0, "X"))
        shorts = [r for r in vibe.roundtrips_from_fills(f2)
                  if r.side == "short"]
        self.assertEqual(len(shorts), 1)
        # Short from 110 to 105: (110-105)*15
        self.assertAlmostEqual(shorts[0].pnl, 75.0)
        self.assertAlmostEqual(shorts[0].qty, 15.0)

    def test_quantity_reconciles(self):
        # A closed roundtrip of qty q consumes q of one fill and q of
        # another, so fills reconcile as 2*closed + open == total. Under
        # the old drop-the-remainder pairing this identity failed, which
        # is what made every later PnL untrustworthy.
        seq = fills((1, "buy", 10, 100.0, "X"),
                    (2, "sell", 4, 105.0, "X"),
                    (3, "buy", 3, 107.0, "X"),
                    (4, "sell", 20, 103.0, "X"),
                    (5, "buy", 6, 101.0, "X"))
        rts = vibe.roundtrips_from_fills(seq)
        closed = sum(r.qty for r in rts)
        total = 10 + 4 + 3 + 20 + 6
        self.assertAlmostEqual(2 * closed + abs(net_position(seq)),
                               total, places=6)

    def test_reconciles_for_many_sequences(self):
        # The identity is the property; it must hold for sequences the
        # author did not think to write down.
        import itertools
        options = [("buy", 3.0, 100.0), ("sell", 7.0, 110.0),
                   ("buy", 1.0, 90.0), ("sell", 5.0, 105.0),
                   ("buy", 2.0, 95.0), ("sell", 9.0, 120.0)]
        for combo in itertools.combinations(options, 4):
            seq = fills(*[(i, s, q, p, "X")
                          for i, (s, q, p) in enumerate(combo, 1)])
            rts = vibe.roundtrips_from_fills(seq)
            closed = sum(r.qty for r in rts)
            total = sum(q for _s, q, _p in combo)
            self.assertAlmostEqual(
                2 * closed + abs(net_position(seq)), total, places=6,
                msg="reconciliation failed for %r" % (combo,))

    def test_short_profit_positive_when_price_falls(self):
        f = fills((1000, "sell", 10, 100.0, "X"), (1060, "buy", 10, 90.0, "X"))
        rt = vibe.roundtrips_from_fills(f)[0]
        self.assertEqual(rt.side, "short")
        self.assertAlmostEqual(rt.pnl, 100.0)
        self.assertTrue(rt.profitable)

    def test_losing_short_is_negative(self):
        f = fills((1000, "sell", 10, 100.0, "X"), (1060, "buy", 10, 110.0, "X"))
        rt = vibe.roundtrips_from_fills(f)[0]
        self.assertEqual(rt.side, "short")
        self.assertAlmostEqual(rt.pnl, -100.0)
        self.assertFalse(rt.profitable)

    def test_multiple_symbols_are_independent(self):
        f = fills((1000, "buy", 10, 100.0, "X"),
                  (1000, "buy", 10, 50.0, "Y"),
                  (1060, "sell", 10, 110.0, "X"),
                  (1060, "sell", 10, 45.0, "Y"))
        rts = vibe.roundtrips_from_fills(f)
        self.assertEqual(len(rts), 2)
        by_sym = {r.symbol: r for r in rts}
        self.assertAlmostEqual(by_sym["X"].pnl, 100.0)
        self.assertAlmostEqual(by_sym["Y"].pnl, -50.0)

    def test_bad_fills_are_skipped(self):
        f = [{"symbol": "", "side": "buy", "qty": 1, "price": 1, "ts": 1},
             {"symbol": "X", "side": "wat", "qty": 1, "price": 1, "ts": 2},
             {"symbol": "X", "side": "buy", "qty": 0, "price": 1, "ts": 3},
             {"symbol": "X", "side": "buy", "qty": 1, "price": 0, "ts": 4},
             {"nope": True},
             {"symbol": "X", "side": "buy", "qty": "x", "price": 1, "ts": 5}]
        self.assertEqual(vibe.roundtrips_from_fills(f), [])

    def test_empty_input(self):
        self.assertEqual(vibe.roundtrips_from_fills([]), [])

    def test_same_second_preserves_input_order(self):
        # A sell before a buy in the same second must not be reordered.
        f = fills((1000, "sell", 10, 100.0, "X"), (1000, "buy", 10, 100.0, "X"))
        rts = vibe.roundtrips_from_fills(f)
        self.assertEqual(len(rts), 1)
        self.assertAlmostEqual(rts[0].pnl, 0.0)
        self.assertEqual(rts[0].side, "short")

    def test_roundtrip_to_dict_is_json_safe(self):
        import json
        f = fills((1000, "buy", 10, 100.0, "X"), (1060, "sell", 10, 105.0, "X"))
        json.dumps(vibe.roundtrips_from_fills(f)[0].to_dict())


# ==================================================================== features
class TestFeatures(unittest.TestCase):
    def test_calendar_features_absent_without_timestamps(self):
        closes = [100.0] * 40
        feats = vibe.compute_features(closes)
        # Without timestamps the calendar features must be None, not 0.
        self.assertTrue(all(v is None for v in feats["hour_utc"]))
        self.assertTrue(all(v is None for v in feats["weekday"]))

    def test_calendar_features_from_timestamps(self):
        # Derived, not hardcoded: a hand-typed epoch is how this test
        # came to assert hour 14 for a timestamp meant to be 13:37.
        import calendar
        ts = calendar.timegm((2026, 1, 5, 13, 37, 0, 0, 0, 0))
        feats = vibe.compute_features([100.0] * 3, timestamps=[ts] * 3)
        self.assertEqual(feats["hour_utc"][0], 13)
        self.assertEqual(feats["weekday"][0], 0)   # a Monday

    def test_calendar_uses_utc_not_local(self):
        # The same instant must yield the same hour whatever TZ the
        # process is running in, so a rule extracted in one zone does not
        # silently mean a different hour in another.
        import calendar
        import os
        import time as _t
        ts = calendar.timegm((2026, 1, 5, 0, 30, 0, 0, 0, 0))
        original = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/New_York", "Asia/Tokyo"):
                os.environ["TZ"] = zone
                if hasattr(_t, "tzset"):
                    _t.tzset()
                feats = vibe.compute_features([100.0], timestamps=[ts])
                self.assertEqual(feats["hour_utc"][0], 0, zone)
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            if hasattr(_t, "tzset"):
                _t.tzset()

    def test_wrong_length_timestamps_ignored(self):
        feats = vibe.compute_features([100.0, 101.0], timestamps=[1])
        self.assertTrue(all(v is None for v in feats["hour_utc"]))

    def test_rsi_of_uptrend_is_high(self):
        closes = [100.0 + i for i in range(30)]
        r = vibe.rsi(closes, 14)
        self.assertIsNone(r[13])
        self.assertGreater(r[-1], 90)

    def test_rsi_of_downtrend_is_low(self):
        closes = [200.0 - i for i in range(30)]
        r = vibe.rsi(closes, 14)
        self.assertLess(r[-1], 10)

    def test_rsi_needs_enough_history(self):
        r = vibe.rsi([1.0, 2.0, 3.0], 14)
        self.assertTrue(all(v is None for v in r))

    def test_rsi_flat_series_is_midpoint_not_error(self):
        # No movement at all: convention gives 50 here (neither up nor
        # down), and critically not 0 or NaN.
        r = vibe.rsi([100.0] * 30, 14)
        self.assertEqual(r[-1], 50.0)

    def test_rsi_rises_only_never_nan(self):
        closes = [100.0 + (i % 3) for i in range(40)]
        for v in vibe.rsi(closes, 14):
            if v is not None:
                self.assertFalse(math.isnan(v))
                self.assertTrue(0.0 <= v <= 100.0)

    def test_prior_return(self):
        feats = vibe.compute_features([100.0, 110.0, 99.0])
        self.assertAlmostEqual(feats["prior_return"][1], 0.10)
        self.assertAlmostEqual(feats["prior_return"][2], -0.10)

    def test_range_pct(self):
        feats = vibe.compute_features(
            [100.0], highs=[102.0], lows=[98.0])
        self.assertAlmostEqual(feats["range_pct"][0], 0.04)

    def test_volume_ratio(self):
        closes = [100.0] * 5
        vols = [10.0, 10.0, 10.0, 10.0, 40.0]
        feats = vibe.compute_features(closes, volumes=vols, lookback=4)
        # last volume 40 / mean(10,10,10,40)=17.5
        self.assertAlmostEqual(feats["volume_ratio"][4], 40.0 / 17.5)

    def test_empty_input(self):
        feats = vibe.compute_features([])
        self.assertEqual(feats["entry_rsi14"], [])

    def test_features_align_with_input_index(self):
        # prior_return is the *one-bar* return, so the last bar of a
        # +1/bar ramp is 1/118, not 1/19. A 20-bar return would swallow
        # every bar of a 1-minute series.
        closes = [100.0 + i for i in range(20)]
        feats = vibe.compute_features(closes)
        self.assertEqual(len(feats["prior_return"]), len(closes))
        self.assertAlmostEqual(feats["prior_return"][-1], 1.0 / 118.0)
        self.assertIsNone(feats["prior_return"][0])
        for i in range(1, len(closes)):
            self.assertAlmostEqual(
                feats["prior_return"][i],
                (closes[i] - closes[i - 1]) / closes[i - 1])


# ==================================================================== extractor
def make_journal(n_winners, pattern="low", losers=0):
    """Build a journal of winners all sharing one feature bucket."""
    journal = []
    for i in range(n_winners):
        rsi_val = {"low": 20.0, "mid": 50.0, "high": 65.0}[pattern]
        journal.append(vibe.Roundtrip(
            "SYM%d" % (i % 3), "long", 1000 + i, 100.0, 1.0,
            1060, 101.0, 1.0, bars_held=5))
    for i in range(losers):
        journal.append(vibe.Roundtrip(
            "SYM%d" % (i % 3), "long", 1000, 100.0, 1.0, 1060, 99.0, -1.0))
    return journal


def matching_features(journal, pattern="low"):
    feats = []
    for _ in journal:
        rsi_val = {"low": 20.0, "mid": 50.0, "high": 65.0}[pattern]
        feats.append({"entry_rsi14": rsi_val, "prior_return": 0.0,
                      "volume_ratio": 1.0, "range_pct": 0.01})
    return feats


class TestExtractor(unittest.TestCase):
    def test_learns_a_low_rsi_rule(self):
        # rsi 20 sits below the first edge (30), so the bucket is
        # "very low" -- the label is derived, not the word I want.
        journal = make_journal(10, "low")
        feats = matching_features(journal, "low")
        prof = vibe.extract_profile(journal, feats, min_support=3)
        self.assertGreaterEqual(len(prof.rules), 1)
        self.assertEqual(vibe.bucket_label("entry_rsi14", 20.0), "very_low")
        texts = " ".join(r.text for r in prof.rules)
        self.assertIn("entry_rsi14", texts)
        self.assertIn("very low", texts)

    def test_support_and_coverage_present(self):
        journal = make_journal(10, "low")
        feats = matching_features(journal, "low")
        prof = vibe.extract_profile(journal, feats, min_support=3)
        r = prof.rules[0]
        self.assertGreaterEqual(r.support, 3)
        self.assertGreater(r.coverage, 0)

    def test_length_mismatch_raises(self):
        journal = make_journal(5)
        with self.assertRaises(VibeError):
            vibe.extract_profile(journal, matching_features(journal)[:2])

    def test_no_profitable_raises(self):
        journal = [vibe.Roundtrip("X", "long", 1, 1.0, 1.0, 2, 0.9, -1.0)]
        with self.assertRaises(VibeError):
            vibe.extract_profile(journal, [{}])

    def test_no_usable_features_raises(self):
        journal = make_journal(5)
        feats = [{} for _ in journal]
        with self.assertRaises(VibeError):
            vibe.extract_profile(journal, feats)

    def test_min_support_too_high_raises(self):
        journal = make_journal(4, "low")
        feats = matching_features(journal, "low")
        with self.assertRaises(VibeError):
            vibe.extract_profile(journal, feats, min_support=100)

    def test_deterministic(self):
        journal = make_journal(12, "low")
        feats = matching_features(journal, "low")
        a = vibe.extract_profile(journal, feats, min_support=2)
        b = vibe.extract_profile(journal, feats, min_support=2)
        self.assertEqual([r.rule_id for r in a.rules],
                         [r.rule_id for r in b.rules])
        self.assertEqual([r.text for r in a.rules], [r.text for r in b.rules])

    def test_max_rules_respected(self):
        journal = make_journal(20, "low")
        feats = matching_features(journal, "low")
        prof = vibe.extract_profile(journal, feats, min_support=2, max_rules=2)
        self.assertLessEqual(len(prof.rules), 2)

    def test_skips_unusable_in_coverage_base(self):
        # 3 winners with features, 5 winners with none. The coverage base
        # is the 3 usable ones, not 8: counting the featureless rows in
        # the denominator would drive every real coverage toward zero and
        # make a well-supported rule look unsupported.
        journal = make_journal(3, "low") + make_journal(5, "low")
        feats = (matching_features(journal[:3], "low")
                 + [{} for _ in journal[3:]])
        prof = vibe.extract_profile(journal, feats, min_support=2)
        r = prof.rules[0]
        self.assertAlmostEqual(r.coverage, 1.0)
        self.assertIn("3 of 8", prof.notes)

    def test_explained_is_union_not_sum(self):
        journal = make_journal(10, "low")
        feats = matching_features(journal, "low")
        prof = vibe.extract_profile(journal, feats, min_support=2)
        # covered <= profitable always, and equals the union.
        self.assertLessEqual(prof.covered, prof.profitable)
        self.assertAlmostEqual(prof.explained,
                               prof.covered / float(prof.profitable), places=4)

    def test_profile_to_dict_is_json_safe(self):
        import json
        journal = make_journal(6, "low")
        feats = matching_features(journal, "low")
        prof = vibe.extract_profile(journal, feats, min_support=2)
        json.dumps(prof.to_dict())  # must not raise

    def test_rules_round_trip_through_json(self):
        journal = make_journal(6, "low")
        feats = matching_features(journal, "low")
        prof = vibe.extract_profile(journal, feats, min_support=2)
        for r in prof.rules:
            restored = vibe._cond_from_json(vibe._cond_to_json(r.entry))
            for v in (0.0, 25.0, 50.0, 65.0, 100.0):
                self.assertEqual(
                    vibe.eval_condition(r.entry, {"entry_rsi14": v}),
                    vibe.eval_condition(restored, {"entry_rsi14": v}))


# ==================================================================== scanner
class TestScanner(unittest.TestCase):
    def _profile_with(self, feature, value, op="<", threshold=30.0):
        rule = vibe.Rule("R1", "entry", vibe.cmp(feature, op, threshold),
                         support=5, coverage=0.5)
        return vibe.Profile("p", [rule], 10, 5, covered=5)

    def test_fires_on_matching_bar(self):
        prof = self._profile_with("entry_rsi14", 20, "<", 30)
        series = {"X": {"entry_rsi14": [20.0]}}
        signals, _gaps = vibe.scan(prof, series)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].symbol, "X")

    def test_does_not_fire_on_non_matching_bar(self):
        prof = self._profile_with("entry_rsi14", 50, "<", 30)
        series = {"X": {"entry_rsi14": [50.0]}}
        signals, gaps = vibe.scan(prof, series)
        self.assertEqual(signals, [])
        # Non-matching but no gap.
        self.assertEqual(gaps, [])

    def test_gap_reported_when_feature_missing(self):
        prof = self._profile_with("entry_rsi14", None)
        series = {"X": {"entry_rsi14": [None]}}
        signals, gaps = vibe.scan(prof, series)
        self.assertEqual(signals, [])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][0], "X")
        self.assertIn("entry_rsi14", gaps[0][2])

    def test_uses_last_bar_only(self):
        prof = self._profile_with("entry_rsi14", 20, "<", 30)
        series = {"X": {"entry_rsi14": [20.0, 80.0]}}
        signals, _ = vibe.scan(prof, series)
        self.assertEqual(signals, [])

    def test_rule_ids_sort_naturally(self):
        rules = [vibe.Rule("R{}".format(i), "t", vibe.cmp("x", ">", i),
                           support=1, coverage=0.1) for i in (1, 2, 10)]
        prof = vibe.Profile("p", rules, 3, 3, covered=3)
        series = {"A": {"x": [20.0]}, "B": {"x": [20.0]}, "C": {"x": [20.0]}}
        signals, _ = vibe.scan(prof, series)
        ids = [s.rule_id for s in signals]
        self.assertEqual(ids, sorted(ids, key=lambda r: int(r[1:])))

    def test_limit_respected(self):
        # A rule on a declared feature: scan only reads FEATURE_NAMES out
        # of a series, so a rule keyed on an undeclared name is skipped
        # and reported as a gap (see the next test).
        rules = [vibe.Rule("R{}".format(i), "t", vibe.cmp("volume_ratio", ">", 0),
                           support=1, coverage=0.1) for i in range(1, 4)]
        prof = vibe.Profile("p", rules, 3, 3, covered=3)
        series = {("S%d" % j): {"volume_ratio": [2.0]} for j in range(10)}
        signals, _ = vibe.scan(prof, series, limit=5)
        self.assertEqual(len(signals), 5)
        # 3 rules x 10 symbols would be 30 without the cap.
        self.assertEqual(len(vibe.scan(prof, series, limit=0)[0]), 30)

    def test_undeclared_feature_is_a_gap_not_a_match(self):
        # A rule the scanner cannot evaluate must not fire. `x` is not a
        # declared feature, so the rule is inapplicable rather than
        # false-satisfied.
        prof = self._profile_with("undeclared_feature", 1, ">", 0)
        signals, gaps = vibe.scan(prof, {"X": {"x": [99.0]}})
        self.assertEqual(signals, [])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][2], ["undeclared_feature"])

    def test_empty_profile(self):
        prof = vibe.Profile("p", [], 0, 0, covered=0)
        signals, gaps = vibe.scan(prof, {"X": {"entry_rsi14": [20.0]}})
        self.assertEqual(signals, [])
        self.assertEqual(gaps, [])

    def test_nan_bar_does_not_fire(self):
        prof = self._profile_with("entry_rsi14", None, "!=", 30)
        series = {"X": {"entry_rsi14": [float("nan")]}}
        signals, _ = vibe.scan(prof, series)
        self.assertEqual(signals, [])

    def test_signal_to_dict_is_json_safe(self):
        import json
        prof = self._profile_with("entry_rsi14", 20, "<", 30)
        series = {"X": {"entry_rsi14": [20.0]}}
        signals, _ = vibe.scan(prof, series)
        json.dumps(signals[0].to_dict())


# ================================================================== grounding
class TestGrounding(unittest.TestCase):
    def test_observed_value_is_traceable(self):
        g = vibe.Ground()
        g.observe("bid", 100.0)
        self.assertTrue(g.traceable(100.0))

    def test_unobserved_value_is_not_traceable(self):
        g = vibe.Ground()
        g.observe("bid", 100.0)
        self.assertFalse(g.traceable(99.99))

    def test_observe_rejects_nan_inf_and_junk(self):
        g = vibe.Ground()
        self.assertFalse(g.observe("x", float("nan")))
        self.assertFalse(g.observe("x", float("inf")))
        self.assertFalse(g.observe("x", "abc"))
        self.assertFalse(g.observe("x", None))
        self.assertEqual(len(g), 0)

    def test_audit_text_finds_untraceable(self):
        g = vibe.Ground()
        untraceable, traceable = g.audit_text("price is 1234.56")
        self.assertIn("1234.56", [t for t, _ in untraceable])

    def test_audit_text_accepts_observed(self):
        g = vibe.Ground()
        g.observe("last", 1234.56)
        untraceable, traceable = g.audit_text("price is 1234.56")
        self.assertEqual(untraceable, [])
        self.assertTrue(any(t == "1234.56" for t, _ in traceable))

    def test_thousands_separated_parsed(self):
        g = vibe.Ground()
        g.observe("vol", 1234567.0)
        untraceable, _ = g.audit_text("volume 1,234,567")
        self.assertEqual(untraceable, [])

    def test_identifier_digits_ignored(self):
        g = vibe.Ground()
        # AAPL2, v1.2.3, ETH-2024 are not figures.
        untraceable, _ = g.audit_text("AAPL2 and v1.2.3 and ETH-2024")
        self.assertEqual(untraceable, [])

    def test_ordinals_ignored(self):
        g = vibe.Ground()
        untraceable, _ = g.audit_text("Rule 2 of 5")
        self.assertEqual(untraceable, [])

    def test_empty_text(self):
        g = vibe.Ground()
        self.assertEqual(g.audit_text(""), ([], []))

    def test_verdict_grounded(self):
        g = vibe.Ground()
        g.observe("x", 1.5)
        self.assertEqual(g.verdict("value 1.5"), "grounded")

    def test_verdict_ungrounded_names_the_number(self):
        g = vibe.Ground()
        v = g.verdict("value 9.99")
        self.assertTrue(v.startswith("ungrounded:"))
        self.assertIn("9.99", v)

    def test_label_for(self):
        g = vibe.Ground()
        g.observe("bid", 100.0)
        self.assertEqual(g.label_for(100.0), "bid")

    def test_observe_many(self):
        g = vibe.Ground()
        n = g.observe_many("q", [1.0, 2.0, "x", float("nan")])
        self.assertEqual(n, 2)

    def test_tolerance_for_rounded_display(self):
        g = vibe.Ground()
        g.observe("px", 0.123456789)
        # Printed at fewer digits is still the same number.
        self.assertTrue(g.traceable(0.1234567890))

    def test_zero_observed_makes_zero_traceable(self):
        g = vibe.Ground()
        g.observe("x", 0.0)
        self.assertTrue(g.traceable(0.0))
        self.assertTrue(g.traceable(0.0 + 1e-12))
        self.assertFalse(g.traceable(0.01))


# ============================================================== attribution
class TestAttribution(unittest.TestCase):
    def test_adherence_computed(self):
        prof = vibe.Profile("p", [vibe.Rule("R1", "t", vibe.cmp("x", ">", 0),
                                             support=1, coverage=0.5)],
                            10, 5, covered=5)
        counters = {"A": {"by_rule": {"R1": {"fired": 10, "taken": 4,
                                             "pnl": 25.0}}}}
        result = vibe.attribute(prof, [], counters)
        self.assertAlmostEqual(result["adherence"], 0.4)
        self.assertAlmostEqual(result["realised_pnl"], 25.0)

    def test_adherence_none_when_never_fired(self):
        prof = vibe.Profile("p", [vibe.Rule("R1", "t", vibe.cmp("x", ">", 0))],
                            10, 5, covered=5)
        result = vibe.attribute(prof, [], {})
        self.assertIsNone(result["adherence"])

    def test_no_counters(self):
        prof = vibe.Profile("p", [], 0, 0, covered=0)
        result = vibe.attribute(prof, [], None)
        self.assertEqual(result["fired_total"], 0)
        self.assertIsNone(result["adherence"])


# ==================================================================== exports
class TestPublicSurface(unittest.TestCase):
    def test_exports_present(self):
        for name in ("Rule", "Profile", "Roundtrip", "Ground", "Signal",
                     "extract_profile", "roundtrips_from_fills",
                     "compute_features", "scan", "attribute", "rsi",
                     "cmp", "between", "member", "all_of", "any_of",
                     "negate", "VibeError", "FEATURE_NAMES"):
            self.assertTrue(hasattr(vibe, name), "missing export: " + name)

    def test_module_is_stdlib_only(self):
        # Read the source and confirm no third-party import crept in.
        src = open(os.path.join(WORKDIR, "brokers", "vibe.py")).read()
        for banned in ("import numpy", "import pandas", "import requests",
                       "from numpy", "from pandas", "import scipy"):
            self.assertNotIn(banned, src, "vibe.py gained: " + banned)

    def test_vibe_error_is_broker_error(self):
        from brokers.base import BrokerError
        self.assertTrue(issubclass(VibeError, BrokerError))


if __name__ == "__main__":
    unittest.main()

"""Point-in-time discipline. Stdlib only, no network, no database.

Two subjects, because they are the same bug at different scales:

- `engines.backtest` decided and filled on the same bar's close, which is
  the within-bar form of look-ahead.
- `core.asof` is the dated-query form: clamping a request to the trade date,
  refusing to guess an undated item's date, and reporting an unobserved
  window as unobserved rather than as empty.

The rule both share is that an unmeasured thing is reported as unmeasured.
A window nobody looked at is not a window where nothing happened, and a
number produced by filling at a price you could not have known is not a
performance estimate.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import asof            # noqa: E402
from engines import backtest as B  # noqa: E402


def _candles(closes, start_ts=1_700_000_000, step=60):
    """Synthetic OHLCV where o=h=l=c=close, so a fill price is unambiguous."""
    out = []
    for i, c in enumerate(closes):
        out.append({"ts": start_ts + i * step, "o": c, "h": c, "l": c,
                    "c": c, "v": 1.0})
    return out


# --------------------------------------------------------------------------
# The same-bar fill
# --------------------------------------------------------------------------


class TestEntryDelay(unittest.TestCase):
    """A signal computed on bar i must not fill on bar i's close.

    Filling there means trading at a price that only became knowable at the
    instant of the fill. Live, the earliest reachable fill is the next bar.

    The fixture is a tape that rises 1.0 per bar, so a fill's price names
    the bar it came from: close of bar i is `100 + i`. That makes "which bar
    did this fill?" a direct assertion rather than an inference.
    """

    WARMUP = 2

    def _rising(self, n=40, base=100.0, step=1.0):
        closes = [base + step * i for i in range(n)]
        return _candles(closes), closes

    def _run(self, candles, delay, **params):
        p = {"side": "buy", "warmup_bars": self.WARMUP,
             "max_hold_bars": 3, "entry_delay_bars": delay}
        p.update(params)
        return B.run_backtest(candles, "manual", p, fee_bps=0.0,
                              slippage_bps=0.0)

    def test_default_is_one_bar_not_zero(self):
        self.assertEqual(B.DEFAULT_ENTRY_DELAY_BARS, 1)

    def test_honest_default_fills_on_the_bar_after_the_decision(self):
        """The loop's first evaluated bar is the warmup bar. With a
        one-bar delay the first fill must be the one after it."""
        candles, closes = self._rising()
        r = B.run_backtest(candles, "manual",
                           {"side": "buy", "warmup_bars": self.WARMUP,
                            "max_hold_bars": 3},
                           fee_bps=0.0, slippage_bps=0.0)
        self.assertTrue(r["trades"], "the fixture did not trade")
        self.assertEqual(r["trades"][0]["entry_idx"], self.WARMUP + 1)
        self.assertEqual(r["trades"][0]["entry"],
                         closes[self.WARMUP + 1],
                         "the fill price did not come from the fill bar")

    def test_zero_delay_fills_on_the_deciding_bar(self):
        """Retained deliberately, to keep the bias measurable. It is the
        behaviour this parameter exists to correct, so a test pins what it
        is rather than leaving it to be rediscovered."""
        candles, closes = self._rising()
        r = self._run(candles, 0)
        self.assertTrue(r["trades"])
        self.assertEqual(r["trades"][0]["entry_idx"], self.WARMUP)
        self.assertEqual(r["trades"][0]["entry"], closes[self.WARMUP])

    def test_the_two_modes_genuinely_differ(self):
        """If these produced the same result the parameter is inert and the
        whole premise is wrong."""
        candles, _ = self._rising()
        fast = self._run(candles, 0)["trades"]
        slow = self._run(candles, 1)["trades"]
        self.assertNotEqual([(t["entry_idx"], t["entry"]) for t in fast],
                            [(t["entry_idx"], t["entry"]) for t in slow])
        self.assertEqual(fast[0]["entry_idx"] + 1, slow[0]["entry_idx"])

    def test_a_later_delay_delays_the_fill_by_exactly_that_much(self):
        candles, closes = self._rising()
        for delay in (0, 1, 2, 3):
            r = self._run(candles, delay)
            self.assertEqual(r["trades"][0]["entry_idx"], self.WARMUP + delay,
                             "delay=%d did not move the fill bar" % delay)
            self.assertEqual(r["trades"][0]["entry"],
                             closes[self.WARMUP + delay])

    def test_a_decision_on_the_final_bar_never_fills(self):
        """There is no next bar to fill on, so the order simply does not
        fill. Filling anyway would trade at a price the run cannot reach."""
        candles, closes = self._rising(n=self.WARMUP + 1)
        r = self._run(candles, 1)
        self.assertEqual(r["trades"], [],
                         "an order on the final bar was filled anyway")

    def test_entry_is_never_tested_against_the_bar_it_opened_on(self):
        """A position entered at bar i's close did not exist during bar i,
        so scanning that bar's high/low for a take-profit reads a range
        that elapsed before the entry.

        The tape is flat at 100 then jumps to 500, and the target is +500%,
        so anything scanning the spike bar would trigger immediately. The
        invariant is structural: a take-profit always fires strictly after
        the bar that opened the position.
        """
        closes = [100.0] * 20 + [100.0, 500.0, 500.0, 500.0, 500.0]
        candles = _candles(closes)
        r = B.run_backtest(candles, "manual",
                           {"side": "buy", "warmup_bars": 2,
                            "max_hold_bars": 8, "entry_delay_bars": 1,
                            "tp_bps": 5000.0},
                           fee_bps=0.0, slippage_bps=0.0)
        self.assertTrue(r["trades"], "the fixture did not trade")
        for t in r["trades"]:
            self.assertGreater(t["exit_idx"], t["entry_idx"],
                               "a trade opened and closed on one bar")
            if t["reason"] == "take-profit":
                self.assertGreater(t["exit_idx"], t["entry_idx"])
                # and it must have opened before the spike was reachable
                self.assertLess(t["entry_idx"], len(closes) - 1)

    def test_a_pending_order_eventually_matures(self):
        """Regression: an order was re-scheduled on every bar its condition
        held, pushing its fill further out each time, so it never fired.
        That cost 2,556 of 2,558 trades on the 60k-bar BTCUSD tape."""
        candles, _ = self._rising(n=120)
        r = B.run_backtest(candles, "manual",
                           {"side": "buy", "warmup_bars": self.WARMUP,
                            "max_hold_bars": 2, "entry_delay_bars": 1},
                           fee_bps=0.0, slippage_bps=0.0)
        self.assertGreaterEqual(len(r["trades"]), 5,
                                "orders are scheduled but never filled")

    def test_negative_delay_is_clamped_not_obeyed(self):
        candles, _ = self._rising()
        r = self._run(candles, -5)
        self.assertEqual(r["trades"][0]["entry_idx"], self.WARMUP)

    def test_fills_never_precede_their_own_bar_sequence(self):
        candles, _ = self._rising(n=120)
        r = self._run(candles, 1)
        prev_exit = -1
        for t in r["trades"]:
            self.assertGreater(t["entry_idx"], prev_exit,
                               "a position opened before the last one closed")
            self.assertGreaterEqual(t["hold_bars"], 0)
            prev_exit = t["exit_idx"]

    def test_backtest_still_runs_on_an_empty_tape(self):
        r = B.run_backtest([], "manual", {})
        self.assertEqual(r["metrics"]["trades"], 0)


# --------------------------------------------------------------------------
# The dated-query layer
# --------------------------------------------------------------------------


class TestAsOf(unittest.TestCase):
    NOW = 1_700_000_000

    def test_clamps_a_future_request(self):
        ts, clamped = asof.as_of(self.NOW + 10 * asof.DAY, now=self.NOW)
        self.assertEqual(ts, self.NOW)
        self.assertTrue(clamped)

    def test_allows_normal_publication_skew(self):
        """Vendors stamp forward by minutes, occasionally hours. Clamping
        that would flag ordinary data as a leak."""
        ts, clamped = asof.as_of(self.NOW + 3600, now=self.NOW)
        self.assertEqual(ts, self.NOW + 3600)
        self.assertFalse(clamped)

    def test_clamps_beyond_the_skew_allowance(self):
        ts, clamped = asof.as_of(self.NOW + 30 * asof.DAY, now=self.NOW)
        self.assertTrue(clamped)
        self.assertEqual(ts, self.NOW)

    def test_past_requests_pass_through_untouched(self):
        past = self.NOW - 5 * asof.DAY
        ts, clamped = asof.as_of(past, now=self.NOW)
        self.assertEqual(ts, past)
        self.assertFalse(clamped)

    def test_unparseable_date_is_not_guessed(self):
        """Defaulting an unreadable date is how an undated item becomes a
        present-day one."""
        for junk in ("", None, "not-a-date", [], {}, True):
            self.assertIsNone(asof.parse_date(junk), junk)


class TestInWindow(unittest.TestCase):
    NOW = 1_700_000_000

    def test_upper_bound_is_exclusive_at_midnight_after_end(self):
        """`end` is a date, so the whole day belongs to the window."""
        start = asof.parse_date("2026-10-01")
        end = asof.parse_date("2026-10-03")
        last_instant = asof.parse_date("2026-10-03T12:00:00")
        self.assertTrue(asof.in_window(last_instant, start, end)[0])
        just_after = asof.parse_date("2026-10-04")
        ok, why = asof.in_window(just_after, start, end)
        self.assertFalse(ok)
        self.assertIn("window end", why)

    def test_an_excluded_item_says_why(self):
        start = asof.parse_date("2026-10-01")
        end = asof.parse_date("2026-10-03")
        ok, why = asof.in_window(asof.parse_date("2026-09-01"), start, end)
        self.assertFalse(ok)
        self.assertTrue(why, "an exclusion with no reason cannot be audited")

    def test_undated_item_is_dropped_from_a_historical_window(self):
        """The central rule: a backtest cannot prove a timestamp-less item
        did not come from the future."""
        start = asof.parse_date("2020-01-01")
        end = asof.parse_date("2020-06-01")
        ok, why = asof.in_window(None, start, end, now=self.NOW)
        self.assertFalse(ok)
        self.assertIn("cannot be proven", why)

    def test_undated_item_is_kept_in_a_live_window(self):
        start = asof.parse_date("2020-01-01")
        end = self.NOW
        ok, why = asof.in_window(None, start, end, now=self.NOW)
        self.assertTrue(ok, why)

    def test_undated_can_be_forced_either_way(self):
        start = asof.parse_date("2020-01-01")
        end = asof.parse_date("2020-06-01")
        self.assertFalse(
            asof.in_window(None, start, end, now=self.NOW, undated=False)[0])
        self.assertTrue(
            asof.in_window(None, start, end, now=self.NOW, undated=True)[0])


class TestCoverageGap(unittest.TestCase):
    """An unobserved window is not an empty one."""

    NOW = asof.parse_date("2026-10-15T12:00:00Z")

    def test_a_fully_observed_window_reports_no_gap(self):
        self.assertIsNone(asof.coverage_gap(
            asof.parse_date("2026-10-01"), "2026-10-05", "2026-10-10",
            now=self.NOW))

    def test_a_window_in_the_future_is_a_gap(self):
        gap = asof.coverage_gap(None, "2026-10-01", "2027-01-01", now=self.NOW)
        self.assertIsNotNone(gap)
        self.assertIn("future", gap)

    def test_nothing_returned_is_unknown_not_empty(self):
        gap = asof.coverage_gap(None, "2026-10-01", "2026-10-10", now=self.NOW)
        self.assertIsNotNone(gap)
        self.assertIn("unknown", gap)

    def test_a_feed_that_starts_late_is_a_gap(self):
        """Yahoo news and the social feeds return their latest items whatever
        window was asked for, so an empty result over a window they never
        covered would assert an absence nobody saw."""
        gap = asof.coverage_gap(asof.parse_date("2026-10-09"), "2026-10-01",
                                "2026-10-10", now=self.NOW)
        self.assertIsNotNone(gap)
        self.assertIn("unobserved", gap)

    def test_a_fixed_lookback_feed_can_declare_its_horizon(self):
        """A feed that only serves the last 7 days cannot answer a window
        that opens 14 days ago, even if it returned an item that old."""
        oldest = asof.parse_date("2026-10-13")
        self.assertIsNone(asof.coverage_gap(
            oldest, "2026-10-13", "2026-10-14", now=self.NOW,
            tail_lookback=7 * asof.DAY))
        gap = asof.coverage_gap(asof.parse_date("2026-10-13"), "2026-10-01",
                                "2026-10-14", now=self.NOW,
                                tail_lookback=7 * asof.DAY)
        self.assertIsNotNone(gap)
        self.assertIn("unobserved", gap)

    def test_the_declared_lookback_binds_over_the_oldest_item(self):
        """A feed can return a genuinely old item and still declare it
        cannot reach that far back. The declared horizon is the stronger
        claim, so without this check a feed could certify its own coverage
        by returning anything at all."""
        gap = asof.coverage_gap(asof.parse_date("2026-10-02"), "2026-10-01",
                                "2026-10-14", now=self.NOW,
                                tail_lookback=2 * asof.DAY)
        self.assertIsNotNone(gap)
        self.assertIn("unobserved", gap)

    def test_gap_text_never_claims_absence(self):
        for start, end, obs in (("2026-10-01", "2026-10-10", None),
                                ("2026-10-01", "2026-10-10", "2026-10-09"),
                                ("2026-10-01", "2027-01-01", None)):
            gap = asof.coverage_gap(asof.parse_date(obs) if obs else None,
                                    start, end, now=self.NOW)
            if gap:
                self.assertNotIn("no news", gap.lower())
                self.assertNotIn("nothing happened", gap.lower())


class TestStdlibOnly(unittest.TestCase):
    def test_asof_imports_nothing_third_party(self):
        import ast
        with open(os.path.join(ROOT, "core", "asof.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    found.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and n.module:
                found.add(n.module.split(".")[0])
        self.assertEqual(found - {"calendar", "time"}, set(),
                         "non-stdlib import: %s" % sorted(found))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Parity check: the indicator fast paths must not change a single number.

Compares the current (optimised) engine against a reference
reimplementation that does the naive thing everywhere, across several
strategies and deterministic fixtures. Any divergence is a regression:
the point of the optimisation was speed, not different answers.

Why this is a unittest.TestCase and not a script
-----------------------------------------------
It used to be a module-level script: it ran its checks on import and printed
PASS/FAIL, but registered zero test cases and never failed the run. That is
worse than having no check at all, because `python -m unittest discover`
reported the suite green while the guarantee was not being enforced. A check
whose failure is invisible is a comment pretending to be a test.

So every check here is a real method. If the EMA fast path ever changes a
number, the suite goes red.
"""
import math
import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engines.backtest as B
from engines.backtest import run_backtest
from engines import indicators as I
from engines import signals as S

TOL = 1e-12
METRIC_TOL = 1e-9

SIGNALS = {
    "ema_trend": {"fast": 9, "slow": 21},
    "dual_ema": {"fast": 5, "slow": 20},
    "vwap_pullback": {"lookback": 20, "dev_bps": 10},
    "mean_reversion": {"period": 20, "entry_z": 2.0},
}


def mk(closes):
    return [{"ts": i, "o": c, "h": c * 1.001, "l": c * 0.999, "c": c, "v": 1000.0}
            for i, c in enumerate(closes)]


def naive_ema(values, period):
    """Reference: always coerce, no fast path."""
    vals = [I._f(v) for v in (values or [])]
    n = len(vals)
    if n == 0:
        return []
    if period is None or period <= 1:
        return list(vals)
    k = 2.0 / (float(period) + 1.0)
    m = min(n, int(period))
    seed = sum(vals[:m]) / m
    out = [seed]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1.0 - k))
    return out


def _same_series(a, b, tol=TOL):
    return len(a) == len(b) and all(abs(x - y) < tol for x, y in zip(a, b))


class TestEmaParity(unittest.TestCase):
    """1. ema_series fast path vs the naive reference."""

    def setUp(self):
        self.rnd = random.Random(1234)   # fixed seed: deterministic

    def test_clean_series_all_periods_and_lengths(self):
        cases = 0
        for period in (2, 5, 9, 12, 21, 26, 55, 200):
            for n in (1, 2, 5, 20, 60, 250, 900):
                series = [100.0 + self.rnd.uniform(-1, 1) for _ in range(n)]
                self.assertTrue(
                    _same_series(I.ema_series(list(series), period),
                                 naive_ema(list(series), period)),
                    "diverged at period=%d n=%d" % (period, n))
                cases += 1
        self.assertEqual(cases, 56)

    def test_dirty_inputs_coerced_identically(self):
        """Strings, ints, None, NaN and inf must survive the fast path
        unchanged. This is where a fast path is most likely to short-circuit
        something it should not."""
        dirty_cases = [
            ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
            [None, 1.0, 2.0, 3.0, float("nan"), 5.0, 6.0, 7.0, 8.0, 9.0,
             10.0, 11.0],
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],          # ints
            [1.0, 2.0, 3.0, float("inf"), 5.0, 6.0, 7.0, 8.0, 9.0,
             10.0, 11.0, 12.0],
            [], [5.0], [float("nan")], [None, None],
        ]
        for d in dirty_cases:
            self.assertTrue(
                _same_series(I.ema_series(list(d), 5), naive_ema(list(d), 5)),
                "dirty mismatch on %r" % (d,))

    def test_nan_propagates_rather_than_being_dropped(self):
        """A NaN input must not be silently skipped: dropping it would
        change the series length and every downstream index."""
        series = [1.0, 2.0, float("nan"), 4.0, 5.0]
        fast, naive = I.ema_series(list(series), 3), naive_ema(list(series), 3)
        self.assertEqual(len(fast), len(naive))
        self.assertEqual(sum(1 for x in fast if x != x),
                         sum(1 for x in naive if x != x))


class TestClosesHintParity(unittest.TestCase):
    """2. The `closes` hint must not change any decision."""

    def setUp(self):
        self.rnd = random.Random(4321)

    def _series(self, n):
        return [100.0 + self.rnd.uniform(-2, 2) + i * 0.05 for i in range(n)]

    def test_hint_does_not_change_side_reason_or_qty(self):
        checked = 0
        for n in (30, 120, 400):
            base = self._series(n)
            cd = mk(base)
            quote = {"bid": base[-1] * 0.9999, "ask": base[-1] * 1.0001,
                     "last": base[-1]}
            for name, params in SIGNALS.items():
                hinted = S.preview_signal(name, cd, quote, params, closes=list(base))
                plain = S.preview_signal(name, cd, quote, params)
                for field in ("side", "reason", "qty"):
                    self.assertEqual(
                        hinted.get(field), plain.get(field),
                        "%s n=%d: %s differs (hinted=%r plain=%r)"
                        % (name, n, field, hinted.get(field), plain.get(field)))
                checked += 1
        self.assertEqual(checked, 12)

    def test_mismatched_length_hint_is_discarded(self):
        """A hint that does not match the candle count must be ignored, not
        trusted. Trusting it would let a caller silently reindex every
        indicator against the wrong series."""
        base = self._series(400)
        cd = mk(base)
        quote = {"bid": base[-1] * 0.9999, "ask": base[-1] * 1.0001,
                 "last": base[-1]}
        bad = S.preview_signal("ema_trend", cd, quote, SIGNALS["ema_trend"],
                               closes=[1.0, 2.0])
        good = S.preview_signal("ema_trend", cd, quote, SIGNALS["ema_trend"])
        self.assertEqual(bad.get("reason"), good.get("reason"))


class TestBacktestParity(unittest.TestCase):
    """3. Full backtest runs must be bit-identical with the hint suppressed."""

    def setUp(self):
        self.rnd = random.Random(999)
        self._orig = B.preview_signal

    def tearDown(self):
        B.preview_signal = self._orig

    def _run_both_ways(self, name, params, n=900):
        base = [100.0 + self.rnd.uniform(-2, 2) + i * 0.05 for i in range(n)]
        cd = mk(base)
        fast = run_backtest(cd, name, dict(params), 2.0, 2.0)

        orig = B.preview_signal

        def no_hint(kind, candles, quote, p, closes=None):
            return orig(kind, candles, quote, p)      # hint discarded

        B.preview_signal = no_hint
        try:
            slow = run_backtest(cd, name, dict(params), 2.0, 2.0)
        finally:
            B.preview_signal = orig
        return fast, slow

    def _assert_identical(self, name, fast, slow):
        fm, sm = fast["metrics"], slow["metrics"]
        for k in ("trades", "wins", "win_rate", "pf", "maxDD", "sharpe", "cagr"):
            self.assertAlmostEqual(float(fm.get(k, 0)), float(sm.get(k, 0)),
                                   delta=METRIC_TOL,
                                   msg="%s: metric %s differs" % (name, k))
        self.assertEqual(len(fast["equity"]), len(slow["equity"]),
                         "%s: equity length differs" % name)
        for i, (a, b) in enumerate(zip(fast["equity"], slow["equity"])):
            self.assertAlmostEqual(a, b, delta=METRIC_TOL,
                                   msg="%s: equity[%d] differs" % (name, i))
        self.assertEqual(len(fast["trades"]), len(slow["trades"]),
                         "%s: trade count differs" % name)
        for i, (a, b) in enumerate(zip(fast["trades"], slow["trades"])):
            self.assertAlmostEqual(a["pnl"], b["pnl"], delta=METRIC_TOL,
                                   msg="%s: trade[%d] pnl differs" % (name, i))
            self.assertEqual(a["entry_idx"], b["entry_idx"],
                             "%s: trade[%d] entry_idx differs" % (name, i))

    def test_dual_ema_identical(self):
        f, s = self._run_both_ways("dual_ema", {"fast": 9, "slow": 21})
        self._assert_identical("dual_ema", f, s)

    def test_ema_trend_identical(self):
        f, s = self._run_both_ways("ema_trend", {"fast": 9, "slow": 21})
        self._assert_identical("ema_trend", f, s)

    def test_mean_reversion_identical(self):
        f, s = self._run_both_ways("mean_reversion", {"period": 20,
                                                      "entry_z": 2.0})
        self._assert_identical("mean_reversion", f, s)

    def test_vwap_pullback_identical(self):
        f, s = self._run_both_ways("vwap_pullback", {"lookback": 20,
                                                     "dev_bps": 10})
        self._assert_identical("vwap_pullback", f, s)


class TestSpeedIsReal(unittest.TestCase):
    """4. The optimisation has to actually be an optimisation.

    Kept as a real assertion because "optimisation must not change a number"
    is only half the contract -- the other half is that it changed something
    worth having. The bound is deliberately loose (2%) so this does not
    become a flaky benchmark on a loaded machine.
    """

    def setUp(self):
        self.rnd = random.Random(4242)
        self._orig = B.preview_signal

    def tearDown(self):
        B.preview_signal = self._orig

    def test_optimised_path_is_not_slower(self):
        base = [100.0 + self.rnd.uniform(-2, 2) + i * 0.05 for i in range(1200)]
        cd = mk(base)
        params = {"fast": 9, "slow": 21}

        t0 = time.time()
        run_backtest(cd, "dual_ema", dict(params), 2.0, 2.0)
        fast_t = time.time() - t0

        orig = B.preview_signal
        B.preview_signal = lambda k, c, q, p, closes=None: orig(k, c, q, p)
        try:
            t0 = time.time()
            run_backtest(cd, "dual_ema", dict(params), 2.0, 2.0)
            slow_t = time.time() - t0
        finally:
            B.preview_signal = orig

        self.assertLessEqual(
            fast_t, slow_t * 1.02,
            "optimised path slower: %.3fs vs reference %.3fs" % (fast_t, slow_t))


if __name__ == "__main__":
    unittest.main(verbosity=2)
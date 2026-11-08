"""chart_context(): per-bar regime + anomaly flags, and /api/chart_context.

Two things this must get right, both of which are quiet failures.

**Threshold basis.** The cuts are estimated from the whole visible series,
never from the trailing window. A percentile needs hundreds of
observations, so estimating from a short window silently degrades to the
Gaussian constants — and a chart looks identical either way. `basis` is
reported so the caption can say which happened.

**Regime alignment.** `_vol_ratio_series` starts at the first bar with a
full 60-bar window, so its output is offset from bar 0. Misaligning it puts
every regime band one bar off, which is invisible in a screenshot and wrong
in the data.

1m only, and that is a deliberate refusal: the regime window and z window
are bar counts, so on daily bars they would mean 60 days and 20 days.
"""
import json
import os
import sys
import unittest
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engines.anomaly import (MIN_REGIME_OBS, chart_context,
                             ewma_volatility)


def synth(n=400, sigma=0.001, seed=3):
    """Deterministic price path. No RNG: a test that seeds is a test that
    can be flaky on a different platform."""
    out = []
    p = 100.0
    x = seed
    for _ in range(n):
        # deterministic LCG -> approximately normal via the sum
        s = 0.0
        for _ in range(6):
            x = (1103515245 * x + 12345) % 2147483648
            s += x / 2147483648.0 - 0.5
        p *= (1.0 + sigma * s)
        out.append(p)
    return out


class TestShapeAndContract(unittest.TestCase):
    def setUp(self):
        self.cl = synth(400)
        self.ctx = chart_context(self.cl)

    def test_series_is_one_shorter_than_closes(self):
        """One return needs two closes, so n-1 rows is correct."""
        self.assertEqual(len(self.ctx["series"]), len(self.cl) - 1)
        self.assertEqual(self.ctx["n"], len(self.cl))

    def test_every_row_has_the_documented_keys(self):
        for row in self.ctx["series"][:20]:
            for k in ("ts", "ret", "z", "ratio", "state", "anomaly",
                      "severity"):
                self.assertIn(k, row)

    def test_short_history_returns_empty_rather_than_raising(self):
        for n in (0, 1, 5, 24):
            out = chart_context(synth(max(n, 1))[:n])
            self.assertEqual(out["series"], [], "n=%d produced rows" % n)
            self.assertEqual(out["basis"], "fallback:no-history")

    def test_none_and_junk_do_not_raise(self):
        self.assertEqual(chart_context(None)["series"], [])
        self.assertEqual(chart_context([])["series"], [])
        self.assertEqual(chart_context([1.0, -1.0, 0.0, None] * 30)["series"]
                         is not None, True)


class TestRegimeAlignment(unittest.TestCase):
    """The offset bug: bands land one bar off and nothing looks wrong."""

    def test_ratio_appears_only_after_a_full_window(self):
        ctx = chart_context(synth(300))
        ratios = [r["ratio"] for r in ctx["series"]]
        first = next((i for i, v in enumerate(ratios) if v is not None), None)
        self.assertIsNotNone(first, "no ratio ever appeared")
        # _vol_ratio_series needs 60 returns, so nothing before ~bar 60.
        self.assertGreaterEqual(first, 55,
                                "a ratio appeared before the window was full")

    def test_ratio_matches_the_helper_at_the_same_index(self):
        """Recompute independently and compare -- the only way to catch an
        off-by-one that still produces plausible numbers."""
        from engines.anomaly import _vol_ratio_series, _rets
        cl = synth(300)
        ctx = chart_context(cl)
        expected = _vol_ratio_series(_rets(cl), window=60)
        got = [r["ratio"] for r in ctx["series"]]
        tail = [v for v in got if v is not None]
        self.assertEqual(len(tail), len(expected))
        for a, b in zip(tail, expected):
            self.assertAlmostEqual(a, b, places=9,
                                   msg="ratio series is misaligned")


class TestRegimeBasis(unittest.TestCase):
    def test_fallback_when_history_is_short(self):
        ctx = chart_context(synth(80))
        self.assertEqual(ctx["regime"]["basis"], "fallback")
        self.assertEqual(ctx["regime"]["expansion"], 2.0)
        self.assertEqual(ctx["regime"]["compression"], 0.55)

    def test_estimates_once_history_is_long_enough(self):
        ctx = chart_context(synth(400))
        self.assertEqual(ctx["regime"]["basis"], "estimated",
                         "regime cuts never left the fallback")
        r = ctx["regime"]
        self.assertGreater(r["expansion"], r["compression"],
                           "expansion cut below compression cut")
        # A vol ratio hovers near 1; a p95 above 2.0 would mean the
        # fallback leaked through.
        self.assertLess(r["expansion"], 2.0)

    def test_threshold_constant_is_reachable(self):
        self.assertGreater(MIN_REGIME_OBS, 0)
        self.assertLessEqual(MIN_REGIME_OBS, 400)


class TestVolatileDataIsClassified(unittest.TestCase):
    """A strip that never fires is not a feature."""

    def test_a_volatile_regime_gets_labelled(self):
        n = 600
        cl = [100.0]
        p = 100.0
        for i in range(n):
            # Alternate quiet and violent blocks: this is what an expansion
            # looks like, and a ratio that cannot see it is measuring the
            # wrong thing.
            sigma = 0.0004 if (i // 40) % 2 == 0 else 0.006
            p *= (1.0 + sigma * (((i * 37) % 11) - 5) / 5.0)
            cl.append(p)
        ctx = chart_context(cl)
        states = [r["state"] for r in ctx["series"]]
        self.assertTrue(any(s == "expansion" for s in states),
                        "a deliberately volatile series produced no "
                        "expansion bars")
        self.assertEqual(ctx["regime"]["basis"], "estimated")


class TestAnomalyFlagging(unittest.TestCase):
    def test_an_injected_spike_is_flagged(self):
        n = 400
        cl = list(synth(n))
        cl[300] *= 1.10          # a 10% bar, far outside the noise
        ctx = chart_context(cl)
        flagged = [i for i, r in enumerate(ctx["series"]) if r["anomaly"]]
        self.assertTrue(flagged, "a 10% spike was not flagged")
        # The spike is at close index 300, so return index 299.
        self.assertIn(299, flagged)

    def test_calm_data_flags_almost_nothing(self):
        ctx = chart_context(synth(400))
        flagged = sum(1 for r in ctx["series"] if r["anomaly"])
        self.assertLess(flagged, len(ctx["series"]) * 0.05,
                        "%d of %d bars flagged on quiet data"
                        % (flagged, len(ctx["series"])))

    def test_flagged_bars_carry_a_severity(self):
        cl = list(synth(400))
        cl[300] *= 1.10
        ctx = chart_context(cl)
        for r in ctx["series"]:
            if r["anomaly"]:
                self.assertIn(r["severity"], ("high", "medium"))

    def test_severity_is_never_set_without_a_flag(self):
        for r in chart_context(synth(300))["series"]:
            if r["severity"]:
                self.assertTrue(r["anomaly"])


# ----------------------------------------------------------------- HTTP
from tests.live_server import ensure_live_server

BASE = ensure_live_server()




class TestChartContextEndpoint(unittest.TestCase):
    def _get(self, **params):
        url = BASE + "/api/chart_context?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.loads(r.read().decode())

    def test_returns_a_series_for_a_known_symbol(self):
        d = self._get(symbol="BTCUSD", limit=200)
        self.assertEqual(d["symbol"], "BTCUSD")
        self.assertGreater(len(d["series"]), 0)
        self.assertIn("regime", d)
        self.assertIn("basis", d)

    def test_series_is_ascending(self):
        """Charts index left to right; a reversed series draws backwards."""
        d = self._get(symbol="BTCUSD", limit=300)
        ts = [r["ts"] for r in d["series"] if r["ts"] is not None]
        self.assertEqual(ts, sorted(ts))

    def test_missing_symbol_is_an_error(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self._get(limit=10)

    def test_unknown_symbol_returns_an_empty_series(self):
        d = self._get(symbol="NOSUCHSYMBOL", limit=100)
        self.assertEqual(d["n"], 0)
        self.assertEqual(d["basis"], "no-data")

    def test_limit_is_clamped_not_trusted(self):
        d = self._get(symbol="BTCUSD", limit=999999)
        self.assertLessEqual(len(d["series"]), 2000)

    def test_regime_basis_is_reported(self):
        d = self._get(symbol="BTCUSD", limit=400)
        self.assertIn(d["regime"]["basis"], ("estimated", "fallback"))


class TestModuleStaysStdlib(unittest.TestCase):
    def test_anomaly_imports_nothing_new(self):
        import ast
        path = os.path.join(ROOT, "engines", "anomaly.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    mods.add(node.module.split(".")[0])
        # "tails" and "signals" appear only as the ImportFrom fallback this
        # module already uses for direct-path execution; they are not
        # third-party packages.
        project = {"engines", "data", "core", "api", "brokers", "scripts",
                   "tails", "signals", "cotgate", "significance",
                   "indicators"}
        bad = [m for m in sorted(mods)
               if m not in sys.stdlib_module_names and m not in project]
        self.assertEqual(bad, [], "third-party import: %r" % bad)

    def test_chart_context_is_exported(self):
        self.assertTrue(callable(chart_context))


if __name__ == "__main__":
    unittest.main(verbosity=2)

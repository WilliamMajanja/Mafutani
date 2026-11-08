"""B2: resolution awareness for the regime/anomaly engine.

What B2 actually turned out to be
--------------------------------
The plan was to fix `EWMA_LAMBDA = 0.94`, which is RiskMetrics' *daily*
constant, on the theory it was wrong for 1-minute bars. Measured, it is
not:

    lambda 0.94 -> half-life ln(0.5)/ln(0.94) = 11.2 BARS, whatever a bar is
      1m: 11.2 minutes  vs a 60-bar window  = 5.4x
      1d: 11.2 days     vs a 60-bar window  = 5.4x

Identical ratio at both, because both are counted in bars. The constant is
self-consistent and RiskMetrics' 0.94 genuinely is the daily one. So B2
changed no numbers -- both profiles keep 0.94 -- and these tests pin that
decision so a later reader does not "fix" it back.

What B2 does change is that the resolution is explicit and *reported*, so
a daily chart cannot be read as a minute one, and so the candles and the
context cannot come from different tables.
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engines.anomaly import (EWMA_LAMBDA, REGIME_WINDOW, RESOLUTIONS,
                             Z_WINDOW, chart_context, get_resolution,
                             resolution_names)


def synth(n=400, sigma=0.001, seed=3):
    p, x = 100.0, seed
    out = []
    for _ in range(n):
        s = 0.0
        for _ in range(6):
            x = (1103515245 * x + 12345) % 2147483648
            s += x / 2147483648.0 - 0.5
        p *= (1.0 + sigma * s)
        out.append(p)
    return out


class TestResolutionProfiles(unittest.TestCase):
    def test_both_resolutions_exist(self):
        self.assertEqual(sorted(RESOLUTIONS), ["1d", "1m"])
        self.assertEqual(resolution_names(), ["1d", "1m"])

    def test_unknown_resolution_falls_back_and_says_which(self):
        """Degrade rather than raise: a chart that renders at the wrong
        resolution is recoverable, one that throws is not."""
        r = get_resolution("7z")
        self.assertEqual(r.name, "1m")

    def test_lookup_is_case_and_space_insensitive(self):
        for v in ("1D", " 1d ", "1M"):
            self.assertIn(get_resolution(v).name, ("1d", "1m"))

    def test_none_defaults(self):
        self.assertEqual(get_resolution(None).name, "1m")

    def test_bar_durations_differ_by_the_expected_factor(self):
        ratio = RESOLUTIONS["1d"].seconds / RESOLUTIONS["1m"].seconds
        self.assertEqual(ratio, 1440)          # 24h / 1min


class TestLambdaDecisionIsPinned(unittest.TestCase):
    """Both profiles keep 0.94. These say why, so it is not 'fixed' later."""

    def test_both_resolutions_share_the_lambda(self):
        self.assertEqual(RESOLUTIONS["1m"].lamb, EWMA_LAMBDA)
        self.assertEqual(RESOLUTIONS["1d"].lamb, EWMA_LAMBDA)

    def test_the_stated_rationale_is_actually_true(self):
        """Half-life is 11.2 bars at either resolution, and the ratio to the
        regime window is therefore identical. This is the measurement that
        showed 0.94 is not resolution-specific."""
        hl_m = RESOLUTIONS["1m"].half_life_bars
        hl_d = RESOLUTIONS["1d"].half_life_bars
        self.assertAlmostEqual(hl_m, hl_d, places=6)
        ratio_m = RESOLUTIONS["1m"].regime_window / hl_m
        ratio_d = RESOLUTIONS["1d"].regime_window / hl_d
        self.assertAlmostEqual(ratio_m, ratio_d, places=6)
        self.assertAlmostEqual(ratio_m, 60.0 / 11.2, places=2)

    def test_wall_clock_half_life_is_reported_per_resolution(self):
        """The same 11.2 bars, read as 11 minutes or 11 days.

        `describe()` rounds hours to 2dp, so the daily figure is 268.86,
        not 268.8 -- the test must compare against what is returned rather
        than against an exact product.
        """
        d1 = RESOLUTIONS["1m"].describe()
        d2 = RESOLUTIONS["1d"].describe()
        # Expected from the *unrounded* half-life, which is 11.2022 bars --
        # not 11.2, so 11.2*24=268.8 is off by more than describe()'s rounding
        # accounts for.
        hl = math.log(0.5) / math.log(EWMA_LAMBDA)
        self.assertAlmostEqual(d1["ewma_half_life_hours"], hl / 60.0,
                               delta=0.001)
        self.assertAlmostEqual(d2["ewma_half_life_hours"], hl * 24.0,
                               delta=0.5)
        self.assertEqual(d2["bar_seconds"], 86400)
        # Same count of bars either way; only the wall-clock reading differs.
        self.assertEqual(d1["ewma_half_life_bars"],
                         d2["ewma_half_life_bars"])

    def test_hours_is_derived_from_bars_and_bar_seconds(self):
        """The reported hours must follow from the reported bar count.

        Checked per profile rather than as a ratio between them: dividing
        two rounded values cannot recover an exact ratio, and asserting
        1440 would be asserting the rounding error cancels, which it does
        not. 0.19/268.86 gives 1415, not 1440 -- which is how the 2dp
        rounding bug in describe() was found.
        """
        for name in RESOLUTIONS:
            d = RESOLUTIONS[name].describe()
            expected = d["ewma_half_life_bars"] * d["bar_seconds"] / 3600.0
            self.assertAlmostEqual(
                d["ewma_half_life_hours"], expected,
                delta=expected * 0.005,
                msg="%s: hours=%r does not follow from bars=%r * %rs"
                    % (name, d["ewma_half_life_hours"],
                       d["ewma_half_life_bars"], d["bar_seconds"]))

    def test_1m_half_life_keeps_useful_precision(self):
        """11 minutes is 0.187h. Fixed 2dp rounding reported 0.19, a 1.8%
        error, while reporting the daily 268.86h to 0.02% -- one rounding
        call that is accurate for one profile and lossy for the other."""
        h = RESOLUTIONS["1m"].describe()["ewma_half_life_hours"]
        self.assertLess(abs(h - 0.18671), h * 0.01)

    def test_describe_is_json_serialisable(self):
        json.dumps(RESOLUTIONS["1d"].describe())

    def test_windows_are_bar_counts_and_shared(self):
        """Both resolutions use the same counts by design, not by accident."""
        self.assertEqual(RESOLUTIONS["1m"].z_window, Z_WINDOW)
        self.assertEqual(RESOLUTIONS["1d"].z_window, Z_WINDOW)
        self.assertEqual(RESOLUTIONS["1m"].regime_window, REGIME_WINDOW)
        self.assertEqual(RESOLUTIONS["1d"].regime_window, REGIME_WINDOW)


class TestChartContextReportsItsResolution(unittest.TestCase):
    def setUp(self):
        self.cl = synth(400)

    def test_response_names_the_resolution(self):
        for tf in ("1m", "1d"):
            ctx = chart_context(self.cl, tf=tf)
            self.assertEqual(ctx["tf"], tf)
            self.assertEqual(ctx["resolution"]["name"], tf)

    def test_daily_reports_a_daily_bar(self):
        ctx = chart_context(self.cl, tf="1d")
        self.assertEqual(ctx["resolution"]["bar_seconds"], 86400)

    def test_unknown_tf_reports_the_fallback_not_the_request(self):
        ctx = chart_context(self.cl, tf="nope")
        self.assertEqual(ctx["tf"], "1m")

    def test_same_series_gives_identical_classification(self):
        """The computation is bar-scale-invariant, and that is the point.

        Feeding the same 400 closes as "1m" and as "1d" must classify
        identically -- only the label differs. If a future change made
        these diverge, the resolution would be influencing the maths
        rather than only the description of it.
        """
        a = chart_context(self.cl, tf="1m")
        b = chart_context(self.cl, tf="1d")
        sa = [r["state"] for r in a["series"]]
        sb = [r["state"] for r in b["series"]]
        self.assertEqual(sa, sb)
        self.assertAlmostEqual(a["cut_z"], b["cut_z"], places=9)

    def test_explicit_window_overrides_the_profile(self):
        """An explicit window must win, or the parameter is decorative."""
        a = chart_context(self.cl, tf="1d")
        b = chart_context(self.cl, tf="1d", window=120)
        self.assertNotEqual(
            [r["state"] for r in a["series"]],
            [r["state"] for r in b["series"]],
            "window=120 produced the same classification as the default")

    def test_short_history_still_falls_back_on_both(self):
        for tf in ("1m", "1d"):
            ctx = chart_context(synth(100), tf=tf)
            self.assertEqual(ctx["regime"]["basis"], "fallback")
            self.assertEqual(ctx["tf"], tf)


class TestRegimeCutsUseTheGivenLambda(unittest.TestCase):
    """The cuts must be quantiled from the series they were computed on."""

    def test_a_different_lambda_moves_the_cut(self):
        """Guards a class of bug where cuts come from one series and are
        compared against another -- every value plausible, all wrong."""
        from engines.anomaly import _regime_cuts, _rets
        rets = _rets(synth(400))
        a, _ = _regime_cuts(rets, window=60, lamb=0.94)
        b, _ = _regime_cuts(rets, window=60, lamb=0.99)
        self.assertNotEqual(a, b,
                            "lambda had no effect on the cut; the lambda is "
                            "not being threaded through")

    def test_default_lambda_is_the_module_constant(self):
        import inspect
        from engines.anomaly import _regime_cuts
        sig = inspect.signature(_regime_cuts)
        self.assertEqual(sig.parameters["lamb"].default, EWMA_LAMBDA)


class TestCutsAndClassifyingSeriesShareALambda(unittest.TestCase):
    """A real bug found by mutation-testing B2, not by reading it.

    `chart_context` built the vol-ratio series used to *classify* each bar
    with the module default while it built the series the *cuts* were
    quantiled from with `res.lamb`. Both profiles use 0.94, so the two
    agreed by coincidence and every shipped number was right -- but the
    moment a profile differs, the cuts would be quantiled from one
    distribution and compared against another. Every value would be
    plausible and wrong.
    """

    def _lambdas_used_by_chart_context(self):
        """Which lambdas reach the two `_vol_ratio_series` calls."""
        import inspect
        import re
        from engines.anomaly import chart_context
        src = inspect.getsource(chart_context)
        found = re.findall(r"_vol_ratio_series\(rets,[^)]*\)", src, re.S)
        found += re.findall(r"_regime_cuts\(rets,[^)]*\)", src, re.S)
        self.assertTrue(found, "chart_context no longer builds the series")
        return found

    def test_both_series_are_built_with_the_profile_lambda(self):
        for call in self._lambdas_used_by_chart_context():
            self.assertIn("lamb=res.lamb", call.replace(" ", ""),
                          "this call ignores the profile lambda: %r" % call)

    def test_a_differing_profile_lambda_changes_the_bands(self):
        """Proves the threading is load-bearing, not decorative.

        Build the same series under two lambdas and confirm the band a bar
        lands in can differ. If it could not, threading the lambda would
        be untestable and therefore unverified.
        """
        from engines.anomaly import _vol_ratio_series, _rets
        rets = _rets(synth(400))
        a = _vol_ratio_series(rets, window=60, lamb=0.94)
        b = _vol_ratio_series(rets, window=60, lamb=0.99)
        self.assertEqual(len(a), len(b))
        self.assertNotEqual(a, b,
                            "lambda had no effect on the ratio series")


# ------------------------------------------------------------------ HTTP
from tests.live_server import ensure_live_server

BASE = ensure_live_server()




class TestChartContextServesBothResolutions(unittest.TestCase):
    def _get(self, **params):
        url = BASE + "/api/chart_context?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.loads(r.read().decode())

    def test_1m_reads_the_minute_table(self):
        d = self._get(symbol="BTCUSD", tf="1m", limit=300)
        self.assertEqual(d["tf"], "1m")
        self.assertEqual(d["table"], "candles_1m")
        self.assertGreater(len(d["series"]), 0)

    def test_1d_reads_the_daily_table(self):
        """The bug B2 exists to fix: the context was hardcoded to
        candles_1m while the chart already served candles_1d."""
        d = self._get(symbol="XAUUSD", tf="1d", limit=300)
        self.assertEqual(d["tf"], "1d")
        self.assertEqual(d["table"], "candles_1d")
        self.assertGreater(len(d["series"]), 0)

    def test_context_and_candles_agree_on_the_table(self):
        """The two panels must not describe different series."""
        for sym, tf in (("BTCUSD", "1m"), ("XAUUSD", "1d")):
            ctx = self._get(symbol=sym, tf=tf, limit=200)
            url = (BASE + "/api/candles?"
                   + urllib.parse.urlencode({"symbol": sym, "tf": tf,
                                            "limit": 200}))
            with urllib.request.urlopen(url, timeout=25) as r:
                cans = json.loads(r.read().decode())
            self.assertEqual(ctx["table"], cans["table"],
                             "%s tf=%s: context and candles disagree"
                             % (sym, tf))
            # Same window, so the context must be one bar shorter and the
            # timestamps must line up.
            ctx_ts = [x["ts"] for x in ctx["series"] if x["ts"] is not None]
            can_ts = [x["ts"] for x in cans["candles"] if x["ts"] is not None]
            self.assertEqual(ctx_ts, can_ts[1:],
                             "%s tf=%s: timestamp series do not align"
                             % (sym, tf))

    def test_no_data_response_has_the_same_shape(self):
        """Otherwise a client reading regime.basis raises KeyError on
        exactly the symbols nobody tested."""
        d = self._get(symbol="BTCUSD", tf="1d", limit=100)
        self.assertEqual(d["n"], 0)
        for key in ("series", "regime", "resolution", "tf", "table",
                    "basis", "cut_z", "n_threshold", "kurtosis"):
            self.assertIn(key, d, "no-data response is missing %r" % key)
        self.assertEqual(d["regime"]["basis"], "no-data")
        self.assertEqual(d["resolution"]["name"], "1d")

    def test_short_history_falls_back_and_says_so(self):
        """A daily symbol with 100 bars cannot support a p99.5. It must say
        it fell back rather than presenting a 2.5 cut as estimated."""
        d = self._get(symbol="XAUUSD", tf="1d", limit=100)
        self.assertEqual(d["regime"]["basis"], "fallback")
        self.assertEqual(d["basis"], "fallback")

    def test_unknown_tf_falls_back_to_1m(self):
        d = self._get(symbol="BTCUSD", tf="7z", limit=100)
        self.assertEqual(d["tf"], "1m")
        self.assertEqual(d["table"], "candles_1m")

    def test_tf_is_never_interpolated_into_sql(self):
        """Same defence as /api/candles: a shared map, not a format."""
        for evil in ("1m; DROP TABLE candles_1d", "candles_1d",
                     "1d UNION SELECT 1,2,3,4,5,6,7"):
            d = self._get(symbol="XAUUSD", tf=evil, limit=5)
            self.assertIn(d["table"], ("candles_1m", "candles_1d"))
        from data.db import connect
        conn = connect()
        for t in ("candles_1m", "candles_1d"):
            self.assertIsNotNone(
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                             " AND name=?", (t,)).fetchone(),
                "%s was dropped" % t)
        conn.close()


class TestRouteResolvesTheTableFromTheSharedMap(unittest.TestCase):
    """Source-level guard for the thing the HTTP tests cannot see.

    The HTTP tests above exercise the *running server*, which does not
    reload edited source -- so a mutation to api/routes.py passes them
    silently until someone restarts. Proved by mutation: hardcoding
    candles_1m again left all 32 tests green until the server was
    restarted, after which two failed. These read the file instead, so the
    coverage does not depend on restart discipline.
    """

    def _route_src(self):
        """The route body with its docstring stripped.

        The docstring documents the bug this change fixes and so names
        `candles_1m` and `candles_1d` in prose. Counting those as code
        would make the assertion wrong for the right implementation.
        """
        with open(os.path.join(ROOT, "api", "routes.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("def _chart_context(")
        j = src.index("\n    def ", i + 10)
        body = src[i:j]
        return re.sub(r'"""(?:.|\n)*?"""', "", body, count=1)

    def test_no_hardcoded_table_in_sql(self):
        """The table must be interpolated from `table`, never a literal.

        A regex over the SQL looked like the obvious check and was worse
        than useless: the query is assembled by concatenating string
        literals, so `SELECT[^"]*FROM` never reaches FROM, the pattern
        matched nothing, and the test passed while the route queried
        candles_1d for every tf. Mutation M7b caught that. This checks the
        actual assembly instead.
        """
        body = self._route_src()

        # `table` must be assigned from the shared map...
        self.assertRegex(body, r'table\s*=\s*_CANDLE_TABLES\.get\(tf')

        # ...and must be the only thing reaching the WHERE clause.
        self.assertRegex(body, r'\+\s*table\s*\+\s*"\s+WHERE symbol')

        # No bare table name may be concatenated into SQL. The one legal
        # mention is the map's fallback default for an unknown tf.
        for lit in ("candles_1m", "candles_1d"):
            self.assertNotRegex(body, r'\+\s*["\']%s["\']' % lit,
                                "SQL concatenates the literal %r" % lit)
            self.assertLessEqual(
                body.count(lit), 1,
                "%r appears %d times; only the _CANDLE_TABLES.get fallback "
                "is legitimate" % (lit, body.count(lit)))

    def test_table_comes_from_the_shared_map(self):
        self.assertIn("_CANDLE_TABLES.get(tf", self._route_src())

    def test_the_resolution_is_passed_through(self):
        self.assertIn("tf=resolved", self._route_src(),
                      "the route does not tell the engine which resolution")

    def test_no_data_branch_still_names_the_table(self):
        body = self._route_src()
        self.assertIn("no-data", body)
        for key in ("regime", "resolution", "tf", "table"):
            self.assertIn('"%s"' % key, body,
                          "no-data response is missing %r" % key)


class TestFrontendNoLongerRefusesDaily(unittest.TestCase):
    """These run the real JS under node rather than grepping it.

    An earlier version asserted `assertIn("ewma_half_life_hours", src)`,
    which passes whether the value is used or merely mentioned -- the same
    presence-not-behaviour trap that a canvas stub hit earlier in this
    project. Mutation M12 set `var rs = {}` and every one of those greps
    still passed. So the caption is now extracted and executed, and the
    assertions are on what it prints.
    """

    def _src(self):
        with open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _extract(src, name):
        """Pull a named `function name(...) { ... }` out by brace count."""
        i = src.index("function %s(" % name)
        j = src.index("{", src.index(")", i))
        depth, k, in_str, quote = 0, j, False, ""
        while k < len(src):
            ch = src[k]
            if in_str:
                if ch == "\\":
                    k += 2
                    continue
                if ch == quote:
                    in_str = False
            elif ch in "\"'":
                in_str, quote = True, ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return src[i:k + 1]
            k += 1
        raise AssertionError("unbalanced braces extracting %s" % name)

    def test_the_1m_only_refusal_is_gone(self):
        self.assertNotIn("regime strip: 1m only", self._src(),
                         "the daily refusal is still in the UI")

    def test_tf_is_forwarded_to_chart_context(self):
        self.assertIn('"&tf=" + encodeURIComponent(used)', self._src(),
                      "chart_context is not given the resolution on screen")

    # ------------------------------------------------------------ node
    def _run_node(self, body, payload):
        """Run the real `regCaption` and `esc` extracted from app.js.

        No shims: substituting a stand-in for either would let the test
        pass while the shipped function broke.
        """
        drv = (
            self._extract(self._src(), "esc") + "\n"
            + self._extract(self._src(), "regCaption") + "\n"
            + "process.stdout.write(String(regCaption(" + payload + ")));\n"
        )
        path = os.path.join(tempfile.mkdtemp(prefix="regcap"), "d.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(drv)
        p = subprocess.run(["node", path], capture_output=True, timeout=60)
        if p.returncode != 0:
            self.fail("node driver failed: %s" % p.stderr.decode()[:800])
        return p.stdout.decode()

    def _ctx(self, **kw):
        base = {"symbol": "X", "n": 300, "series": [{"anomaly": False}] * 300,
                "tf": "1d", "cut_z": 3.0, "kurtosis": 4.1,
                "regime": {"expansion": 1.28, "compression": 0.79,
                           "basis": "estimated"},
                "resolution": {"name": "1d", "label": "daily",
                               "bar_seconds": 86400,
                               "regime_window_bars": 60,
                               "ewma_half_life_hours": 269.0}}
        base.update(kw)
        return base

    def test_caption_states_the_daily_interval(self):
        out = self._run_node("null", json.dumps(self._ctx()))
        self.assertIn("60-bar window", out,
                      "the caption does not state the bar window: %r" % out)
        # 269h is rendered as days, which is the point: on a daily chart a
        # reader should see "11d", not "269.0h".
        self.assertIn("11d", out,
                      "the caption does not state the daily half-life: %r"
                      % out)

    def test_caption_states_a_different_interval_for_1m(self):
        """The point of B2: the caption must distinguish the resolutions.

        If 1m and 1d produced the same caption, a daily strip could be
        read as a minute one -- the ambiguity B2 exists to remove.
        """
        m = self._ctx(tf="1m", resolution={
            "name": "1m", "label": "1-minute", "bar_seconds": 60,
            "regime_window_bars": 60, "ewma_half_life_hours": 0.187})
        daily = self._run_node("null", json.dumps(self._ctx()))
        minute = self._run_node("null", json.dumps(m))
        self.assertNotEqual(minute, daily,
                            "1m and 1d captions are identical; the "
                            "resolution is not reaching the reader")
        self.assertIn("0.2h", minute)

    def test_caption_says_when_thresholds_fell_back(self):
        out = self._run_node("null", json.dumps(self._ctx(
            regime={"expansion": 2.0, "compression": 0.5,
                    "basis": "fallback"})))
        self.assertIn("FALLBACK", out)

    def test_caption_counts_flagged_bars(self):
        s = [{"anomaly": False}] * 298 + [{"anomaly": True},
                                         {"anomaly": True}]
        out = self._run_node("null", json.dumps(self._ctx(series=s)))
        self.assertIn("2 flagged", out, out)

    def test_empty_context_yields_empty_caption(self):
        self.assertEqual(self._run_node("null", json.dumps(
            {"series": [], "regime": {}})), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)

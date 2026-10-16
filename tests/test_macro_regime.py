"""Macro regime: derived from stored data, and absent when it cannot be.

The central property is the one that is easiest to get wrong in the
*convenient* direction. The obvious implementation defaults VIX to 17, the
curve to +0.5, and emits a regime -- which means a deployment with no FRED
data silently behaves as though macro were neutral, and nobody can tell the
gate was inert. A gate that is quietly always-on looks exactly like a
control.

So: `regime()` returns `None` when it cannot measure, and `None` is a
different answer from "neutral". `gate()` says which case it hit.

These build a database by hand rather than fetching, so the thresholds are
tested against constructed histories instead of whatever FRED served today.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engines import macro  # noqa: E402


def _seed(conn, series_id, pairs):
    """pairs: [(date, value)] ascending."""
    meta = {"units": "percent", "category": "test", "title": series_id}
    conn.executemany(
        "INSERT INTO macro_observations (series_id, obs_date, value, units,"
        " category, title, source) VALUES (?, ?, ?, ?, ?, ?, 'test')",
        [(series_id, d, v, meta["units"], meta["category"], meta["title"])
         for d, v in pairs])


class MacroBase(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("DB_PATH")
        self._tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"] = os.path.join(self._tmp, "m.db")
        from data.db import init_db
        self.conn = init_db()

    def tearDown(self):
        self.conn.close()
        if self._prev is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._prev


class TestUnmeasuredIsNotNeutral(MacroBase):
    def test_empty_database_measures_nothing(self):
        r = macro.regime(self.conn)
        self.assertIsNone(r["regime"], "an empty database produced a regime")
        self.assertEqual(r["confidence"], 0.0)

    def test_the_reason_names_the_remedy(self):
        r = macro.regime(self.conn)
        self.assertIn("fetch", r["reason"].lower())

    def test_neutral_is_distinguishable_from_unmeasured(self):
        """These must never collapse into each other."""
        self.assertIsNone(macro.regime(self.conn)["regime"])
        _seed(self.conn, "VIXCLS", [("2026-10-14", 17.0)])
        self.conn.commit()
        r = macro.regime(self.conn)
        self.assertIsNotNone(r["regime"])
        self.assertNotEqual(r["regime"], None)

    def test_stale_data_is_unmeasured_not_used(self):
        """A reading from last month is a memory, not a measurement."""
        _seed(self.conn, "VIXCLS", [("2020-01-02", 45.0)])   # would be risk_off
        self.conn.commit()
        r = macro.regime(self.conn, max_age_days=7)
        self.assertIsNone(r["regime"], "a 5-year-old VIX was treated as current")

    def test_gate_reports_that_it_did_not_apply(self):
        g = macro.gate({}, macro.regime(self.conn), block_in=["risk_off"])
        self.assertTrue(g["allowed"], "an unmeasured gate must not veto")
        self.assertIn("unmeasured", g["reason"])


class TestRegimeBands(MacroBase):
    def _today(self, n=1):
        import datetime
        return (datetime.date.today() - datetime.timedelta(days=0)).isoformat()

    def _recent(self, vix, base=15.0, spread=0.0):
        """A plausible recent VIX history ending today at `vix`.

        `today` is written exactly once. An earlier version assigned it after
        the loop, which had already emitted it at i=0, so the seeding hit the
        (series_id, obs_date) primary key and every band test errored on its
        fixture rather than on the logic under test.

        `spread` gives the history a realistic distribution. A history of
        nothing but identical values makes any different today a 100th
        percentile -- arithmetically right, analytically useless -- so any
        test needing a mid-percentile reading must ask for a spread.
        """
        import datetime
        today = datetime.date.today()
        out = [((today - datetime.timedelta(days=i)).isoformat(),
                base + (spread if i % 2 else -spread))
               for i in range(40)]
        out = [(d, v) for d, v in out if d != today.isoformat()]
        out.append((today.isoformat(), vix))
        out.sort()
        _seed(self.conn, "VIXCLS", out)

    def test_high_vix_is_risk_off(self):
        self._recent(35.0)
        self.conn.commit()
        r = macro.regime(self.conn)
        self.assertEqual(r["regime"], "risk_off")
        self.assertIn("vix", r["reason"].lower())

    def test_low_vix_is_risk_on(self):
        self._recent(12.0)
        self.conn.commit()
        r = macro.regime(self.conn)
        self.assertEqual(r["regime"], "risk_on")

    def test_measured_but_nothing_fires_is_neutral(self):
        """A real answer, distinct from "not measured".

        Getting here needs the *dead band* on both legs: VIX neither high,
        nor low, nor a p90 spike; a curve between -0.10 and 0, which is
        neither an inversion nor a genuine steepening. That band exists on
        purpose -- most days the curve sits there -- and a regime function
        that cannot return "neutral" is one that is lying about the other
        days.
        """
        self._recent(18.0, base=18.0, spread=4.0)
        _seed(self.conn, "T10Y2Y", [(self._today(), -0.05)])
        self.conn.commit()
        r = macro.regime(self.conn)
        self.assertEqual(r["regime"], "neutral")
        self.assertIn("no threshold", r["reason"])

    def test_inverted_curve_is_risk_off(self):
        self._recent(17.0)
        _seed(self.conn, "T10Y2Y", [(self._today(), -0.50)])
        self.conn.commit()
        r = macro.regime(self.conn)
        self.assertEqual(r["regime"], "risk_off")
        self.assertIn("inverted", r["reason"])

    def test_conflicting_evidence_is_transition(self):
        """Credit widening against a steep curve is exactly what 'transition'
        means -- and the reason must name both sides.

        Two fixture details are load-bearing. The VIX history is given a
        spread so VIX is *not* itself an off-signal (a flat 15.0 history
        puts 17.0 at p100, and the test would then pass for the wrong
        reason). And HY is given 6 points, because a median over fewer than
        that is not a baseline -- with 4 the widening leg silently drops out
        and this degrades into a plain risk_on.
        """
        self._recent(17.0, base=17.0, spread=3.0)
        _seed(self.conn, "T10Y2Y", [(self._today(), 0.60)])
        _seed(self.conn, "BAMLH0A0HYM2",
              [("2026-09-28", 3.0), ("2026-10-01", 3.0), ("2026-10-05", 3.1),
               ("2026-10-08", 3.15), ("2026-10-10", 3.2),
               (self._today(), 9.9)])   # far above its own recent median
        self.conn.commit()
        r = macro.regime(self.conn)
        self.assertIsNotNone(macro.credit_context(self.conn)["hy_widening"],
                            "fixture did not produce a widening signal")
        self.assertEqual(r["regime"], "transition")
        self.assertIn("hy spreads", r["reason"])
        self.assertIn("counter-evidence", r["reason"])

    def test_confidence_reflects_how_many_legs_contributed(self):
        import datetime
        today = datetime.date.today().isoformat()
        _seed(self.conn, "VIXCLS", [(today, 17.0)])
        self.conn.commit()
        self.assertEqual(macro.regime(self.conn)["confidence"], 0.33)
        _seed(self.conn, "T10Y2Y", [(today, 0.20)])
        _seed(self.conn, "BAMLH0A0HYM2", [(today, 3.2)])
        self.conn.commit()
        self.assertEqual(macro.regime(self.conn)["confidence"], 1.0)


class TestVixContext(MacroBase):
    def _history(self, base, spread, current, count=40):
        """`count` readings around `base`, newest being `current`."""
        import datetime
        today = datetime.date.today()
        out = [((today - datetime.timedelta(days=i)).isoformat(),
                base + (spread if i % 2 else -spread))
               for i in range(count)]
        out = [(d, v) for d, v in out if d != today.isoformat()]
        out.append((today.isoformat(), current))
        out.sort()
        return out

    def _reseed(self, pairs):
        self.conn.execute("DELETE FROM macro_observations")
        _seed(self.conn, "VIXCLS", pairs)
        self.conn.commit()

    def test_same_level_different_history_gives_different_percentile(self):
        """The whole reason for a percentile: 30 after a decade of 12 is
        not the same event as 30 after a decade of 30.

        Both halves use the *identical* current level. Only the history
        differs, so any difference in the answer is attributable to the
        history -- which is what is being asserted.
        """
        self._reseed(self._history(base=12.0, spread=1.0, current=30.0))
        calm_history = macro.vix_context(self.conn)

        self._reseed(self._history(base=30.0, spread=1.0, current=30.0))
        stressed_history = macro.vix_context(self.conn)

        self.assertEqual(calm_history["vix"], stressed_history["vix"],
                         "the two cases must present the same current level")
        self.assertGreater(calm_history["percentile"], 0.9,
                           "30 after a quiet history should read as extreme")
        self.assertLess(stressed_history["percentile"], 0.7,
                        "30 after a stressed history is ordinary, not extreme")
        self.assertGreater(calm_history["percentile"],
                           stressed_history["percentile"])

    def test_percentile_is_within_range(self):
        self._reseed(self._history(base=15.0, spread=2.0, current=16.0))
        p = macro.vix_context(self.conn)["percentile"]
        self.assertIsNotNone(p)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)

    def test_context_is_unusable_when_absent(self):
        c = macro.vix_context(self.conn)
        self.assertIsNone(c["vix"])
        self.assertFalse(c["usable"])

    def test_context_is_unusable_when_stale(self):
        self._reseed([("2020-01-02", 45.0)])
        c = macro.vix_context(self.conn, max_age_days=7)
        self.assertFalse(c["usable"], "a 5-year-old VIX was marked usable")
        # ...but the level is still reported, so the staleness is visible.
        self.assertEqual(c["vix"], 45.0)
        self.assertGreater(c["age_days"], 7)


class TestGate(MacroBase):
    def _regime(self, name):
        return {"regime": name, "confidence": 1.0, "reason": "because", "inputs": {}}

    def test_no_policy_means_no_gating(self):
        """The default. A control nobody asked for that silently suppresses
        trades is worse than no control."""
        g = macro.gate({}, self._regime("risk_off"))
        self.assertTrue(g["allowed"])

    def test_block_in_vetoes(self):
        g = macro.gate({}, self._regime("risk_off"), block_in=["risk_off"])
        self.assertFalse(g["allowed"])
        self.assertIn("risk_off", g["reason"])

    def test_allow_in_restricts(self):
        g = macro.gate({}, self._regime("transition"), allow_in=["risk_on"])
        self.assertFalse(g["allowed"])
        g2 = macro.gate({}, self._regime("risk_on"), allow_in=["risk_on"])
        self.assertTrue(g2["allowed"])

    def test_default_allow_false_is_respected(self):
        g = macro.gate({}, self._regime("risk_on"), default_allow=False)
        self.assertFalse(g["allowed"])

    def test_unmeasured_honours_default_allow(self):
        unmeasured = {"regime": None, "confidence": 0.0, "reason": "no data",
                      "inputs": {}}
        self.assertTrue(macro.gate({}, unmeasured,
                                   block_in=["risk_off"])["allowed"])
        self.assertFalse(macro.gate({}, unmeasured,
                                    block_in=["risk_off"],
                                    default_allow=False)["allowed"])

    def test_gate_always_reports_the_regime_it_saw(self):
        g = macro.gate({}, self._regime("transition"))
        self.assertEqual(g["regime"], "transition")

    def test_params_policy_is_honoured_not_ignored(self):
        """Regression: `gate` accepted `params` and never read it.

        A per-strategy policy has to live in the strategy's own config, so
        a strategy that must not trade into a credit event can say so.
        Silently ignoring the argument would be worse than not taking it,
        because the caller would believe the setting took effect.
        """
        params = {"macro_block_in": ["risk_off", "transition"]}
        self.assertFalse(macro.gate(params, self._regime("risk_off"))["allowed"])
        self.assertFalse(macro.gate(params, self._regime("transition"))["allowed"])
        self.assertTrue(macro.gate(params, self._regime("risk_on"))["allowed"])

    def test_params_allow_in_is_honoured(self):
        params = {"macro_allow_in": ["risk_on"]}
        self.assertTrue(macro.gate(params, self._regime("risk_on"))["allowed"])
        self.assertFalse(macro.gate(params, self._regime("neutral"))["allowed"])

    def test_params_accepts_a_bare_string(self):
        self.assertFalse(
            macro.gate({"macro_block_in": "risk_off"},
                       self._regime("risk_off"))["allowed"])

    def test_explicit_argument_beats_params(self):
        """The caller's argument is the more specific instruction."""
        g = macro.gate({"macro_block_in": ["risk_off"]},
                       self._regime("risk_off"), block_in=["neutral"])
        self.assertTrue(g["allowed"])
        self.assertEqual(g["policy"]["block_in"], ["neutral"])

    def test_string_false_is_not_treated_as_true(self):
        """Regression: a non-bool default arriving as text must be ignored,
        not coerced.

        The distinguishing case is `default_allow=False` in the call with
        `"false"` in params. Ignoring the string leaves the veto in place;
        `bool("false")` is True and silently inverts the policy into an
        allow. Asserting the outcome with the default in its permissive
        position cannot tell the two apart -- both give True -- which is why
        this starts from a restrictive default.
        """
        g = macro.gate({"macro_default_allow": "false"},
                       self._regime("risk_on"), default_allow=False)
        self.assertFalse(g["allowed"],
                         'params "false" overrode the caller default')
        self.assertFalse(g["policy"]["default_allow"])

    def test_bool_default_in_params_is_honoured(self):
        """The contrasting case, so the test above is not vacuous: a real
        bool is a deliberate instruction and does apply."""
        g = macro.gate({"macro_default_allow": True},
                       self._regime("risk_on"), default_allow=False)
        self.assertTrue(g["allowed"])
        self.assertTrue(g["policy"]["default_allow"])

    def test_params_default_allow_is_honoured_when_unmeasured(self):
        unmeasured = {"regime": None, "confidence": 0.0, "reason": "no data",
                      "inputs": {}}
        g = macro.gate({"macro_block_in": ["risk_off"], "macro_default_allow": False},
                       unmeasured)
        self.assertFalse(g["allowed"])

    def test_gate_reports_the_policy_it_used(self):
        g = macro.gate({"macro_block_in": ["risk_off"]},
                       self._regime("risk_on"))
        self.assertEqual(g["policy"]["block_in"], ["risk_off"])

    def test_no_policy_says_the_gate_did_not_apply(self):
        """Measured and permitted is not the same as checked and permitted."""
        g = macro.gate({}, self._regime("risk_off"))
        self.assertTrue(g["allowed"])
        self.assertIn("no policy", g["reason"])

    def test_junk_params_do_not_raise(self):
        for junk in (None, [], "risk_off", 7, {"macro_block_in": 5}):
            self.assertIn("allowed", macro.gate(junk, self._regime("risk_on")))


class TestNoDefaultRegime(MacroBase):
    """The regression test for the whole design."""

    def test_there_is_no_default_value_anywhere(self):
        import inspect
        src = inspect.getsource(macro)
        # A literal default for the headline field is the bug. The 17.0/0.5
        # style placeholders in the docstring are the anti-pattern named.
        body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
        self.assertNotIn('regime = "neutral"', body)
        self.assertNotIn('regime = "risk_on"', body)

    def test_regime_returns_none_rather_than_a_string_when_unmeasured(self):
        self.assertIsNone(macro.regime(self.conn)["regime"])


class TestStdlibOnly(unittest.TestCase):
    def test_macro_module_imports_nothing_third_party(self):
        import ast
        path = os.path.join(ROOT, "engines", "macro.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    found.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and n.module:
                found.add(n.module.split(".")[0])
        # Only mafutani's own modules; everything else must be stdlib.
        internal = {"data", "engines", "fred", "macro"}
        self.assertEqual(found - internal, set(),
                         "non-stdlib, non-internal import: %s"
                         % sorted(found - internal))


class TestCurveIsClassifiedExactlyOnce(MacroBase):
    """Regression: one reading must not be evidence for and against itself.

    Inversion was tested as `value < 0` and steepness as
    `value >= -0.10`. Those predicates overlap on [-0.10, 0), so a curve of
    -0.05 was reported as "inverted at -0.05; counter-evidence: steep at
    -0.05" -- a transition manufactured out of a single number, with a flat
    curve labelled inverted on the way past.

    The curve now has exactly one classification, so this cannot recur.
    """

    def _regime_at(self, v):
        """Regime from a quiet VIX and a 2s10s of exactly `v`."""
        import datetime
        self.conn.execute("DELETE FROM macro_observations")
        t = datetime.date.today()
        hist = [((t - datetime.timedelta(days=i)).isoformat(),
                 17.0 + (3.0 if i % 2 else -3.0)) for i in range(40)]
        hist = [(d, val) for d, val in hist if d != t.isoformat()]
        hist.append((t.isoformat(), 17.0))
        hist.sort()
        _seed(self.conn, "VIXCLS", hist)
        _seed(self.conn, "T10Y2Y", [(t.isoformat(), v)])
        self.conn.commit()
        return macro.regime(self.conn)

    def test_no_reading_is_both_inverted_and_steep(self):
        for v in (-1.0, -0.5, -0.3, -0.25, -0.24, -0.1, -0.05, 0.0, 0.1,
                  0.24, 0.25, 0.6, 1.5):
            r = self._regime_at(v)
            self.assertFalse(
                "inverted" in r["reason"] and "steep" in r["reason"],
                "2s10s=%+.2f was reported as both inverted and steep (%s)"
                % (v, r["reason"]))

    def test_classification_is_a_single_label(self):
        self.assertEqual(macro.classify_curve(-0.5), "inverted")
        self.assertEqual(macro.classify_curve(-0.05), "flat")
        self.assertEqual(macro.classify_curve(0.0), "flat")
        self.assertEqual(macro.classify_curve(0.6), "steep")
        self.assertIsNone(macro.classify_curve(None))

    def test_the_dead_band_is_flat_not_steep(self):
        """The range between the bands is most days. It must read as flat,
        not be silently rounded up to 'steep' by a >= comparison."""
        self.assertEqual(macro.classify_curve(0.24), "flat")
        self.assertEqual(macro.classify_curve(0.25), "steep")

    def test_a_flat_curve_is_not_called_inverted(self):
        r = self._regime_at(-0.05)
        self.assertNotIn("inverted", r["reason"])

    def test_bands_are_reported_with_the_regime(self):
        self._regime_at(0.6)
        bands = macro.curve_context(self.conn)["bands"]
        self.assertIn("inverted_below", bands)
        self.assertIn("steep_at_or_above", bands)


class TestThinCreditHistoryIsNotCalmCredit(MacroBase):
    """Regression: an under-fed leg used to look measured but contribute
    nothing.

    `credit_context` required >= 5 stored points before computing a median,
    correctly. But it returned `usable: True` and left `hy_widening: None`,
    so `regime()` counted the leg toward confidence while it silently
    produced no evidence at all -- and "credit is fine" is what a leg that
    measured nothing looks like from the outside.
    """

    def _hy(self, n, current=9.9):
        import datetime
        today = datetime.date.today()
        pts = [((today - datetime.timedelta(days=i)).isoformat(), 3.0 + i * 0.01)
               for i in range(n - 1, 0, -1)]
        pts.append((today.isoformat(), current))
        _seed(self.conn, "BAMLH0A0HYM2", pts)
        self.conn.commit()

    def test_thin_history_reports_why_it_cannot_answer(self):
        self._hy(4)
        c = macro.credit_context(self.conn)
        self.assertIsNone(c["hy_widening"])
        self.assertIn("median", c["hy_basis"],
                      "a thin history did not explain itself")

    def test_enough_history_does_answer(self):
        self._hy(6)
        c = macro.credit_context(self.conn)
        self.assertIs(c["hy_widening"], True, c["hy_basis"])
        self.assertIn("median", c["hy_basis"])

    def test_thin_history_is_not_reported_as_credit_is_fine(self):
        """The specific lie: widening is unknown, so it must not be False.

        The current reading is deliberately the *lowest* of the three. A
        naive median over so few points then returns `False` -- "spreads at
        or below their median" -- which is precisely the fabricated
        reassurance this test exists to catch. Picking a current reading of
        9.9 instead would have hidden it, because that path returns True and
        happens to look correct.
        """
        import datetime
        today = datetime.date.today()
        _seed(self.conn, "BAMLH0A0HYM2",
              [((today - datetime.timedelta(days=2)).isoformat(), 9.0),
               ((today - datetime.timedelta(days=1)).isoformat(), 8.5),
               (today.isoformat(), 1.0)])
        self.conn.commit()
        c = macro.credit_context(self.conn)
        self.assertIsNone(c["hy_widening"],
                          "3 points produced a verdict: %r" % c["hy_widening"])
        self.assertIn("median", c["hy_basis"])


class TestRoutesExist(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "api", "routes.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_macro_routes_registered(self):
        for p in ('"/api/macro"', '"/api/macro/series"', '"/api/macro/fetch"'):
            self.assertIn(p, self.src)

    def test_fetch_is_post_only(self):
        self.assertIn('path == "/api/macro/fetch" and method == "POST"', self.src)

    def test_signal_preview_carries_macro_context(self):
        self.assertIn('prev["macro"]', self.src)

    def test_preview_attaches_rather_than_suppresses(self):
        """A preview that returned "flat" because of macro would be
        indistinguishable from a strategy finding no setup."""
        seg = self.src[self.src.index('prev["macro"]'):][:1200]
        self.assertNotIn('prev["side"] = None', seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)

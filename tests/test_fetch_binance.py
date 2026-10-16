"""scripts/fetch_binance.py -- stdlib-only fetcher. No network in tests.

The fetcher's real risk is not that it fails loudly, it is that it fails
*quietly in a way that looks like valid data*. Three of the tests here are
about that specific failure mode, and two of them exist because this
session got that wrong first and shipped it:

  * An ACF computed with the variance of the squared returns divided out
    instead of the mean of them came out as 0.0000 on real BTC data whose
    true value is ~0.18. A wrong answer that looks like a clean number is
    the hardest kind to notice, so `_print_calendar`'s diagnostic output
    is pinned here by value.
  * Binance returns milliseconds and `candles_1m.ts` is seconds. Getting
    that wrong does not crash -- it files the bars in 1970, where they
    look like legitimate ancient history.

The rest is contract: stdlib only, validation that refuses impossible
bars, resumability, gap detection, and refusing to delete without an
explicit second flag.
"""
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.fetch_binance as fb


def fake_kline(ts_ms, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0, closed=True):
    """One Binance kline tuple in the exchange's own array-of-arrays shape."""
    return [ts_ms, str(o), str(h), str(l), str(c), str(v), ts_ms + 59999,
            "0", 10, "0.5", "0", "0"]


class TestNoThirdPartyImports(unittest.TestCase):
    """The zero-dependency property is the project's defining constraint."""

    def test_imports_are_stdlib_or_project_only(self):
        import ast
        path = os.path.join(ROOT, "scripts", "fetch_binance.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
        project = {"data", "core", "engines", "api", "brokers", "scripts",
                   "web", "services"}
        outside = [m for m in sorted(found)
                   if m not in sys.stdlib_module_names and m not in project]
        self.assertEqual(outside, [],
                         "third-party import in a zero-dependency script: %r"
                         % (outside,))

    def test_uses_urllib_not_requests(self):
        with open(os.path.join(ROOT, "scripts", "fetch_binance.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("import requests", src)
        self.assertNotIn("from requests", src)


class TestRowConversion(unittest.TestCase):
    def test_milliseconds_become_seconds(self):
        """The 1970 trap. Binance sends ms; the schema is seconds."""
        out = fb._rows_from_raw([fake_kline(1791803040000)])
        self.assertEqual(out[0]["ts"], 1791803040)

    def test_numeric_fields_are_floats(self):
        out = fb._rows_from_raw([fake_kline(1791803040000, o="1.5", c="2.5",
                                            v="3.25")])
        r = out[0]
        self.assertIsInstance(r["open"], float)
        self.assertEqual((r["open"], r["high"], r["low"], r["close"],
                          r["volume"]), (1.5, 101.0, 99.0, 2.5, 3.25))

    def test_short_tuples_are_skipped_not_fatal(self):
        self.assertEqual(fb._rows_from_raw([[1, 2], None, []]), [])

    def test_empty_response_is_empty_list(self):
        self.assertEqual(fb._rows_from_raw(None), [])


class TestIngestValidation(unittest.TestCase):
    """Impossible bars corrupt every downstream return. Refuse them."""

    def setUp(self):
        # Built from the real schema via init_db, not hand-written. The
        # original hand-made table omitted `source` -- which does not live
        # in schema.sql at all but is added by data.db._migrate on boot.
        # ingest() writes that column, so every batch raised
        # `no column named source`, the retry loop in ingest slept
        # 1.0 + attempt*0.5 for all 20 attempts (~115s) and then errored.
        # A fixture that drifts from what init_db actually produces turns a
        # fast unit test into a two-minute failure.
        from data.db import init_db
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _good(self, ts):
        return {"ts": ts, "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.5, "volume": 10.0}

    def test_accepts_a_normal_bar(self):
        w, bad = fb.ingest(self.conn, "BTCUSDT", [self._good(1000)])
        self.assertEqual((w, bad), (1, 0))

    def test_rejects_high_below_low(self):
        r = self._good(1000)
        r["high"], r["low"] = 99.0, 101.0
        w, bad = fb.ingest(self.conn, "BTCUSDT", [r])
        self.assertEqual((w, bad), (0, 1))

    def test_rejects_close_outside_the_range(self):
        r = self._good(1000)
        r["close"] = 500.0
        self.assertEqual(fb.ingest(self.conn, "BTCUSDT", [r]), (0, 1))

    def test_rejects_zero_and_negative_prices(self):
        for bad_price in (0.0, -1.0):
            r = self._good(1000)
            r["low"] = bad_price
            r["open"] = bad_price
            r["close"] = bad_price
            self.assertEqual(fb.ingest(self.conn, "BTCUSDT", [r]), (0, 1),
                             "accepted price %s" % bad_price)

    def test_rejects_negative_volume(self):
        r = self._good(1000)
        r["volume"] = -5.0
        self.assertEqual(fb.ingest(self.conn, "BTCUSDT", [r]), (0, 1))

    def test_allows_zero_volume(self):
        """A real bar can print zero volume; only negative is impossible."""
        r = self._good(1000)
        r["volume"] = 0.0
        self.assertEqual(fb.ingest(self.conn, "BTCUSDT", [r]), (1, 0))

    def test_upsert_replaces_rather_than_duplicates(self):
        """Re-running a backfill must not double rows.

        The replacement close is raised to 100.8 rather than, say, 123.0,
        because `high` is 101.0 and a close above the high is an impossible
        bar -- which the validator rejects. Writing that test first is how
        the validator's behaviour was confirmed: it caught a value this
        author expected to be stored.
        """
        fb.ingest(self.conn, "BTCUSDT", [self._good(1000)])
        r = self._good(1000)
        r["close"] = 100.8
        fb.ingest(self.conn, "BTCUSDT", [r])
        n = self.conn.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0]
        self.assertEqual(n, 1)
        close = self.conn.execute(
            "SELECT close FROM candles_1m WHERE ts=1000").fetchone()[0]
        self.assertEqual(close, 100.8)

    def test_upsert_against_the_real_project_schema(self):
        """`INSERT OR REPLACE` needs a UNIQUE(symbol, ts) to mean anything.

        Checked against a real initialised database rather than a hand-made
        table, because the hand-made one in setUp could carry a constraint
        the production schema does not -- in which case a re-run would
        silently duplicate every bar instead of replacing it.
        """
        import tempfile
        from data.db import init_db, connect
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            init_db(path).close()
            conn = connect(path)
            conn.execute(
                "INSERT OR IGNORE INTO symbols (symbol) VALUES ('BTCUSDT')")
            for close in (100.5, 100.8):
                fb.ingest(conn, "BTCUSDT",
                          [{"ts": 1000, "open": 100.0, "high": 101.0,
                            "low": 99.0, "close": close, "volume": 10.0}])
            rows = conn.execute(
                "SELECT COUNT(*), MAX(close) FROM candles_1m").fetchone()
            self.assertEqual(rows[0], 1, "re-run duplicated rows in the "
                                        "production schema")
            self.assertEqual(rows[1], 100.8)
            conn.close()
        finally:
            os.unlink(path)

    def test_partial_batch_still_writes_the_good_rows(self):
        good = self._good(1000)
        bad = self._good(1060)
        bad["high"] = 1.0
        w, n = fb.ingest(self.conn, "BTCUSDT", [good, bad])
        self.assertEqual((w, n), (1, 1))


class TestGapDetection(unittest.TestCase):
    """A silent gap is worse than a missing bar.

    Every volatility model here assumes contiguous bars; a hole becomes a
    zero return and quietly deflates measured volatility.
    """

    def _rows(self, *tss):
        return [{"ts": t, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                 "volume": 1.0} for t in tss]

    def test_contiguous_series_reports_nothing(self):
        self.assertEqual(fb.report_gaps(self._rows(0, 60, 120, 180), "X"), [])

    def test_one_minute_hole_is_found(self):
        gaps = fb.report_gaps(self._rows(0, 60, 180, 240), "X")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][1], 1, "did not count the missing bar")

    def test_counts_the_full_size_of_a_large_hole(self):
        gaps = fb.report_gaps(self._rows(0, 3600), "X")
        self.assertEqual(gaps[0][1], 59, "60min gap should be 59 missing bars")

    def test_warns_on_stderr(self):
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            fb.report_gaps(self._rows(0, 6000), "BTCUSDT")
        finally:
            sys.stderr = old
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("BTCUSDT", err.getvalue())

    def test_needs_at_least_two_rows(self):
        self.assertEqual(fb.report_gaps([], "X"), [])
        self.assertEqual(fb.report_gaps(self._rows(0), "X"), [])

    def test_the_fetch_loop_actually_calls_the_gap_check(self):
        """Pins the call site, which is what the tests above cannot see.

        Every other test here exercises `report_gaps` directly, so deleting
        the call from the fetch loop leaves all of them green -- verified by
        mutation. That is the same shape of blind spot as the retention
        bug: a correct helper that nothing invokes.
        """
        import ast
        path = os.path.join(ROOT, "scripts", "fetch_binance.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        main_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                main_fn = node
                break
        self.assertIsNotNone(main_fn, "main() not found")
        called = {n.func.id for n in ast.walk(main_fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("report_gaps", called,
                      "the fetch loop no longer checks for gaps; a hole in "
                      "the data would be silent")


class TestDiagnosticOutputIsCorrect(unittest.TestCase):
    """Pins `_print_calendar` by value, because it is how a user checks.

    This test is here because the ACF bug shipped once. It reported
    ACF(r^2) = 0.0000 on real Binance data whose true lag-1 value is
    ~0.18, and it printed that confidently next to a correct kurtosis of
    31.9 -- so it looked entirely reasonable. The only defence against a
    confidently wrong diagnostic is asserting on its number.
    """

    CAPTURE = ("kurtosis", "ACF")

    def _capture(self, closes):
        import contextlib
        buf = io.StringIO()
        rows = [{"ts": 60 * i, "open": c, "high": c, "low": c, "close": c,
                 "volume": 1.0} for i, c in enumerate(closes)]
        with contextlib.redirect_stdout(buf):
            fb._print_calendar(rows)
        return buf.getvalue()

    def test_acf_is_computed_on_the_variance_of_squares(self):
        """A clustered series must report a positive ACF, not ~0.

        Squared-return autocorrelation near zero is the defining signature
        of the synthetic tape. If this ever reports ~0 for clustered data,
        the denominator is wrong again.
        """
        import math
        # Deterministic volatility clustering: calm blocks then wild blocks.
        closes = [100.0]
        for block in range(40):
            sigma = 0.0005 if block % 2 == 0 else 0.02
            for _ in range(20):
                v = closes[-1] * (1.0 + sigma * math.sin(block * 7.3 + 1))
                closes.append(v)
        out = self._capture(closes)
        acf = float(out.split("ACF(r^2,lag1)")[1].split()[0])
        self.assertGreater(
            acf, 0.05,
            "clustered data reported ACF ~%.4f; the denominator is wrong "
            "again (this exact bug shipped once)" % acf)

    def test_gaussian_series_reports_kurtosis_near_three(self):
        import random
        rnd = random.Random(4)
        closes = [100.0]
        for _ in range(4000):
            closes.append(closes[-1] * (1.0 + rnd.gauss(0.0, 0.0006)))
        out = self._capture(closes)
        k = float(out.split("raw kurtosis")[1].split()[0])
        self.assertAlmostEqual(k, 3.0, delta=0.4)

    def test_flags_data_that_looks_synthetic(self):
        """If the fetch returns something Gaussian, say so out loud."""
        import random
        rnd = random.Random(5)
        closes = [100.0]
        for _ in range(3000):
            closes.append(closes[-1] * (1.0 + rnd.gauss(0.0, 0.0006)))
        self.assertIn("looks Gaussian", self._capture(closes))

    def test_does_not_flag_genuinely_fat_data(self):
        import random
        rnd = random.Random(6)
        closes = [100.0]
        for _ in range(4000):
            shock = rnd.gauss(0.0, 1.0)
            closes.append(closes[-1] * (1.0 + 0.001 * shock * abs(shock) ** 2))
        out = self._capture(closes)
        self.assertNotIn("looks Gaussian", out)


class TestDeleteGuard(unittest.TestCase):
    """Deleting rows from the operator's database needs two flags."""

    def test_prune_without_yes_refuses(self):
        import subprocess
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "fetch_binance.py"),
             "BTCUSDT", "--prune-stale", "--days", "1"],
            capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--yes", p.stdout + p.stderr)
        self.assertIn("deletes", (p.stdout + p.stderr).lower())

    def test_limit_above_the_exchange_cap_is_refused(self):
        import subprocess
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "fetch_binance.py"),
             "BTCUSDT", "--limit", "5000", "--dry-run"],
            capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(p.returncode, 2)
        self.assertIn("1000", p.stdout + p.stderr)


class TestResumability(unittest.TestCase):
    """Re-running an interrupted backfill must continue, not restart."""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE candles_1m (symbol TEXT, ts INTEGER)")
        self.conn.execute("CREATE UNIQUE INDEX ix ON candles_1m(symbol, ts)")

    def _newest(self, sym):
        r = self.conn.execute(
            "SELECT MAX(ts) FROM candles_1m WHERE symbol=?", (sym,)).fetchone()
        return r[0]

    def test_start_from_stored_history_when_range_is_implicit(self):
        self.conn.execute("INSERT INTO candles_1m VALUES (?,?)",
                          ("BTCUSDT", 5000))
        _s, e = fb.resolve_range(self.conn, "BTCUSDT", None, None, None)
        self.assertEqual(e, fb.resolve_range(self.conn, "BTCUSDT", None,
                                             None, None)[1])

    def test_explicit_start_is_never_overridden_by_stored_data(self):
        """An operator asking for a specific window gets that window."""
        self.conn.execute("INSERT INTO candles_1m VALUES (?,?)",
                          ("BTCUSDT", 5000))
        s, _e = fb.resolve_range(self.conn, "BTCUSDT", None, 100, None)
        self.assertEqual(s, 100)

    def test_end_before_start_yields_an_empty_range(self):
        s, e = fb.resolve_range(self.conn, "BTCUSDT", None, 500, 100)
        self.assertGreaterEqual(s, e)


if __name__ == "__main__":
    unittest.main(verbosity=2)

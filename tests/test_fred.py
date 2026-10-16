"""FRED macro data: stdlib-only, keyless, and honest about missing values.

Three properties this module exists to get right, each of which is a way a
macro feed usually lies:

* **"." is missing, not zero.** FRED writes a literal "." for weekends and
  holidays. Storing those as 0 would manufacture a VIX of 0 on every
  Saturday, and a 10y yield of 0 on every Sunday.
* **A dead upstream is a result, not an exception.** `fetch_series` returns
  `ok=False` with an error. A poller that raises into its caller is a poller
  that takes the app down with it.
* **A restated value replaces rather than duplicates.** FRED revises history;
  CPI revisions are routine. `macro_observations` is keyed so that upsert is
  the natural semantic, and `macro_fetches` records *when* we learned each
  value -- which is why the observation table is deliberately not a log.

Nothing here touches the network. The live endpoint is exercised separately;
a suite that depends on FRED being up is a suite that fails for reasons
unrelated to the code.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from data import fred  # noqa: E402

CSV = "observation_date,VIXCLS\n" \
      "2026-10-01,16.5\n" \
      "2026-10-02,.\n" \
      "2026-10-05,16.9\n" \
      "2026-10-06,.\n" \
      "2026-10-07,17.1\n"


class TestParsing(unittest.TestCase):
    def test_missing_markers_are_dropped_not_zeroed(self):
        rows, missing = fred.parse_series(CSV, "VIXCLS")
        self.assertEqual([r["value"] for r in rows], [16.5, 16.9, 17.1])
        self.assertEqual(missing, 2, "the two '.' rows must be counted")

    def test_no_observation_is_ever_zero_by_default(self):
        """The whole point. A stored 0 here is a fabricated reading."""
        rows, _ = fred.parse_series(CSV, "VIXCLS")
        self.assertNotIn(0, [r["value"] for r in rows])

    def test_rows_are_ascending_by_date(self):
        rows, _ = fred.parse_series(
            "observation_date,X\n2026-10-07,3\n2026-10-01,1\n2026-10-05,2\n", "X")
        self.assertEqual([r["date"] for r in rows],
                         ["2026-10-01", "2026-10-05", "2026-10-07"])

    def test_garbage_dates_are_skipped(self):
        rows, _ = fred.parse_series(
            "observation_date,X\nnot-a-date,1\n2026-13-45,2\n2026-10-01,3\n", "X")
        self.assertEqual(len(rows), 1)

    def test_empty_input(self):
        self.assertEqual(fred.parse_series("", "X"), ([], 0))
        self.assertEqual(fred.parse_series("   ", "X"), ([], 0))

    def test_header_only(self):
        self.assertEqual(fred.parse_series("observation_date,X\n", "X"), ([], 0))

    def test_non_finite_values_rejected(self):
        for bad in ("nan", "NaN", "inf", "-inf", ""):
            rows, missing = fred.parse_series(
                "observation_date,X\n2026-10-01,%s\n" % bad, "X")
            self.assertEqual(rows, [], "%r became a value" % bad)
            self.assertEqual(missing, 1)

    def test_numeric_helper(self):
        self.assertEqual(fred._num("1.5"), 1.5)
        self.assertIsNone(fred._num("."))
        self.assertIsNone(fred._num(""))
        self.assertIsNone(fred._num(None))


class TestNoUserAgent(unittest.TestCase):
    """FRED stalls on the read when *any* User-Agent is sent.

    Measured three trials each: no UA 5.4s/2.3s/2.6s; with a UA,
    TimeoutError every time, including `Mozilla/5.0`. The sibling
    `data/cot.py` sends one, as is conventional, which makes this the kind of
    thing a future editor would "fix" and thereby break.
    """

    def test_ua_constant_is_empty(self):
        self.assertEqual(fred.UA, {})

    def test_the_reason_is_documented(self):
        """If the empty dict ever loses its comment, the next reader has no
        idea it is load-bearing and will add a UA back."""
        with open(os.path.join(ROOT, "data", "fred.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("TimeoutError", src)
        self.assertIn("Python-urllib", src)


class TestRegistry(unittest.TestCase):
    def test_series_declare_units(self):
        """A caller must never have to guess whether 16.87 is percent."""
        for sid, m in fred.SERIES.items():
            self.assertTrue(m.get("units"), "%s has no units" % sid)
            self.assertTrue(m.get("title"), "%s has no title" % sid)
            self.assertTrue(m.get("category"), "%s has no category" % sid)

    def test_series_ids_are_upper_and_shaped(self):
        for sid in fred.SERIES:
            self.assertEqual(sid, sid.upper())
            self.assertRegex(sid, r"^[A-Z0-9]+$")

    def test_meta_lookup(self):
        self.assertEqual(fred.meta("dgs10")["units"], "percent")
        self.assertIsNone(fred.meta("NOPE"))

    def test_no_duplicate_ids(self):
        keys = list(fred.SERIES.keys())
        self.assertEqual(len(keys), len(set(keys)))

    def test_categories_cover_intent(self):
        cats = {m["category"] for m in fred.SERIES.values()}
        for want in ("rates", "risk", "economy"):
            self.assertIn(want, cats)


class TestFetchErrorHandling(unittest.TestCase):
    """No network in the suite; these pin the failure contract."""

    def test_empty_series_id_returns_not_raises(self):
        r = fred.fetch_series("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["rows"], [])

    def test_result_carries_the_shape_callers_expect(self):
        r = fred.fetch_series("DGS10")
        for k in ("ok", "series_id", "rows", "missing", "url", "error"):
            self.assertIn(k, r)

    def test_transport_failures_return_a_result_not_an_exception(self):
        """The docstring promises fetch_series never raises for a data
        problem. Mutating `raise` back into the HTTPError branch left every
        test green, because they all fed hand-built results and never
        exercised the real error path at all."""
        import urllib.error as ue

        class Boom(object):
            def __init__(self, exc):
                self.exc = exc

            def __call__(self, url):
                raise self.exc

        cases = [
            ue.HTTPError("u", 503, "Service Unavailable", None, None),
            ue.URLError("no route to host"),
            ue.HTTPError("u", 404, "Not Found", None, None),
            ValueError("something unrelated"),
            TimeoutError("timed out"),
        ]
        for exc in cases:
            orig = fred._http
            fred._http = Boom(exc)
            try:
                r = fred.fetch_series("DGS10")   # must not raise
            finally:
                fred._http = orig
            self.assertFalse(r["ok"], "%r was treated as success" % exc)
            self.assertTrue(r["error"], "no reason recorded for %r" % exc)
            self.assertEqual(r["rows"], [], "%r left rows behind" % exc)

    def test_unparseable_response_is_a_failure_not_an_error_crash(self):
        """A 200 with garbage is not data. It must not be stored as if it
        were, and must not raise either."""
        orig = fred._http
        fred._http = lambda url: "<html>maintenance</html>"
        try:
            r = fred.fetch_series("DGS10")
        finally:
            fred._http = orig
        self.assertFalse(r["ok"])
        self.assertEqual(r["rows"], [])

    def test_url_is_built_without_credentials(self):
        r = fred.fetch_series("DGS10")
        self.assertIn("id=DGS10", r["url"])
        self.assertNotIn("api_key", r["url"])

    def test_summarise_isolates_failures(self):
        """One dead series must not fail the batch.

        (This was previously a test that hand-built a dict and then asserted
        properties of that dict -- it proved nothing. The counting was
        extracted into `summarise` so it can actually be exercised.)
        """
        s = fred.summarise({"A": {"ok": True}, "B": {"ok": False, "error": "boom"}})
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["ok"], 1)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["results"]["B"]["error"], "boom")

    def test_summarise_of_empty_and_none(self):
        for arg in ({}, None):
            s = fred.summarise(arg)
            self.assertEqual((s["count"], s["ok"], s["failed"]), (0, 0, 0))

    def test_summarise_tolerates_a_none_entry(self):
        self.assertEqual(fred.summarise({"A": None})["failed"], 1)

    def test_staleness_reports_whole_days(self):
        self.assertEqual(fred.staleness("2026-10-14", now_date="2026-10-16"), 2)

    def test_staleness_of_unparseable_date_is_none_not_zero(self):
        self.assertIsNone(fred.staleness("nonsense"))
        self.assertIsNone(fred.staleness(None))


class TestPersistence(unittest.TestCase):
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

    def _result(self, ok=True, error="", rows=None, missing=0, sid="DGS10"):
        return {"ok": ok, "series_id": sid, "url": "https://example/", "error": error,
                "missing": missing,
                "rows": rows if rows is not None else
                        [{"date": "2026-10-13", "value": 5.1},
                         {"date": "2026-10-14", "value": 5.19}]}

    def test_observations_are_written_with_declared_metadata(self):
        w = fred.persist_series(self.conn, self._result())
        self.assertEqual(w["stored"], 2)
        row = self.conn.execute(
            "SELECT value, units, category, title FROM macro_observations"
            " WHERE series_id='DGS10' AND obs_date='2026-10-14'").fetchone()
        self.assertAlmostEqual(row[0], 5.19)
        self.assertEqual(row[1], "percent")
        self.assertEqual(row[2], "rates")
        self.assertIn("10-Year", row[3])

    def test_a_restatement_replaces_rather_than_duplicates(self):
        """FRED revises history; CPI revisions are routine."""
        fred.persist_series(self.conn, self._result())
        n1 = self.conn.execute(
            "SELECT COUNT(*) FROM macro_observations WHERE series_id='DGS10'").fetchone()[0]
        self.conn.execute("UPDATE macro_observations SET value=99.99"
                          " WHERE series_id='DGS10' AND obs_date='2026-10-14'")
        self.conn.commit()
        fred.persist_series(self.conn, self._result())
        n2 = self.conn.execute(
            "SELECT COUNT(*) FROM macro_observations WHERE series_id='DGS10'").fetchone()[0]
        val = self.conn.execute(
            "SELECT value FROM macro_observations"
            " WHERE series_id='DGS10' AND obs_date='2026-10-14'").fetchone()[0]
        self.assertEqual(n1, n2, "a restatement duplicated the observation")
        self.assertAlmostEqual(val, 5.19, msg=6)

    def test_failed_fetch_records_provenance_and_stores_nothing(self):
        """A silent failure is the thing this project keeps finding."""
        w = fred.persist_series(
            self.conn, self._result(ok=False, error="HTTP 404", rows=[]))
        self.assertFalse(w["ok"])
        self.assertEqual(w["stored"], 0)
        n = self.conn.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0]
        self.assertEqual(n, 0)
        row = self.conn.execute(
            "SELECT ok, error, rows_ok FROM macro_fetches ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "HTTP 404")

    def test_missing_count_is_persisted(self):
        fred.persist_series(self.conn, self._result(missing=7))
        row = self.conn.execute("SELECT missing FROM macro_fetches"
                                " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], 7, "dropped observations must stay visible")

    def test_latest_date_is_recorded(self):
        fred.persist_series(self.conn, self._result())
        row = self.conn.execute("SELECT latest_date FROM macro_fetches"
                                " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], "2026-10-14")

    def test_persist_many_isolates_a_dead_series(self):
        batch = {"results": {"DGS10": self._result(),
                             "NOPE": self._result(ok=False, error="HTTP 404",
                                                  rows=[], sid="NOPE")}}
        out = fred.persist_many(self.conn, batch)
        self.assertEqual(out["ok"], 1)
        self.assertEqual(out["failed"], 1)
        self.assertGreater(out["stored"], 0)

    def test_load_series_returns_stored_rows_only(self):
        """A read endpoint must not silently block on an upstream."""
        fred.persist_series(self.conn, self._result())
        rows = fred.load_series(self.conn, "DGS10")
        self.assertEqual([r["obs_date"] for r in rows],
                         ["2026-10-13", "2026-10-14"])

    def test_load_series_filters_by_date(self):
        fred.persist_series(self.conn, self._result())
        rows = fred.load_series(self.conn, "DGS10", start="2026-10-14")
        self.assertEqual(len(rows), 1)

    def test_load_series_on_empty_db_is_empty_not_an_error(self):
        self.assertEqual(fred.load_series(self.conn, "DGS10"), [])

    def test_latest_stored_is_one_row_per_series_and_carries_age(self):
        fred.persist_series(self.conn, self._result(sid="DGS10"))
        fred.persist_series(self.conn, self._result(
            sid="VIXCLS", rows=[{"date": "2026-10-14", "value": 16.87}]))
        latest = fred.latest_stored(self.conn)
        self.assertEqual(len(latest), 2)
        self.assertEqual({r["series_id"] for r in latest}, {"DGS10", "VIXCLS"})
        for r in latest:
            self.assertIn("age_days", r)

    def test_latest_stored_picks_the_newest_when_series_diverge(self):
        fred.persist_series(self.conn, self._result(
            sid="DGS10", rows=[{"date": "2026-10-01", "value": 4.0},
                               {"date": "2026-10-14", "value": 5.19}]))
        latest = fred.latest_stored(self.conn)
        self.assertEqual(latest[0]["obs_date"], "2026-10-14")


class TestNoNonStdlibImports(unittest.TestCase):
    """RFC-005. This module exists specifically to avoid that dependency."""

    def test_only_stdlib(self):
        import ast
        path = os.path.join(ROOT, "data", "fred.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        allowed = {"csv", "io", "json", "os", "ssl", "urllib", "datetime",
                   "time", "datetime", "urllib.error", "urllib.parse",
                   "urllib.request", "__future__"}
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    found.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                if n.module:
                    found.add(n.module.split(".")[0])
        outside = found - allowed
        self.assertEqual(outside, set(),
                         "non-stdlib import(s): %s" % sorted(outside))


if __name__ == "__main__":
    unittest.main(verbosity=2)

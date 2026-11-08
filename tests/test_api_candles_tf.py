"""/api/candles resolution routing. Live HTTP against a real server.

The bug this pins
-----------------
`web/app.js` has always sent `&tf=1m` to `/api/candles`, and the route
never read it. Every chart therefore queried `candles_1m` regardless, and
the daily bars fetched into `candles_1d` were invisible through the API --
50,756 rows of real data that no chart could reach. A parameter present on
one side of a boundary and silently ignored on the other, which is why it
went unnoticed.

These tests drive the running server over HTTP rather than calling the
handler, because the bug lived in the *wiring*. A test that constructed the
handler directly would have passed throughout.

They also pin the two things that could make this a liability instead of a
feature: the table name must never come from user input, and the response
must report which resolution it actually used.
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

from tests.live_server import ensure_live_server

BASE = ensure_live_server()


def get(path, **params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read().decode())




class TestResolutionRouting(unittest.TestCase):
    def test_1m_still_works(self):
        """The default path must not regress. 30 symbols depend on it."""
        d = get("/api/candles", symbol="BTCUSD", tf="1m", limit=3)
        self.assertEqual(d["tf"], "1m")
        self.assertEqual(d["table"], "candles_1m")
        self.assertEqual(len(d["candles"]), 3)

    def test_1d_reaches_the_daily_table(self):
        """The actual bug. XAUUSD has no usable 1m real data but 1258
        daily bars, so this is the case that was previously invisible."""
        d = get("/api/candles", symbol="XAUUSD", tf="1d", limit=5)
        self.assertEqual(d["tf"], "1d")
        self.assertEqual(d["table"], "candles_1d")
        self.assertEqual(len(d["candles"]), 5)

    def test_responses_are_ordered_oldest_first(self):
        """Charts index left-to-right; a reversed series draws backwards."""
        d = get("/api/candles", symbol="EURUSD", tf="1d", limit=20)
        ts = [c["ts"] for c in d["candles"]]
        self.assertEqual(ts, sorted(ts), "candles are not ascending by ts")

    def test_each_resolution_returns_a_different_length(self):
        """Proves the parameter actually changes the query rather than
        being echoed back with the same rows."""
        m = get("/api/candles", symbol="XAUUSD", tf="1m", limit=5)
        day = get("/api/candles", symbol="XAUUSD", tf="1d", limit=5)
        self.assertNotEqual([c["ts"] for c in m["candles"]],
                            [c["ts"] for c in day["candles"]])


class TestUnrecognisedResolution(unittest.TestCase):
    def test_unknown_tf_falls_back_to_1m_and_says_so(self):
        """Degrades to a renderable chart rather than an error.

        The response reports the resolution actually used, so a silent
        fallback is visible rather than something the operator has to
        infer from the shape of the bars.
        """
        d = get("/api/candles", symbol="BTCUSD", tf="7z", limit=2)
        self.assertEqual(d["tf"], "1m")
        self.assertEqual(d["table"], "candles_1m")

    def test_missing_tf_defaults_to_1m(self):
        d = get("/api/candles", symbol="BTCUSD", limit=2)
        self.assertEqual(d["tf"], "1m")

    def test_case_and_whitespace_are_tolerated(self):
        for variant in ("1D", " 1d ", "1D ".strip()):
            d = get("/api/candles", symbol="XAUUSD", tf=variant, limit=2)
            self.assertEqual(d["tf"], "1d", "tf=%r not normalised" % variant)


class TestTableNameIsNeverUserControlled(unittest.TestCase):
    """`tf` reaches SQL as a table name, so it must not be interpolated."""

    def _try(self, tf):
        d = get("/api/candles", symbol="XAUUSD", tf=tf, limit=2)
        return d.get("table")

    def test_sql_fragment_is_refused(self):
        self.assertEqual(self._try("1m; DROP TABLE candles_1d"), "candles_1m")

    def test_union_select_is_refused(self):
        self.assertEqual(
            self._try("1m UNION SELECT 1,2,3,4,5,6,7 FROM candles_1d"),
            "candles_1m")

    def test_quoted_identifier_is_refused(self):
        self.assertEqual(self._try("candles_1d"), "candles_1m")

    def test_both_tables_still_exist_afterwards(self):
        """Existence, not row count.

        An earlier version asserted >0 rows and failed: `tests/__init__.py`
        redirects DB_PATH to a sandbox, which is empty, so the assertion
        was testing the test harness rather than the injection defence.
        What matters is that the tables were not dropped.
        """
        from data.db import connect
        conn = connect()
        for t in ("candles_1m", "candles_1d"):
            found = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            self.assertIsNotNone(found, "%s was dropped" % t)
        conn.close()

    def test_live_data_survives_the_injection_attempts(self):
        """And on the database the server actually reads, nothing was lost.

        Asserts the tables still exist and that candles_1m -- the table the
        seeder and every feed writes -- is populated. candles_1d is checked
        for existence but not row count: a deployment can legitimately carry
        no daily bars yet (it is populated by scripts/fetch_yahoo.py, which
        is rate-limited and may never have run), and requiring rows there
        tests the deployment's fetch history rather than the injection
        defence this test exists to pin.
        """
        from data.db import connect as live_connect
        import os
        live = os.path.join(ROOT, "data", "trading.db")
        if not os.path.isfile(live):
            self.skipTest("no live database present")
        conn = live_connect(live)
        for t in ("candles_1m", "candles_1d"):
            found = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            self.assertIsNotNone(found, "%s was dropped" % t)
        n = conn.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0]
        self.assertGreater(n, 0, "candles_1m is empty after the injection tests")
        conn.close()


class TestSourceContract(unittest.TestCase):
    """The frontend and the API must agree, since the bug was a mismatch."""

    def _src(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_frontend_offers_exactly_the_mapped_resolutions(self):
        import re
        routes = self._src("api/routes.py")
        mapped = set(re.findall(r'"(1[md])":\s*"candles_1', routes))
        app = self._src("web/app.js")
        offered = set(re.findall(r"<option value='(1[md])'", app))
        self.assertTrue(mapped, "no _CANDLE_TABLES entries found")
        self.assertEqual(mapped, offered,
                         "frontend offers %s, API maps %s"
                         % (sorted(offered), sorted(mapped)))

    def test_frontend_reads_the_resolution_element(self):
        app = self._src("web/app.js")
        self.assertIn("id='c-tf'", app, "no resolution selector in the UI")
        self.assertIn('"c-tf"', app, "selector is never read")

    def test_frontend_passes_tf_to_the_api(self):
        """The original defect: tf was sent but the route ignored it."""
        app = self._src("web/app.js")
        self.assertIn('"&tf=" + encodeURIComponent(tf)', app,
                      "tf is not forwarded to /api/candles")


if __name__ == "__main__":
    unittest.main(verbosity=2)

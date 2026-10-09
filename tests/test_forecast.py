"""Forecast contract, service and scoring tests. Stdlib only.

Organised around the guarantee the whole feature rests on: a number reaches
the UI only if a declared model produced it. Most of this file is therefore
adversarial -- malformed services, lying services, and service-shaped holes
that would otherwise be drawn on a chart indistinguishable from a forecast.

The reference service is exercised over real HTTP rather than by calling
rw_forecast() directly, because the parts most likely to break are the wire
framing and the client's response validation, and neither is reached by a
direct call.
"""
import json
import math
import os
import random
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.tiers as tiers
import data.forecast as dfc
import engines.forecast as ef
from services.forecast.reference_server import rw_forecast


def _bars(n=400, seed=11, price=100.0, sigma=0.002):
    rnd = random.Random(seed)
    out = []
    p = price
    t = 1791544075 - n * 60
    for i in range(n):
        p *= math.exp(rnd.gauss(0.0, sigma))
        out.append([t + i * 60, round(p, 4)])
    return out


# --------------------------------------------------------------- the model

class TestReferenceModel(unittest.TestCase):
    """The random walk itself. These assert the statistics behave, not that
    specific numbers come out -- a fixed seed on a drifting series is a
    test that breaks for no reason."""

    def test_needs_two_bars(self):
        self.assertRaises(ValueError, rw_forecast, [[1, 100.0]])
        self.assertTrue(rw_forecast([[1, 100.0], [2, 101.0]])["predicted"])

    def test_rejects_nonpositive_close(self):
        self.assertRaises(ValueError, rw_forecast, [[1, 100.0], [2, 0.0]])
        self.assertRaises(ValueError, rw_forecast, [[1, -1.0], [2, 2.0]])

    def test_declares_it_is_not_a_trained_model(self):
        """The single most important assertion in this file. If the
        reference service ever stopped saying so, its output would be
        displayed as a forecast."""
        f = rw_forecast(_bars())
        self.assertIs(f["is_real_model"], False)
        self.assertEqual(f["family"], "baseline")
        self.assertIn("not a trained model", f["diagnostics"]["note"])

    def test_flat_series_gives_exactly_flat_forecast(self):
        """Zero volatility must produce zero bands, not a small non-zero
        one from floating point."""
        f = rw_forecast([[1 + i, 100.0] for i in range(50)])
        for p in f["predicted"]:
            self.assertAlmostEqual(p["sigma"], 0.0, places=9)
            self.assertAlmostEqual(p["p50"], 100.0, places=9)

    def test_sigma_grows_as_sqrt_horizon(self):
        """The defining property of a random walk: uncertainty is not linear
        in time. A model whose bands grew linearly would be claiming to know
        more about next week than about tomorrow."""
        f = rw_forecast(_bars(sigma=0.01))
        rows = {p["horizon"]: p["sigma"] for p in f["predicted"]}
        self.assertAlmostEqual(rows[4] / rows[1], 2.0, places=4)
        self.assertAlmostEqual(rows[8] / rows[1], math.sqrt(8), places=4)

    def test_bands_are_ordered(self):
        f = rw_forecast(_bars())
        for p in f["predicted"]:
            self.assertLess(p["p10"], p["p50"])
            self.assertLess(p["p50"], p["p90"])

    def test_drift_shrinks_toward_zero(self):
        """Shrinkage must always move mu toward zero, for any input.

        A deterministic uptrend with *no* variance makes sigma zero, so the
        t-statistic is undefined and the code falls back to mu=0. That is
        the correct answer -- with no dispersion there is no evidence at
        all -- so this asserts the fallback rather than pretending a
        perfect trend should be extrapolated.
        """
        noisy_up = [[1 + i, 100.0 * math.exp(0.0004 * i + 0.01 * math.sin(i))]
                    for i in range(300)]
        d = rw_forecast(noisy_up)["diagnostics"]
        self.assertGreater(d["drift_raw_per_bar"], 0.0)
        self.assertLess(d["drift_used_per_bar"], d["drift_raw_per_bar"])
        self.assertLess(d["shrinkage"], 1.0)
        self.assertGreaterEqual(d["shrinkage"], 0.0)

        # sigma comes out at ~1e-16 rather than exactly 0 because log(exp(x))
        # is not x in floating point. The *intent* is "zero volatility
        # yields no evidence of drift", so assert the magnitude.
        perfect = rw_forecast([[1 + i, 100.0 * math.exp(0.004 * i)]
                               for i in range(300)])["diagnostics"]
        self.assertLess(perfect["sigma_per_bar"], 1e-12)
        self.assertEqual(perfect["drift_used_per_bar"], 0.0)

    def test_shrinkage_never_amplifies(self):
        """mu_used must lie between 0 and mu_raw for every horizon of a
        sweep of inputs -- the one property that must not break."""
        for seed in range(12):
            b = _bars(n=250, seed=seed, sigma=0.003)
            d = rw_forecast(b)["diagnostics"]
            raw, used = d["drift_raw_per_bar"], d["drift_used_per_bar"]
            self.assertLessEqual(abs(used), abs(raw),
                                 "shrinkage amplified drift at seed {}".format(seed))

    def test_confidence_stays_in_unit_interval(self):
        f = rw_forecast(_bars(sigma=0.05))
        for p in f["predicted"]:
            self.assertGreaterEqual(p["confidence"], 0.0)
            self.assertLessEqual(p["confidence"], 1.0)


# --------------------------------------------------------------- validation

class TestValidate(unittest.TestCase):
    """Every path that must NOT produce a number."""

    def good(self, **over):
        p = {"contract": dfc.CONTRACT, "model": "kronos", "family": "foundation",
             "is_real_model": True, "last_close": 100.0,
             "predicted": [{"horizon": 1, "mean": 101.0, "sigma": 1.0,
                            "p10": 99.0, "p50": 101.0, "p90": 103.0}]}
        p.update(over)
        return p

    def test_accepts_a_wellformed_forecast(self):
        out = dfc.validate(self.good())
        self.assertEqual(out["model"], "kronos")
        self.assertIs(out["is_real_model"], True)
        self.assertNotIn("warning", out)

    def test_warns_when_not_a_real_model(self):
        out = dfc.validate(self.good(model="rw-null", is_real_model=False,
                                    family="baseline"))
        self.assertIn("warning", out)
        self.assertIn("rw-null", out["warning"])

    def test_rejects_contract_mismatch(self):
        with self.assertRaises(dfc.ForecastUnavailable) as cm:
            dfc.validate(self.good(contract="other/1"))
        self.assertEqual(cm.exception.reason, "contract_mismatch")

    def test_rejects_missing_contract(self):
        p = self.good()
        del p["contract"]
        with self.assertRaises(dfc.ForecastUnavailable):
            dfc.validate(p)

    def test_rejects_missing_is_real_model(self):
        """The core safety property: a service that cannot say what it is
        gets no number, however well-formed the rest is."""
        p = self.good()
        del p["is_real_model"]
        with self.assertRaises(dfc.ForecastUnavailable) as cm:
            dfc.validate(p)
        self.assertEqual(cm.exception.reason, "unidentified_model")

    def test_rejects_nonbool_is_real_model(self):
        for v in ("yes", 1, None, []):
            with self.assertRaises(dfc.ForecastUnavailable):
                dfc.validate(self.good(is_real_model=v))

    def test_rejects_quantiles_without_sigma(self):
        """Quantiles with no dispersion to check them against are
        decoration -- an 80% band is a calibration claim."""
        with self.assertRaises(dfc.ForecastUnavailable) as cm:
            dfc.validate(self.good(predicted=[{"horizon": 1, "mean": 1.0,
                                               "p10": 0.9, "p90": 1.1}]))
        self.assertEqual(cm.exception.reason, "uncalibrated")

    def test_rejects_nonfinite(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(dfc.ForecastUnavailable):
                dfc.validate(self.good(predicted=[{"horizon": 1, "mean": bad,
                                                   "sigma": 1.0}]))

    def test_rejects_bool_where_number_expected(self):
        """bool is an int subclass in Python, so True would pass a naive
        numeric check and arrive as a forecast of 1."""
        p = self.good()
        p["last_close"] = True
        with self.assertRaises(dfc.ForecastUnavailable):
            dfc.validate(p)

    def test_rejects_inverted_quantiles(self):
        with self.assertRaises(dfc.ForecastUnavailable):
            dfc.validate(self.good(predicted=[{"horizon": 1, "mean": 1.0,
                                               "sigma": 1.0, "p10": 105.0,
                                               "p90": 95.0}]))

    def test_rejects_bad_horizons(self):
        for h in (0, -1, 1.0, "2", True, None):
            with self.assertRaises(dfc.ForecastUnavailable):
                dfc.validate(self.good(predicted=[{"horizon": h, "mean": 1.0,
                                                   "sigma": 1.0}]))

    def test_rejects_negative_sigma(self):
        with self.assertRaises(dfc.ForecastUnavailable):
            dfc.validate(self.good(predicted=[{"horizon": 1, "mean": 1.0,
                                               "sigma": -1.0}]))

    def test_rejects_missing_predictions(self):
        for v in ([], None, "x", {}):
            with self.assertRaises(dfc.ForecastUnavailable):
                dfc.validate(self.good(predicted=v))

    def test_rejects_non_dict_response(self):
        for v in ([1, 2], "x", None, 5):
            with self.assertRaises(dfc.ForecastUnavailable):
                dfc.validate(v)

    def test_derives_mean_from_p50(self):
        """A model reporting only quantiles is usable if it names its
        contract; the mean is filled in rather than rejected."""
        out = dfc.validate(self.good(predicted=[{"horizon": 1, "sigma": 1.0,
                                                 "p10": 99.0, "p50": 101.0,
                                                 "p90": 103.0}]))
        self.assertAlmostEqual(out["predicted"][0]["mean"], 101.0)

    def test_sorts_horizons_ascending(self):
        out = dfc.validate(self.good(predicted=[
            {"horizon": 8, "mean": 1.0, "sigma": 1.0},
            {"horizon": 1, "mean": 1.0, "sigma": 1.0}]))
        self.assertEqual([p["horizon"] for p in out["predicted"]], [1, 8])


# --------------------------------------------------------------- input guards

class TestPredictInput(unittest.TestCase):
    def setUp(self):
        dfc.clear_cache()

    def test_rejects_short_history(self):
        with self.assertRaises(dfc.ForecastUnavailable) as cm:
            dfc.predict([[1, 100.0]])
        self.assertEqual(cm.exception.reason, "insufficient_history")

    def test_rejects_oversized_history(self):
        with self.assertRaises(dfc.ForecastUnavailable) as cm:
            dfc.predict([(i, 100.0) for i in range(dfc.MAX_BARS + 1)])
        self.assertEqual(cm.exception.reason, "too_much_history")

    def test_rejects_nonpositive_close(self):
        with self.assertRaises(dfc.ForecastUnavailable) as cm:
            dfc.predict([[1, 100.0], [2, 0.0], [3, -5.0]])
        self.assertEqual(cm.exception.reason, "bad_input")

    def test_rejects_nan_close(self):
        with self.assertRaises(dfc.ForecastUnavailable):
            dfc.predict([[1, 100.0], [2, float("nan")]])

    def test_rejects_malformed_bar(self):
        for b in ([1], "x", None, [1, "abc"]):
            with self.assertRaises(dfc.ForecastUnavailable):
                dfc.predict([[1, 100.0], b])

    def test_unreachable_service_raises_not_returns(self):
        """Service down must never become a number."""
        old = os.environ.get("MAFUTANI_FORECAST_URL")
        os.environ["MAFUTANI_FORECAST_URL"] = "http://127.0.0.1:9"
        dfc.clear_cache()
        try:
            with self.assertRaises(dfc.ForecastUnavailable) as cm:
                dfc.predict(_bars())
            self.assertEqual(cm.exception.reason, "unreachable")
            self.assertFalse(dfc.is_available(timeout=1.0))
        finally:
            if old is None:
                os.environ.pop("MAFUTANI_FORECAST_URL", None)
            else:
                os.environ["MAFUTANI_FORECAST_URL"] = old
            dfc.clear_cache()


# --------------------------------------------------------------- over the wire

class TestOverHttp(unittest.TestCase):
    """Client against the real service, over a real socket.

    A stubbed urlopen would not exercise the JSON round trip, the status
    codes, or the contract field actually surviving serialisation -- which
    is exactly where a wire protocol breaks.
    """

    @classmethod
    def setUpClass(cls):
        from services.forecast.reference_server import Handler
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls._old = os.environ.get("MAFUTANI_FORECAST_URL")
        os.environ["MAFUTANI_FORECAST_URL"] = "http://127.0.0.1:%d" % cls.port
        dfc.clear_cache()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        if cls._old is None:
            os.environ.pop("MAFUTANI_FORECAST_URL", None)
        else:
            os.environ["MAFUTANI_FORECAST_URL"] = cls._old
        dfc.clear_cache()

    def setUp(self):
        dfc.clear_cache()

    def test_health(self):
        h = dfc.health(force=True)
        self.assertTrue(h["ok"])
        self.assertIs(h["is_real_model"], False)
        self.assertIn("NOT KRONOS", h["warning"].upper())

    def test_predict_round_trip(self):
        bars = _bars()
        f = dfc.predict(bars, symbol="BTCUSD", horizons=[1, 4, 8])
        self.assertEqual(f["contract"], dfc.CONTRACT)
        self.assertIs(f["is_real_model"], False)
        self.assertIn("warning", f)
        self.assertEqual([p["horizon"] for p in f["predicted"]], [1, 4, 8])
        for p in f["predicted"]:
            self.assertLess(p["p10"], p["p50"])
            self.assertLess(p["p50"], p["p90"])
        self.assertAlmostEqual(f["last_close"], bars[-1][1], places=3)

    def test_forecast_is_json_safe(self):
        """No NaN or Infinity anywhere in the payload: those serialise to
        bare literals that a strict JSON reader will reject, so a forecast
        containing one is a forecast that breaks its own transport."""
        f = dfc.predict(_bars())
        json.dumps(f, allow_nan=False)

    def test_caches_repeat_calls(self):
        bars = _bars()
        a = dfc.predict(bars, symbol="BTCUSD")
        b = dfc.predict(bars, symbol="BTCUSD")
        self.assertIs(a, b)
        dfc.clear_cache()
        self.assertIsNot(dfc.predict(bars, symbol="BTCUSD"), a)

    def test_service_errors_surface_verbatim(self):
        """A service that explains itself should not be replaced with a
        generic message."""
        import urllib.request
        body = json.dumps({"contract": dfc.CONTRACT, "bars": [[1, 100.0]]}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/predict" % self.port, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 400")
        except Exception as e:
            self.assertEqual(getattr(e, "code", None), 400)

    def test_wrong_contract_is_rejected_by_service(self):
        """Mismatched contract is refused at the boundary, so the client
        never has to interpret a payload it does not understand."""
        import urllib.error
        import urllib.request
        body = json.dumps({"contract": "nope/9", "bars": _bars(10)}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/predict" % self.port, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 409)

    def test_unknown_endpoint_404s(self):
        import urllib.error
        import urllib.request
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen("http://127.0.0.1:%d/nope" % self.port,
                                   timeout=10)
        self.assertEqual(cm.exception.code, 404)


class TestHostileService(unittest.TestCase):
    """A service that returns a plausible number while declaring nothing
    about itself -- the exact shape of a stub or misconfigured endpoint
    that would otherwise be drawn as a forecast."""

    class Handler(BaseHTTPRequestHandler):
        payload = {}

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._json(200, {"contract": dfc.CONTRACT, "ok": True,
                                        "model": "mystery"})
            self._json(404, {"error": "no"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            self._json(200, dict(self.payload))

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    def _serve(self, payload):
        h = type("H", (self.Handler,), {"payload": payload})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _run(self, payload):
        port = self._serve(payload)
        old = os.environ.get("MAFUTANI_FORECAST_URL")
        os.environ["MAFUTANI_FORECAST_URL"] = "http://127.0.0.1:%d" % port
        dfc.clear_cache()
        try:
            return dfc.predict(_bars())
        finally:
            if old is None:
                os.environ.pop("MAFUTANI_FORECAST_URL", None)
            else:
                os.environ["MAFUTANI_FORECAST_URL"] = old
            dfc.clear_cache()

    def test_undeclared_model_is_refused(self):
        """Well-formed, confident, numerically plausible -- and refused,
        because it never said what produced it."""
        with self.assertRaises(dfc.ForecastUnavailable) as cm:
            self._run({"contract": dfc.CONTRACT, "model": "mystery",
                       "last_close": 100.0,
                       "predicted": [{"horizon": 1, "mean": 101.0,
                                      "sigma": 1.0, "p10": 99.0,
                                      "p50": 101.0, "p90": 103.0}]})
        self.assertEqual(cm.exception.reason, "unidentified_model")

    def test_lying_baseline_can_declare_itself(self):
        """Declaring is_real_model=false is *honoured*, not merely tolerated:
        the flag travels to the caller so the UI can banner it."""
        f = self._run({"contract": dfc.CONTRACT, "model": "rw-null",
                       "family": "baseline", "is_real_model": False,
                       "last_close": 100.0,
                       "predicted": [{"horizon": 1, "mean": 100.0,
                                      "sigma": 1.0}]})
        self.assertIs(f["is_real_model"], False)
        self.assertIn("warning", f)

    def test_garbage_body_is_refused(self):
        for payload in ({"contract": dfc.CONTRACT, "model": "m",
                         "is_real_model": True, "last_close": 100.0,
                         "predicted": [{"horizon": 1, "mean": "up a lot"}]},
                        {"contract": dfc.CONTRACT, "model": "m",
                         "is_real_model": True, "last_close": 100.0,
                         "predicted": "tomorrow"}):
            with self.assertRaises(dfc.ForecastUnavailable):
                self._run(payload)


# --------------------------------------------------------------- scoring

class TestScoring(unittest.TestCase):
    def test_point_prefers_median(self):
        self.assertEqual(ef.point({"mean": 1.0, "median": 2.0, "p50": 3.0}), 3.0)
        self.assertEqual(ef.point({"mean": 1.0, "sigma": 1.0}), 1.0)
        self.assertIsNone(ef.point({"sigma": 1.0}))

    def test_band_from_quantiles_or_sigma(self):
        self.assertEqual(ef.band({"p10": 9.0, "p90": 11.0}), (9.0, 11.0))
        self.assertEqual(ef.band({"mean": 10.0, "sigma": 1.0}), (9.0, 11.0))
        self.assertIsNone(ef.band({"mean": 10.0}))

    def test_band_hit(self):
        row = {"p10": 9.0, "p90": 11.0}
        self.assertTrue(ef.band_hit(row, 10.0))
        self.assertTrue(ef.band_hit(row, 9.0))    # boundary is inclusive
        self.assertFalse(ef.band_hit(row, 12.0))
        self.assertFalse(ef.band_hit({"mean": 1.0}, 1.0))  # no band

    def test_direction(self):
        self.assertEqual(ef.direction({"mean": 110.0}, 100.0, 105.0), 1)
        self.assertEqual(ef.direction({"mean": 110.0}, 100.0, 95.0), -1)
        self.assertEqual(ef.direction({"mean": 90.0}, 100.0, 95.0), -1)
        self.assertEqual(ef.direction({"mean": 90.0}, 100.0, 105.0), 1)
        self.assertEqual(ef.direction({"mean": 100.0}, 100.0, 105.0), 0)
        self.assertEqual(ef.direction({}, 100.0, 105.0), 0)

    def test_nominal_band_scales_with_sigma(self):
        narrow = ef.nominal_band({"mean": 100.0, "sigma": 0.1})
        wide = ef.nominal_band({"mean": 100.0, "sigma": 5.0})
        self.assertIsNotNone(narrow)
        self.assertIsNotNone(wide)
        self.assertLess(narrow, wide)
        self.assertLessEqual(wide, 1.0)

    def test_verdict_flags_overconfidence(self):
        v = ef.verdict(0.50, 0.80, 100)
        self.assertFalse(v["usable"])
        self.assertIn("too narrow", v["detail"])

    def test_verdict_flags_underconfidence(self):
        """Bands far wider than claimed are not usable, and the tolerance is
        genuinely tight: 0.95 against a nominal 0.80 is 15 points over,
        which is the boundary case, so push past it."""
        v = ef.verdict(0.98, 0.80, 100)
        self.assertFalse(v["usable"])
        self.assertIn("too wide", v["detail"])

    def test_verdict_calibrated_but_uninformative(self):
        """Good coverage with no directional skill is still no skill, and
        must not be reported as usable."""
        v = ef.verdict(0.80, 0.80, 100, hit_rate=0.40)
        self.assertFalse(v["usable"])
        self.assertIn("uninformative", v["label"])

    def test_verdict_needs_history(self):
        v = ef.verdict(0.0, 0.80, 0)
        self.assertFalse(v["usable"])
        self.assertEqual(v["label"], "no history")

    def test_not_a_trade_is_not_tradeable(self):
        """The display-only invariant, asserted where it is defined."""
        self.assertFalse(ef.NOT_A_TRADE["tradeable"])
        self.assertIn("no forecast path may place", ef.NOT_A_TRADE["reason"])

    def test_verdict_nominal_band_of_a_real_band(self):
        """A p10/p90 band on a random walk should score near 80%, which is
        what it claims. This is the statistic that says whether a model's
        confidence can be believed."""
        rows = rw_forecast(_bars(sigma=0.004))["predicted"]
        for p in rows:
            nb = ef.nominal_band(p)
            self.assertIsNotNone(nb)
            self.assertAlmostEqual(nb, 0.80, delta=0.02)


# --------------------------------------------------------------- persistence

class TestScoringOverTime(unittest.TestCase):
    """Scoring a model requires history, which requires recording it."""

    def setUp(self):
        import tempfile
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # DB_PATH is the variable data.db actually reads. An earlier
        # revision of this file set MAFUTANI_DB, which nothing reads, so the
        # test module silently ran against data/trading.db and wrote four
        # rows into the live database. Asserted below that this took effect,
        # because that failure mode is invisible: the tests still passed.
        self._old_db = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.path
        for m in [m for m in list(sys.modules) if m.startswith("data.")]:
            del sys.modules[m]
        from data.db import init_db
        self.conn = init_db()
        # Guard: prove isolation rather than assuming it.
        self.assertNotEqual(os.path.abspath(self.conn.execute(
            "PRAGMA database_list").fetchone()[2]),
            os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "data", "trading.db")))
        self.conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol) VALUES ('BTCUSD')")

    def tearDown(self):
        self.conn.close()
        if self._old_db is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old_db
        try:
            os.unlink(self.path)
        except OSError:
            pass
        for m in [m for m in list(sys.modules) if m.startswith("data.")]:
            del sys.modules[m]

    def _candle(self, ts, close):
        # symbol is inlined in the SQL, so only ts plus OHLC are bound.
        self.conn.execute(
            "INSERT OR REPLACE INTO candles_1m (symbol, ts, open, high, low,"
            " close, volume) VALUES ('BTCUSD',?,?,?,?,?,0)",
            (ts, close, close, close, close))

    def _record(self, asof, last, horizon=1, sigma=5.0, model="m"):
        fc = {"model": model, "is_real_model": False, "family": "baseline",
              "asof": asof, "last_close": last,
              "predicted": [{"horizon": horizon, "mean": last, "sigma": sigma,
                             "p10": last - sigma, "p50": last,
                             "p90": last + sigma}]}
        return ef.record(self.conn, 1, "BTCUSD", fc)

    def test_record_and_resolve(self):
        """A forecast whose target bar has closed is scored; one whose
        target has not is pending, not dropped."""
        t = 1791544075 - 600
        # range(6), not range(5): the target of a horizon-1 forecast taken at
        # t+4*60 is t+5*60, which has to exist for the row to resolve.
        for i in range(6):
            self._candle(t + i * 60, 100.0 + i)
        asof = t + 4 * 60
        self._record(asof, 100.0, horizon=1)
        self._record(asof, 100.0, horizon=3)   # target t+7*60 not yet stored
        self.conn.commit()
        s = ef.score_model(self.conn, "BTCUSD", model="m")
        self.assertEqual(s["stored"], 2)
        self.assertEqual(s["resolved"], 1)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["horizon_s"], 60)

    def test_hit_and_miss_are_distinguished(self):
        """Two forecasts of the same target from the same bar, one with a
        band that contains the outcome and one that does not -- coverage
        must count exactly one hit. Both share asof/horizon, so this also
        pins that scoring keys on the resolved candle rather than on row
        identity."""
        t = 1791544075 - 600
        for i in range(6):
            self._candle(t + i * 60, 100.0)
        asof = t + 4 * 60
        # The target bar (asof + 60) moves to 102.0, so a tight band around
        # 100 misses it and a wide one contains it. A band that straddled
        # the outcome would hit under both models and prove nothing.
        self._candle(asof + 60, 102.0)

        wide = {"model": "wide", "is_real_model": False, "asof": asof,
                "last_close": 100.0,
                "predicted": [{"horizon": 1, "mean": 100.0, "sigma": 5.0,
                               "p10": 95.0, "p50": 100.0, "p90": 105.0}]}
        narrow = {"model": "narrow", "is_real_model": False, "asof": asof,
                  "last_close": 100.0,
                  "predicted": [{"horizon": 1, "mean": 100.0, "sigma": 0.1,
                                 "p10": 99.9, "p50": 100.0, "p90": 100.1}]}
        ef.record(self.conn, 1, "BTCUSD", wide)
        ef.record(self.conn, 1, "BTCUSD", narrow)
        self.conn.commit()

        self.assertEqual(
            ef.score_model(self.conn, "BTCUSD", model="wide")["band_hits"], 1)
        self.assertEqual(
            ef.score_model(self.conn, "BTCUSD", model="narrow")["band_hits"], 0)
        both = ef.score_model(self.conn, "BTCUSD")
        self.assertEqual(both["resolved"], 2)
        self.assertEqual(both["band_hits"], 1)
        self.assertAlmostEqual(both["coverage"], 0.5)

    def test_scores_per_model_not_pooled(self):
        """A model must only ever be judged on its own history. Pooling a
        good and a bad model into one coverage number would let a weak
        model hide behind a strong one."""
        t = 1791544075 - 600
        for i in range(6):
            self._candle(t + i * 60, 100.0)
        asof = t + 4 * 60
        # Same setup as above: target bar moves to 102.0, so the wide band
        # contains it and the tight one cannot.
        self._candle(asof + 60, 102.0)
        self._record(asof, 100.0, horizon=1, sigma=5.0, model="good")
        self._record(asof, 100.0, horizon=1, sigma=5.0, model="good")
        self._record(asof, 100.0, horizon=1, sigma=0.05, model="bad")
        self.conn.commit()
        self.assertEqual(ef.score_model(self.conn, "BTCUSD", model="good")["band_hits"], 2)
        self.assertEqual(ef.score_model(self.conn, "BTCUSD", model="bad")["band_hits"], 0)

    def test_empty_history_reports_no_history(self):
        s = ef.score_model(self.conn, "BTCUSD")
        self.assertEqual(s["resolved"], 0)
        self.assertFalse(s["verdict"]["usable"])
        self.assertFalse(s["tradeable"])

    def test_is_real_model_is_persisted(self):
        """Provenance has to survive into the database, not just the API
        response -- otherwise old rows cannot be told apart later."""
        fc = {"model": "kronos", "is_real_model": True, "family": "foundation",
              "asof": 1, "last_close": 100.0,
              "predicted": [{"horizon": 1, "mean": 100.0, "sigma": 1.0}]}
        ef.record(self.conn, 1, "BTCUSD", fc)
        self.conn.commit()
        r = self.conn.execute("SELECT is_real_model FROM forecasts").fetchone()
        self.assertEqual(r[0], 1)


class TestCompareToNull(unittest.TestCase):
    def test_flags_a_model_inside_the_null_band(self):
        null = {"last_close": 100.0,
                "predicted": [{"horizon": 1, "p50": 100.0, "sigma": 5.0,
                               "p10": 93.6, "p90": 106.4}]}
        model = {"predicted": [{"horizon": 1, "mean": 101.0}]}
        out = ef.compare_to_null(model, null)
        self.assertTrue(out[0]["inside_null_band"])
        self.assertAlmostEqual(out[0]["delta_pct"], 1.0, places=6)

    def test_flags_a_model_outside_the_null_band(self):
        null = {"last_close": 100.0,
                "predicted": [{"horizon": 1, "p50": 100.0, "sigma": 1.0,
                               "p10": 98.7, "p90": 101.3}]}
        model = {"predicted": [{"horizon": 1, "mean": 110.0}]}
        self.assertFalse(ef.compare_to_null(model, null)[0]["inside_null_band"])

    def test_returns_none_without_both(self):
        self.assertIsNone(ef.compare_to_null(None, {"predicted": []}))
        self.assertIsNone(ef.compare_to_null({"predicted": []}, None))
        self.assertIsNone(ef.compare_to_null({"predicted": [{"horizon": 1,
                                                            "mean": 1.0}]},
                                             {"predicted": []}))


# --------------------------------------------------------------- gating

class TestForecastIsNotAStrategy(unittest.TestCase):
    """Display-only, enforced structurally rather than by convention.

    `forecast` is an entitlement (which pages are reachable) but is absent
    from every tier's `methods_allowed` (which strategies a backtest or
    signal may run). This is the mechanism: if it were ever added to
    methods_allowed, a backtest could be built on model output and the
    guarantee would be gone without any code changing.
    """

    def test_absent_from_every_tier(self):
        for name in tiers.ENTITLEMENTS:
            self.assertNotIn("forecast", tiers.ENTITLEMENTS[name]["methods_allowed"],
                             "'forecast' must never be a runnable method")
            self.assertFalse(
                tiers.can_use_strategy(name, "forecast"),
                "tier {} could run a forecast strategy".format(name))

    def test_entitlement_gates_the_page_only(self):
        self.assertFalse(tiers.get_entitlements("Free")["forecast"])
        for t in ("Pro", "Institutional", "Admin"):
            self.assertTrue(tiers.get_entitlements(t)["forecast"])
            self.assertFalse(tiers.get_entitlements(t)["live_enabled"] is None)

    def test_require_entitlement_raises_for_free(self):
        with self.assertRaises(PermissionError):
            tiers.require_entitlement("Free", "forecast")
        self.assertTrue(tiers.require_entitlement("Pro", "forecast"))

    def test_documented_strategies_still_unchanged(self):
        """Adding the entitlement must not have disturbed what can actually
        be backtested."""
        self.assertEqual(tiers.get_entitlements("Free")["methods_allowed"],
                         ["ema_trend", "dual_ema"])
        self.assertIn("vwap_bands", tiers.get_entitlements("Pro")["methods_allowed"])


class TestNoOrderPathReachesForecast(unittest.TestCase):
    """Static check that the forecast modules contain no order plumbing.

    A grep, not a proof -- but the alternative is trusting that nobody adds
    one later, and this is the assertion that makes the guarantee cheap to
    keep.
    """

    FILES = ("data/forecast.py", "engines/forecast.py",
             "services/forecast/reference_server.py")

    def test_no_order_writes(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        banned = ("INSERT INTO orders", "UPDATE orders", "DELETE FROM orders",
                  "INSERT INTO fills", "/api/orders", "place_order",
                  "create_order")
        for rel in self.FILES:
            src = open(os.path.join(root, rel), encoding="utf-8").read()
            for b in banned:
                self.assertNotIn(b, src, "{} references {!r}".format(rel, b))

    def test_no_tier_methods_invented(self):
        """The modules must not monkey-patch the strategy registry, and must
        not write to methods_allowed.

        Checked against code only -- docstrings and comments are stripped
        first, because these files discuss methods_allowed in prose precisely
        to explain why they do not touch it, and matching that explanation
        would be a false positive.
        """
        import tokenize
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("data/forecast.py", "engines/forecast.py",
                    "services/forecast/reference_server.py"):
            path = os.path.join(root, rel)
            with open(path, "rb") as fh:
                source = fh.read()
            stripped = []
            for tok in tokenize.tokenize(iter(source.splitlines(True)).__next__):
                if tok.type == tokenize.COMMENT:
                    continue
                if tok.type == tokenize.STRING and tok.line.strip().startswith(
                        ('"""', "'''", 'r"""', "r'''")):
                    # docstring text: keep it out of the comparison
                    if len(tok.string) > 3:
                        continue
                stripped.append(tok.string)
            text = "".join(stripped)
            for banned in ("STRATEGIES", "_DISPATCH", "methods_allowed",
                           "ENTITLEMENTS"):
                self.assertNotIn(
                    banned, text,
                    "{} references {!r} outside a docstring".format(rel, banned))


if __name__ == "__main__":
    unittest.main(verbosity=2)
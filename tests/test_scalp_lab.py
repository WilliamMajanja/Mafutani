"""Scalp Lab: the preview must actually run the strategy it names.

The Scalp Lab shipped broken in three independent ways, all of which
returned HTTP 200 and looked fine from the outside:

  1. api/routes.py::_engine_preview looked for a `preview` symbol on
     engines.signals (never existed), then called the per-strategy
     functions as fn(candles, params) when they take
     (candles, quote, params). The TypeError was swallowed by a bare
     `except Exception: pass`, so every strategy -- including VWAP
     Pullback -- fell through to a generic EMA9/EMA21 computation and
     reported it under that strategy's name.
  2. The UI sent no symbol, so every request was rejected with
     "strategy and symbol required".
  3. The preview's tier gate ran *after* the stub short-circuit, so a
     Free user querying a Pro stub got 200 "stub" instead of the 403
     that tells them to upgrade.

These tests pin the behaviour so the failure cannot come back quietly.
"""
import os
import sqlite3
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from api.routes import _engine_preview, _signal_label  # noqa: E402


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


def add_candles(conn, symbol, closes, base_ts=1_700_000_000):
    """Insert closes as 1m candles oldest-first, with a matching quote."""
    # candles_1m.symbol and quotes_l1.symbol are FKs to symbols.symbol.
    conn.execute(
        "INSERT OR IGNORE INTO symbols (symbol, asset_type, name, enabled,"
        " tick_size, lot_size) VALUES (?, 'test', ?, 1, 0.01, 0.0001)",
        (symbol, symbol),
    )
    conn.executemany(
        "INSERT INTO candles_1m (symbol, ts, open, high, low, close, volume)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(symbol, base_ts + i, c, c, c, c, 1000.0)
         for i, c in enumerate(closes)],
    )
    last = closes[-1]
    conn.execute(
        "INSERT INTO quotes_l1 (symbol, ts, bid, ask, last, source)"
        " VALUES (?, ?, ?, ?, ?, 'test')",
        (symbol, base_ts + len(closes), last - 0.01, last + 0.01, last),
    )
    conn.commit()


def add_strategies(conn):
    """The schema seeds the strategies table itself, so there is nothing
    to add -- this exists as an explicit assertion that the rows are
    there, and that they carry the *drifted* params_json that motivates
    the catalogue merge in _strategies."""
    names = {r[0] for r in conn.execute("SELECT name FROM strategies")}
    from engines.signals import STRATEGIES
    missing = {s["name"] for s in STRATEGIES} - names
    assert not missing, "schema.sql is missing strategy rows: %s" % missing


# Reasons that only the named strategy's own implementation can emit.
# A generic EMA9/EMA21 fallback would produce none of these. Entries
# ending in "*" match as a prefix (the engine formats some reasons with
# a computed value, e.g. "no-extreme-z1.65").
STRATEGY_FINGERPRINTS = {
    "ema_trend": ("no-cross", "warming-up", "ema-bull-cross", "ema-bear-cross",
                  "spread-too-wide", "atr-too-low"),
    "dual_ema": ("no-cross", "warming-up", "dual-ema-bull-cross",
                 "dual-ema-bear-cross", "spread-too-wide"),
    "vwap_pullback": ("no-pullback", "warming-up", "vwap-pullback-long",
                      "vwap-pullback-short", "vwap-slope-flat", "no-vwap",
                      "spread-too-wide"),
    "mean_reversion": ("flat-vol", "warming-up", "spread-too-wide",
                       "no-extreme-z*", "mr-short-z*", "mr-long-z*"),
    "manual": ("manual", "manual-needs-side", "spread-too-wide"),
}


def reason_matches(reason, allowed):
    reason = reason or ""
    return any(reason == a or (a.endswith("*") and reason.startswith(a[:-1]))
               for a in allowed)


class TestPreviewReachesRealEngine(unittest.TestCase):
    # A flat series with one jump on the final bar. The strategies are
    # crossover/z-score systems that need a *transition* on the last bar
    # to fire, so a smooth ramp is the wrong fixture -- it produces
    # "no-cross" everywhere and proves nothing about which engine ran.
    FLAT, JUMP, NBARS = 100.0, 120.0, 100

    def series(self):
        return [self.FLAT] * (self.NBARS - 1) + [self.JUMP]

    def setUp(self):
        self.conn = fresh_db()
        add_strategies(self.conn)
        add_candles(self.conn, "AAA", self.series())

    def tearDown(self):
        self.conn.close()

    def test_each_strategy_reports_its_own_reason(self):
        """The core regression: a preview must not be one strategy's
        answer wearing another strategy's name."""
        for name, allowed_reasons in STRATEGY_FINGERPRINTS.items():
            r = _engine_preview(self.conn, name, "AAA", {})
            self.assertEqual(r.get("source"), "engine",
                             "%s did not reach the engine" % name)
            self.assertTrue(
                reason_matches(r.get("reason"), allowed_reasons),
                "%s produced reason %r, which no %s implementation can emit"
                " -- the generic EMA fallback is back"
                % (name, r.get("reason"), name))

    def test_each_strategy_fires_its_own_signal(self):
        """Stronger than distinct reasons: the engines must also
        disagree about direction, which one shared fallback cannot do."""
        got = {n: _engine_preview(self.conn, n, "AAA", {}).get("signal")
               for n in STRATEGY_FINGERPRINTS}
        # A jump in price: trend systems buy, mean reversion fades it.
        self.assertEqual(got["ema_trend"], "buy")
        self.assertEqual(got["dual_ema"], "buy")
        self.assertEqual(got["mean_reversion"], "sell")
        # If these all matched, one function was answering for all of them.
        self.assertEqual(len(set(got.values())), 3)

    def test_reasons_differ_across_strategies(self):
        reasons = {n: _engine_preview(self.conn, n, "AAA", {}).get("reason")
                   for n in STRATEGY_FINGERPRINTS}
        self.assertEqual(len(set(reasons.values())), len(reasons),
                         "strategies collapsed onto one answer: %r" % reasons)

    def test_response_carries_market_context(self):
        r = _engine_preview(self.conn, "ema_trend", "AAA", {})
        self.assertEqual(r.get("last"), self.JUMP)
        self.assertIn("bid", r)
        self.assertIn("ask", r)
        self.assertIn("spread_bps", r)
        self.assertEqual(r.get("bars"), self.NBARS)

    def test_quote_is_passed_to_the_engine(self):
        """The spread filter is part of the strategy. Omitting the quote
        would silently skip the max_spread_bps gate the live path
        enforces, so the preview would not reproduce live behaviour."""
        # The same series, but quoted at a 100% spread: must be vetoed.
        self.conn.execute("UPDATE quotes_l1 SET bid = 50.0, ask = 150.0")
        self.conn.commit()
        r = _engine_preview(self.conn, "ema_trend", "AAA",
                            {"fast": 9, "slow": 21, "max_spread_bps": 50})
        self.assertEqual(r.get("signal"), "flat")
        self.assertEqual(r.get("reason"), "spread-too-wide")

    def test_missing_candles_is_explicit_not_a_silent_flat(self):
        r = _engine_preview(self.conn, "ema_trend", "NOSUCH", {})
        self.assertEqual(r.get("bars"), 0)
        self.assertIn("no candles stored", r.get("reason", ""))
        self.assertEqual(r.get("signal"), "flat")

    def test_stub_kind_is_reported_as_stub(self):
        r = _engine_preview(self.conn, "rsi_fade", "AAA", {})
        self.assertEqual(r.get("source"), "engine")
        self.assertEqual(r.get("reason"), "stub/upgrade")
        self.assertEqual(r.get("signal"), "flat")

    def test_unknown_strategy_is_reported_not_crashed(self):
        r = _engine_preview(self.conn, "no_such_strategy", "AAA", {})
        self.assertEqual(r.get("signal"), "flat")
        self.assertEqual(r.get("reason"), "unknown-kind")

    def test_manual_requires_side_and_honours_it(self):
        self.assertEqual(
            _engine_preview(self.conn, "manual", "AAA", {})["reason"],
            "manual-needs-side")
        self.assertEqual(
            _engine_preview(self.conn, "manual", "AAA", {"side": "sell"})["signal"],
            "sell")

    def test_params_are_honoured(self):
        """Tuning a parameter must change the answer, which is only
        possible if params actually reach the engine."""
        loose = _engine_preview(self.conn, "mean_reversion", "AAA", {"entry_z": 0.5})
        tight = _engine_preview(self.conn, "mean_reversion", "AAA", {"entry_z": 99.0})
        self.assertEqual(loose.get("signal"), "sell",
                         "entry_z=0.5 must fire on a z=%.1f spike"
                         % float(loose.get("reason", "z0")[-4:]))
        self.assertEqual(tight.get("signal"), "flat")
        self.assertTrue(reason_matches(tight.get("reason"),
                                       ("no-extreme-z*", "flat-vol")))


class TestSignalLabel(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(_signal_label("buy"), "buy")
        self.assertEqual(_signal_label("long"), "buy")
        self.assertEqual(_signal_label("sell"), "sell")
        self.assertEqual(_signal_label("short"), "sell")
        # The engine returns side=None for "no signal". That must read
        # as flat, never as a truthy value.
        self.assertEqual(_signal_label(None), "flat")
        self.assertEqual(_signal_label(""), "flat")
        self.assertEqual(_signal_label("nonsense"), "flat")


class TestStrategyCatalogueMatchesEngine(unittest.TestCase):
    """The UI renders straight from /api/strategies, so a catalogue that
    disagrees with the engine hands the operator wrong defaults."""

    def test_db_params_json_drifts_from_engine(self):
        """Guards the reason the endpoint merges: confirm the seeded
        params_json is genuinely incomplete, so the merge is load-bearing
        rather than redundant."""
        import json as _json
        conn = fresh_db()
        try:
            from engines.signals import STRATEGIES
            drifted = []
            for row in conn.execute("SELECT name, params_json FROM strategies"):
                name, raw = row[0], row[1]
                can = [s for s in STRATEGIES if s["name"] == name]
                if not can:
                    continue
                stored = _json.loads(raw or "{}")
                if stored != can[0].get("params", {}):
                    drifted.append(name)
            self.assertTrue(drifted,
                            "no params_json drift found; if the seed is now "
                            "canonical this merge can be revisited")
            # Specifically: the spread gate and qty are what the seeded
            # rows omit, and they are the ones that change behaviour.
            ema = [s for s in STRATEGIES if s["name"] == "ema_trend"][0]
            self.assertIn("max_spread_bps", ema["params"])
            self.assertIn("qty", ema["params"])
        finally:
            conn.close()

    def test_every_canonical_strategy_has_matching_entry(self):
        from engines.signals import STRATEGIES
        names = {s["name"] for s in STRATEGIES}
        conn = fresh_db()
        try:
            rows = conn.execute("SELECT name FROM strategies").fetchall()
            self.assertEqual({r[0] for r in rows}, names)
        finally:
            conn.close()

    def test_db_mode_matches_engine_mode(self):
        """A DB row claiming 'wired' for a stub would make the catalogue
        promise logic the dispatch table cannot provide."""
        from engines.signals import STRATEGIES
        can = {s["name"]: s.get("mode", "wired") for s in STRATEGIES}
        conn = fresh_db()
        try:
            for row in conn.execute("SELECT name, mode FROM strategies"):
                self.assertEqual(row[1], can.get(row[0]),
                                 "%s mode disagrees with the engine" % row[0])
        finally:
            conn.close()

    def test_stub_strategies_declare_no_live_logic(self):
        """Every stub must be absent from the dispatch table, otherwise
        the catalogue would advertise a method that cannot trade."""
        from engines.signals import STUB_KINDS, _DISPATCH
        for name in STUB_KINDS:
            self.assertNotIn(name, _DISPATCH)


class TestScalpLabOverHTTP(unittest.TestCase):
    """End-to-end checks against a real server on a temp database.

    The three defects fixed here all returned HTTP 200, so unit-testing
    the helpers alone cannot prove the UI now works. These drive the
    actual routes: symbol is required, the tier gate runs before the stub
    short-circuit, and a wired strategy reports its own engine reason.
    """
    @classmethod
    def setUpClass(cls):
        import http.server
        import shutil
        import socket
        import subprocess
        import tempfile
        import time
        import urllib.error
        import urllib.request
        import json as _json

        cls._json = _json
        cls._urllib = urllib.request
        cls._HTTPError = urllib.error.HTTPError

        tmp = tempfile.mkdtemp(prefix="scalplab-")
        cls.tmpdir = tmp
        db = os.path.join(tmp, "test.db")
        import sqlite3
        from core.auth import pbkdf2_hash
        seed = sqlite3.connect(db)
        seed.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
            seed.executescript(f.read())
        # two tiers that differ on a stub method: rsi_fade is Pro-only
        # and has no implementation, so it exercises both gates at once.
        pw = pbkdf2_hash("labpw")
        for email, tier in (("free@lab", "Free"), ("pro@lab", "Pro")):
            seed.execute(
                "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, ?)",
                (email, pw, tier))
            seed.execute(
                "INSERT INTO accounts (user_id, cash) VALUES"
                " ((SELECT id FROM users WHERE email = ?), 1000000.0)", (email,))
        seed.commit()
        seed.close()

        # candles + quote for a symbol the preview can read
        seed = sqlite3.connect(db)
        closes = [100.0] * 99 + [120.0]
        seed.execute("INSERT OR IGNORE INTO symbols (symbol, asset_type, name,"
                     " enabled, tick_size, lot_size)"
                     " VALUES ('TESTX','test','Test X',1,0.01,0.0001)")
        seed.executemany(
            "INSERT INTO candles_1m (symbol, ts, open, high, low, close, volume)"
            " VALUES ('TESTX', ?, ?, ?, ?, ?, 1000.0)",
            [(1700000000 + i, c, c, c, c) for i, c in enumerate(closes)])
        seed.execute(
            "INSERT INTO quotes_l1 (symbol, ts, bid, ask, last, source)"
            " VALUES ('TESTX', 1700000100, 119.99, 120.01, 120.0, 'test')")
        seed.commit()
        seed.close()

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        env = dict(os.environ)
        env["DB_PATH"] = db
        env["PORT"] = str(cls.port)
        # The synthetic feed re-prices quotes in the background, which
        # races any assertion on an exact seeded price.
        env["MAFUTANI_NO_FEED"] = "1"
        cls.proc = subprocess.Popen(
            [sys.executable, "app.py"], cwd=WORKDIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # wait for the port to accept
        deadline = time.time() + 30
        up = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), 0.4):
                    up = True
                    break
            except OSError:
                if cls.proc.poll() is not None:
                    break
                time.sleep(0.15)
        cls.server_up = up
        if not up:
            cls.proc.terminate()
            out = cls.proc.stdout.read().decode(errors="replace") if cls.proc.stdout else ""
            raise AssertionError("test server did not start:\n%s" % out[-2000:])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:
            try:
                cls.proc.kill()
            except Exception:
                pass
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        if not self.server_up:
            self.skipTest("server unavailable")

    def call(self, method, path, body=None, tok=None, cookies=None):
        data = self._json.dumps(body).encode() if body is not None else None
        req = self._urllib.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                   data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "scalp-lab-test/1.0")
        if tok:
            req.add_header("X-CSRF", tok)
            req.add_header("X-CSRF-Token", tok)
        for k, v in (cookies or {}).items():
            req.add_header("Cookie", "%s=%s" % (k, v))
        try:
            with self._urllib.urlopen(req, timeout=30) as f:
                raw = f.read().decode()
                return f.status, (self._json.loads(raw) if raw else {})
        except self._HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, (self._json.loads(raw) if raw else {})
            except Exception:
                return e.code, {"error": raw[:200]}

    def _session_cookie(self, email):
        """Log in, return (cookie jar, entitlements incl. CSRF token)."""
        req = self._urllib.Request(
            "http://127.0.0.1:%d/api/auth/login" % self.port,
            data=self._json.dumps({"email": email, "password": "labpw"}).encode(),
            method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "scalp-lab-test/1.0")
        with self._urllib.urlopen(req, timeout=30) as f:
            cookies = f.headers.get_all("Set-Cookie") or []
            f.read()
        jar = {}
        for c in cookies:
            kv = c.split(";")[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                jar[k.strip()] = v.strip()
        status, ent = self.call("GET", "/api/me/entitlements", cookies=jar)
        assert status == 200, "entitlements failed: %r" % ent
        return jar, ent

    def test_preview_requires_symbol(self):
        jar, ent = self._session_cookie("pro@lab")
        status, body = self.call("POST", "/api/signals/preview",
                                 {"strategy": "ema_trend", "params": {}},
                                 tok=ent.get("csrf_token"), cookies=jar)
        self.assertEqual(status, 400, "got %r" % body)
        self.assertIn("symbol", str(body.get("error", "")).lower())

    def test_backtest_requires_symbol(self):
        jar, ent = self._session_cookie("pro@lab")
        status, body = self.call("POST", "/api/backtests",
                                 {"strategy": "ema_trend", "params": {}},
                                 tok=ent.get("csrf_token"), cookies=jar)
        self.assertEqual(status, 400, "got %r" % body)
        self.assertIn("symbol", str(body.get("error", "")).lower())

    def test_preview_works_over_http(self):
        jar, ent = self._session_cookie("pro@lab")
        status, body = self.call(
            "POST", "/api/signals/preview",
            {"strategy": "ema_trend", "symbol": "TESTX", "params": {}},
            tok=ent.get("csrf_token"), cookies=jar)
        self.assertEqual(status, 200, "preview failed: %r" % body)
        self.assertEqual(body.get("source"), "engine")
        self.assertEqual(body.get("signal"), "buy")
        self.assertEqual(body.get("reason"), "ema-bull-cross")

    def test_strategies_catalogue_serves_engine_params(self):
        jar, _ = self._session_cookie("pro@lab")
        status, body = self.call("GET", "/api/strategies", cookies=jar)
        self.assertEqual(status, 200)
        by_name = {s["name"]: s for s in body["strategies"]}
        from engines.signals import STRATEGIES
        for canon in STRATEGIES:
            self.assertIn(canon["name"], by_name)
            self.assertEqual(by_name[canon["name"]]["params"], canon["params"],
                             "%s params do not match the engine" % canon["name"])
            self.assertEqual(by_name[canon["name"]]["mode"], canon["mode"])
            self.assertIsInstance(by_name[canon["name"]]["params"], dict)

    def test_quote_endpoint(self):
        jar, _ = self._session_cookie("pro@lab")
        status, body = self.call("GET", "/api/quote?symbol=TESTX", cookies=jar)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("found"))
        self.assertAlmostEqual(float(body["last"]), 120.0, places=4)
        self.assertIn("spread_bps", body)
        # unknown symbol is a clear negative, not a 500
        status, body = self.call("GET", "/api/quote?symbol=NOPE", cookies=jar)
        self.assertEqual(status, 200)
        self.assertFalse(body.get("found"))
        # missing symbol is a client error
        status, body = self.call("GET", "/api/quote", cookies=jar)
        self.assertEqual(status, 400)

    def test_quote_requires_auth(self):
        status, _ = self.call("GET", "/api/quote?symbol=TESTX")
        self.assertEqual(status, 401)

    def test_tier_gate_runs_before_the_stub_short_circuit(self):
        """Regression: the stub check used to run first, so a Free user
        asking for a Pro stub got 200 'stub' instead of a 403 telling
        them to upgrade."""
        jar, ent = self._session_cookie("free@lab")
        # rsi_fade is Pro-only AND a stub. Free must be refused.
        status, body = self.call(
            "POST", "/api/signals/preview",
            {"strategy": "rsi_fade", "symbol": "TESTX", "params": {}},
            tok=ent.get("csrf_token"), cookies=jar)
        self.assertEqual(status, 403,
                         "Free user was not refused a Pro stub: %r" % body)
        self.assertIsNotNone(body.get("error"))

    def test_free_tier_can_still_use_free_strategies(self):
        jar, ent = self._session_cookie("free@lab")
        status, body = self.call(
            "POST", "/api/signals/preview",
            {"strategy": "ema_trend", "symbol": "TESTX", "params": {}},
            tok=ent.get("csrf_token"), cookies=jar)
        self.assertEqual(status, 200, "Free user blocked from a Free strategy: %r" % body)
        self.assertEqual(body.get("signal"), "buy")

    def test_catalogue_marks_entitlement_per_strategy(self):
        jar, _ = self._session_cookie("free@lab")
        status, body = self.call("GET", "/api/strategies", cookies=jar)
        by_name = {s["name"]: s for s in body["strategies"]}
        self.assertTrue(by_name["ema_trend"]["allowed"])
        self.assertFalse(by_name["mean_reversion"]["allowed"])
        self.assertFalse(by_name["book_imbalance"]["allowed"])


if __name__ == "__main__":
    unittest.main()

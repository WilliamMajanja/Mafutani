"""Scalp Live ticket: the order and flatten paths the UI depends on.

Three defects here were invisible from the client because the endpoints
returned HTTP 200 with a well-formed body:

  1. POST /api/orders nested everything under "order", so a client
     reading r.id / r.status got undefined and rendered "order #?" for a
     trade that had in fact filled. The fields are now echoed flat too.
  2. GET /api/portfolio returned every position row, including the
     qty=0 rows left behind after a close, so the portfolio listed a
     dozen flat symbols as holdings.
  3. POST /api/positions/flatten silently skipped positions its risk
     check refused, answering {count: 0} -- indistinguishable from
     "you held nothing" while the operator was still exposed.

Plus the live gate: it used to swallow a tier-module failure with a bare
`except Exception: pass`, which waved every tier through live trading.
"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)


class TestPortfolioFiltersFlatRows(unittest.TestCase):
    """Zero-qty rows are history, not holdings."""

    def setUp(self):
        import sqlite3
        from core.auth import pbkdf2_hash
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.execute(
            "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, 'Pro')",
            ("p@lab", pbkdf2_hash("pw")))
        self.uid = self.conn.execute(
            "SELECT id FROM users WHERE email = 'p@lab'").fetchone()[0]
        self.conn.execute("INSERT INTO accounts (user_id, cash) VALUES (?, 100000)",
                          (self.uid,))
        for sym, qty in (("AAA", 5.0), ("BBB", 0.0), ("CCC", -2.0)):
            self.conn.execute(
                "INSERT INTO positions (user_id, symbol, qty, avg_price,"
                " realized_pnl) VALUES (?, ?, ?, 10.0, 0.0)", (self.uid, sym, qty))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_query_excludes_zero_qty(self):
        rows = self.conn.execute(
            "SELECT symbol FROM positions WHERE user_id = ? AND qty != 0",
            (self.uid,)).fetchall()
        self.assertEqual({r[0] for r in rows}, {"AAA", "CCC"})

    def test_short_position_is_retained(self):
        """qty != 0 keeps shorts. A naive abs()/truthiness filter would
        drop a short and hide real exposure."""
        rows = self.conn.execute(
            "SELECT symbol FROM positions WHERE user_id = ? AND qty != 0",
            (self.uid,)).fetchall()
        self.assertIn("CCC", {r[0] for r in rows})

    def test_portfolio_route_uses_the_filtered_query(self):
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        self.assertIn('FROM positions WHERE user_id = ? AND qty != 0', src)


class TestOrderResponseShape(unittest.TestCase):
    def test_order_fields_are_echoed_flat(self):
        """Guards the "order #?" bug: r.id and r.status must be present
        at the top level, not only under r.order."""
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        marker = '"order": od, "fill": fill'
        self.assertIn(marker, src, "order response shape changed")
        block = src.split(marker, 1)[1][:400]
        for field in ('"id"', '"status"', '"side"', '"symbol"'):
            self.assertIn(field, block,
                          "%s is not echoed at the top level" % field)

    def test_nested_order_still_present(self):
        """Existing consumers read r.order; keep it."""
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        self.assertIn('"order": od', src)


class TestFlattenReportsRejections(unittest.TestCase):
    def test_rejected_list_is_reported(self):
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        self.assertIn('"rejected": rejected', src)
        self.assertIn('"rejected_count"', src)

    def test_flatten_no_longer_silently_continues(self):
        """A risk refusal must be recorded, not dropped on the floor."""
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        flatten = src.split("def _flatten", 1)[1].split("def _portfolio", 1)[0]
        self.assertIn("flatten_reject", flatten)
        self.assertIn('rejected.append', flatten)
        # both failure stages, not just the risk one
        self.assertIn('"stage": "risk"', flatten)
        self.assertIn('"stage": "fill"', flatten)

    def test_total_failure_is_a_conflict(self):
        """count 0 with everything refused must not read as success."""
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        flatten = src.split("def _flatten", 1)[1].split("def _portfolio", 1)[0]
        self.assertIn("if rejected and not closed", flatten)
        self.assertIn("status=409", flatten)


class TestLiveGateFailsClosed(unittest.TestCase):
    def test_tier_import_failure_is_not_swallowed(self):
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        create = src.split("def _order_create", 1)[1].split("def _order_list", 1)[0]
        live = create.split('if mode == "live":', 1)[1][:2000]
        # No bare pass after the entitlement check: a broken tier module
        # must block live trading, not wave it through.
        self.assertIn("except ImportError", live)
        self.assertIn("live trading unavailable", live)
        self.assertNotIn("except Exception:\n                pass", live)

    def test_live_requires_explicit_confirm(self):
        src = open(os.path.join(WORKDIR, "api", "routes.py"), encoding="utf-8").read()
        create = src.split("def _order_create", 1)[1].split("def _order_list", 1)[0]
        live = create.split('if mode == "live":', 1)[1][:2500]
        self.assertIn('data.get("confirm")', live)
        self.assertIn("confirm: true", live)


class TestScalpLiveOverHTTP(unittest.TestCase):
    """Full ticket flow against a real server, mirroring what the page
    sends: quote -> buy -> portfolio -> flatten."""
    @classmethod
    def setUpClass(cls):
        import socket
        import sqlite3
        import subprocess
        import tempfile
        import time
        from core.auth import pbkdf2_hash

        cls.tmp = tempfile.mkdtemp(prefix="scalplive-")
        db = os.path.join(cls.tmp, "t.db")
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
            c.executescript(f.read())
        c.execute("INSERT INTO users (email, password_hash, tier)"
                  " VALUES ('live@lab', ?, 'Institutional')", (pbkdf2_hash("pw"),))
        uid = c.execute("SELECT id FROM users WHERE email='live@lab'").fetchone()[0]
        c.execute("INSERT INTO accounts (user_id, cash) VALUES (?, 1000000.0)", (uid,))
        c.execute("INSERT OR IGNORE INTO symbols (symbol, asset_type, name, enabled,"
                  " tick_size, lot_size) VALUES ('LIVE1','test','Live1',1,0.01,0.0001)")
        c.executemany(
            "INSERT INTO candles_1m (symbol, ts, open, high, low, close, volume)"
            " VALUES ('LIVE1', ?, 50, 50, 50, 50, 1000.0)",
            [(1700000000 + i,) for i in range(60)])
        c.execute("INSERT INTO quotes_l1 (symbol, ts, bid, ask, last, source)"
                  " VALUES ('LIVE1', 1700000060, 49.99, 50.01, 50.0, 'test')")
        c.commit()
        c.close()

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        env = dict(os.environ)
        env["DB_PATH"] = db
        env["PORT"] = str(cls.port)
        # The synthetic feed re-prices quotes in the background, which
        # races any assertion on an exact seeded price.
        env["MAFUTANI_NO_FEED"] = "1"
        env["LIVE_TRADING_ENABLED"] = "1"
        env["MAFUTANI_NO_FEED"] = "1"
        cls.proc = subprocess.Popen([sys.executable, "app.py"], cwd=WORKDIR,
                                    env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        cls.up = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), 0.4):
                    cls.up = True
                    break
            except OSError:
                if cls.proc.poll() is not None:
                    break
                time.sleep(0.15)
        if not cls.up:
            out = cls.proc.stdout.read().decode(errors="replace")
            raise AssertionError("server did not start:\n%s" % out[-2000:])

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
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if not self.up:
            self.skipTest("server unavailable")
        self.cookies = {}
        self.tok = None
        self.bootstrap()

    def call(self, method, path, body=None, capture=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
        for k, v in self.cookies.items():
            req.add_header("Cookie", "%s=%s" % (k, v))
        if self.tok:
            req.add_header("X-CSRF", self.tok)
            req.add_header("X-CSRF-Token", self.tok)
        try:
            with urllib.request.urlopen(req, timeout=30) as f:
                raw = f.read().decode()
                if capture:
                    for c in f.headers.get_all("Set-Cookie") or []:
                        kv = c.split(";")[0]
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            self.cookies[k.strip()] = v.strip()
                return f.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, (json.loads(raw) if raw else {})
            except Exception:
                return e.code, {"error": raw[:200]}

    def bootstrap(self):
        """Log in, capture the session cookie, fetch the CSRF token."""
        s, _ = self.call("POST", "/api/auth/login",
                         {"email": "live@lab", "password": "pw"}, capture=True)
        assert s == 200, "login failed"
        s, ent = self.call("GET", "/api/me/entitlements")
        assert s == 200, "entitlements failed: %r" % ent
        self.tok = ent.get("csrf_token")

    def test_ticket_flow_buy_then_flatten(self):
        # start flat
        self.call("POST", "/api/positions/flatten", {"mode": "paper"})
        s, pf = self.call("GET", "/api/portfolio")
        self.assertEqual(s, 200)
        self.assertEqual([p for p in pf["positions"] if p["qty"] != 0], [])
        cash0 = pf["cash"]

        # quote the ticket symbol
        s, q = self.call("GET", "/api/quote?symbol=LIVE1")
        self.assertEqual(s, 200)
        self.assertTrue(q["found"])
        self.assertAlmostEqual(float(q["last"]), 50.0, places=4)

        # buy, as the Buy button does
        s, o = self.call("POST", "/api/orders",
                         {"symbol": "LIVE1", "side": "buy", "type": "market",
                          "qty": 2, "mode": "paper"})
        self.assertEqual(s, 201, o)
        # flat echo present (this is the "order #?" bug)
        self.assertIn("id", o)
        self.assertIn("status", o)
        self.assertEqual(o["status"], "filled")
        self.assertIn("fill", o)
        self.assertIsNotNone(o["fill"].get("price"))
        self.assertIsNotNone(o["fill"].get("cash_after"))

        # position + cash moved
        s, pf = self.call("GET", "/api/portfolio")
        held = [p for p in pf["positions"] if p["symbol"] == "LIVE1"]
        self.assertEqual(len(held), 1)
        self.assertAlmostEqual(float(held[0]["qty"]), 2.0, places=6)
        self.assertLess(pf["cash"], cash0)
        # only open positions are listed
        self.assertTrue(all(p["qty"] != 0 for p in pf["positions"]))

        # flatten closes it and reports cleanly
        s, fl = self.call("POST", "/api/positions/flatten", {"mode": "paper"})
        self.assertEqual(s, 200, fl)
        self.assertEqual(fl["count"], 1)
        self.assertEqual(fl["rejected"], [])
        s, pf = self.call("GET", "/api/portfolio")
        self.assertEqual([p for p in pf["positions"] if p["symbol"] == "LIVE1"], [])

    def test_live_requires_confirm(self):
        s, o = self.call("POST", "/api/orders",
                         {"symbol": "LIVE1", "side": "buy", "type": "market",
                          "qty": 1, "mode": "live"})
        self.assertEqual(s, 403, o)
        self.assertIn("confirm", str(o.get("error")))

    def test_quote_requires_symbol_and_auth(self):
        s, o = self.call("GET", "/api/quote")
        self.assertEqual(s, 400)
        s, o = self.call("GET", "/api/quote?symbol=NOPE")
        self.assertEqual(s, 200)
        self.assertFalse(o.get("found"))


if __name__ == "__main__":
    unittest.main()

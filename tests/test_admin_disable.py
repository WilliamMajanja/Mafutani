"""RFF-005 admin write paths: disable user, disable symbol, export audit CSV.

RFF-005's acceptance line is "Admin can disable symbol/user and export audit
CSV". The *reads* those words imply have existed since the schema landed --
the session lookup filters `users.disabled = 0` and three query paths filter
`symbols.enabled = 1` -- but nothing could ever set either column, and the
audit view was JSON-only behind a 500-row cap. The capability was described
and unreachable.

These tests call the handlers directly with a fake handler rather than going
over HTTP. The auth layer here is `tier == "admin"` on the user row, which
can be exercised honestly by constructing the row, and driving the real
handler keeps the assertions on behaviour (did the column flip, was an audit
row written) instead of on the shape of a JSON response.

Two things get extra attention because they are easy to ship wrong:

* the **default direction** of each toggle, since an operator clicking
  "disable" should not also have to send the value;
* **CSV formula injection**, because an audit export is filed, forwarded and
  opened in a spreadsheet, and its cells carry operator-supplied text.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from api.routes import Router, _csv_field  # noqa: E402


SCHEMA = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  tier TEXT NOT NULL DEFAULT 'Free',
  created_at INTEGER NOT NULL DEFAULT 0,
  disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE symbols (
  symbol TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL DEFAULT 'equity',
  name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  tick_size REAL NOT NULL DEFAULT 0.01,
  lot_size REAL NOT NULL DEFAULT 1.0,
  exchange TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'USD',
  sector TEXT NOT NULL DEFAULT ''
);
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  actor_user_id INTEGER,
  action TEXT NOT NULL,
  tier TEXT NOT NULL DEFAULT '',
  paper_live TEXT NOT NULL DEFAULT 'paper',
  details_json TEXT NOT NULL DEFAULT '{}'
);
"""


class FakeHandler(object):
    """Minimal stand-in for http.server's handler.

    Records status/headers/body so a test can assert on what was actually
    sent, rather than on what the handler returned (every one of these
    returns True, success or failure alike).
    """

    def __init__(self, body=None):
        self.status = None
        self.headers_sent = []
        self.wfile = _Sink()
        self.command = "POST"
        self.path = "/"
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers = {"Content-Type": "application/json",
                        "Content-Length": str(len(raw))}
        self.rfile = _Sink(raw)

    # -- http.server surface used by _send_json / _send_csv / _err -----
    def send_response(self, code):
        self.status = int(code)

    def send_header(self, k, v):
        self.headers_sent.append((k, v))

    def end_headers(self):
        pass

    def header(self, name):
        for k, v in self.headers_sent:
            if k.lower() == name.lower():
                return v
        return None

    def json(self):
        return json.loads(self.wfile.text())


class _Sink(object):
    def __init__(self, data=b""):
        self._buf = bytearray(data)

    def read(self, n=-1):
        if n is None or n < 0:
            out = bytes(self._buf)
            del self._buf[:]
            return out
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def write(self, b):
        self._buf.extend(b)

    def text(self):
        return self._buf.decode("utf-8")


def _user(uid, tier="admin", email=None):
    return {"id": uid, "email": email or ("u%d@x.test" % uid),
            "tier": tier, "disabled": 0}


class AdminWriteBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.conn = sqlite3.connect(os.path.join(self._tmp, "t.db"))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO users (id,email,password_hash,tier)"
                          " VALUES (1,'a@x.test','x','admin')")
        self.conn.execute("INSERT INTO users (id,email,password_hash,tier)"
                          " VALUES (2,'b@x.test','x','pro')")
        self.conn.execute("INSERT INTO symbols (symbol,name,enabled) VALUES ('BTCUSD','Bitcoin',1)")
        self.conn.execute("INSERT INTO symbols (symbol,name,enabled) VALUES ('DEAD','Dead',1)")
        self.conn.commit()

        self.router = Router()
        self.actor = _user(1)

        def fake_require_user(conn, h, _a=self.actor):
            return dict(_a), None
        self.router._require_user = fake_require_user

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def flag(self, table, column, keyval, idcol="id"):
        """Read back the column a handler claims to have written."""
        r = self.conn.execute(
            "SELECT %s FROM %s WHERE %s = ?" % (column, table, idcol), (keyval,)
        ).fetchone()
        return None if r is None else r[0]

    def audit_actions(self):
        return [r[0] for r in self.conn.execute(
            "SELECT action FROM audit_log ORDER BY id").fetchall()]


class TestDisableUser(AdminWriteBase):
    def test_disable_flips_the_column(self):
        h = FakeHandler({"disabled": True})
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/2/disable")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.flag("users", "disabled", 2), 1)

    def test_bodyless_request_disables(self):
        """The operator clicks one button. Defaulting to disable means the
        endpoint cannot be inverted by forgetting the value."""
        h = FakeHandler()
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/2/disable")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.flag("users", "disabled", 2), 1)

    def test_explicit_false_re_enables(self):
        self.conn.execute("UPDATE users SET disabled = 1 WHERE id = 2")
        self.conn.commit()
        h = FakeHandler({"disabled": False})
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/2/disable")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.flag("users", "disabled", 2), 0)

    def test_admin_cannot_disable_self(self):
        """Lockout guard. The session filter rejects a disabled user, so
        self-disable removes the only way back into the admin page."""
        h = FakeHandler({"disabled": True})
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/1/disable")
        self.assertEqual(h.status, 409)
        self.assertEqual(self.flag("users", "disabled", 1), 0,
                         "the account was disabled despite the refusal")

    def test_unknown_user_is_404(self):
        h = FakeHandler({"disabled": True})
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/999/disable")
        self.assertEqual(h.status, 404)

    def test_non_numeric_id_is_400(self):
        h = FakeHandler({"disabled": True})
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/abc/disable")
        self.assertEqual(h.status, 400)

    def test_writes_an_audit_row(self):
        h = FakeHandler({"disabled": True})
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/2/disable")
        self.assertIn("admin.user.disable", self.audit_actions())
        row = self.conn.execute(
            "SELECT actor_user_id, details_json FROM audit_log"
            " WHERE action = 'admin.user.disable'").fetchone()
        self.assertEqual(row[0], 1, "audit must name the acting admin")
        self.assertIn("user_id", row[1])

    def test_enable_is_audited_distinctly(self):
        self.conn.execute("UPDATE users SET disabled = 1 WHERE id = 2")
        self.conn.commit()
        h = FakeHandler({"disabled": False})
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/2/disable")
        self.assertIn("admin.user.enable", self.audit_actions())

    def test_non_admin_is_refused(self):
        self.router._require_user = lambda conn, h: (_user(2, tier="pro"), None)
        h = FakeHandler({"disabled": True})
        self.router._admin_user_disable(self.conn, h, "/api/admin/users/1/disable")
        self.assertEqual(h.status, 403)
        self.assertEqual(self.flag("users", "disabled", 1), 0,
                         "a non-admin changed an account")


class TestDisableSymbol(AdminWriteBase):
    def test_disable_flips_enabled_off(self):
        h = FakeHandler()
        self.router._admin_symbol_disable(self.conn, h, "/api/admin/symbols/BTCUSD/disable")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.flag("symbols", "enabled", "BTCUSD", "symbol"), 0)

    def test_enable_flips_it_back(self):
        self.conn.execute("UPDATE symbols SET enabled = 0 WHERE symbol = 'DEAD'")
        self.conn.commit()
        h = FakeHandler({"enabled": True})
        self.router._admin_symbol_disable(self.conn, h, "/api/admin/symbols/DEAD/disable")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.flag("symbols", "enabled", "DEAD", "symbol"), 1)

    def test_unknown_symbol_is_404(self):
        h = FakeHandler()
        self.router._admin_symbol_disable(self.conn, h, "/api/admin/symbols/NOPE/disable")
        self.assertEqual(h.status, 404)

    def test_lowercase_path_is_normalised(self):
        h = FakeHandler()
        self.router._admin_symbol_disable(self.conn, h, "/api/admin/symbols/btcusd/disable")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.flag("symbols", "enabled", "BTCUSD", "symbol"), 0)

    def test_audited(self):
        h = FakeHandler()
        self.router._admin_symbol_disable(self.conn, h, "/api/admin/symbols/BTCUSD/disable")
        self.assertIn("admin.symbol.disable", self.audit_actions())

    def test_non_admin_is_refused(self):
        self.router._require_user = lambda conn, h: (_user(2, tier="pro"), None)
        h = FakeHandler()
        self.router._admin_symbol_disable(self.conn, h, "/api/admin/symbols/BTCUSD/disable")
        self.assertEqual(h.status, 403)
        self.assertEqual(self.flag("symbols", "enabled", "BTCUSD", "symbol"), 1)


class TestEnforcementIsAlreadyWired(AdminWriteBase):
    """The write is only worth having if the read side honours it.

    These assert the *existing* queries, because the endpoint would be a
    no-op button if any of them ignored the column -- the same trap this
    project has hit repeatedly: a value that is stored and never used.
    """

    def test_session_lookup_excludes_disabled_users(self):
        with open(os.path.join(ROOT, "api", "routes.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertRegex(src, r"FROM users WHERE id = \? AND disabled = 0")

    def test_symbol_queries_filter_enabled(self):
        with open(os.path.join(ROOT, "api", "routes.py"), encoding="utf-8") as f:
            src = f.read()
        n = len(re.findall(r"FROM symbols WHERE enabled = 1", src))
        self.assertGreaterEqual(n, 1)

    def test_disabled_symbol_disappears_from_a_live_query(self):
        self.conn.execute("UPDATE symbols SET enabled = 0 WHERE symbol = 'DEAD'")
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT symbol FROM symbols WHERE enabled = 1 ORDER BY symbol"
        ).fetchall()
        self.assertNotIn("DEAD", [r[0] for r in rows])


class TestAuditCsvExport(AdminWriteBase):
    def _export(self, qs=None, actor=None):
        if actor is not None:
            self.router._require_user = lambda conn, h: (dict(actor), None)
        h = FakeHandler()
        h.headers = {}
        self.router._admin_audit_csv(self.conn, h, qs or {})
        return h

    def _seed_audit(self):
        self.conn.execute(
            "INSERT INTO audit_log (ts,actor_user_id,action,tier,paper_live,details_json)"
            " VALUES (1700000000, 2, 'order.place', 'pro', 'live', '{\"qty\":1}')")
        self.conn.commit()

    def test_content_type_is_csv(self):
        self._seed_audit()
        h = self._export()
        self.assertEqual(h.status, 200)
        self.assertIn("text/csv", h.header("Content-Type"))

    def test_header_row_lists_the_audit_columns(self):
        self._seed_audit()
        h = self._export()
        self.assertTrue(h.wfile.text().startswith(
            '"id","ts","actor_user_id","action","tier","paper_live","details_json"'))

    def test_row_count_and_order(self):
        for i in range(3):
            self.conn.execute(
                "INSERT INTO audit_log (ts,actor_user_id,action,tier,paper_live,"
                "details_json) VALUES (?,?,?,'pro','paper','{}')",
                (1700000000 + i, 2, "act%d" % i))
        self.conn.commit()
        h = self._export()
        lines = [l for l in h.wfile.text().strip().split("\r\n") if l]
        self.assertEqual(len(lines), 4, "expected header + 3 rows")
        self.assertIn("act2", lines[1], "rows must come back newest-first")

    def test_action_filter(self):
        self._seed_audit()
        self.conn.execute(
            "INSERT INTO audit_log (ts,actor_user_id,action,tier,paper_live,"
            "details_json) VALUES (1,2,'other','pro','paper','{}')")
        self.conn.commit()
        h = self._export({"action": ["order.place"]})
        body = h.wfile.text()
        self.assertIn("order.place", body)
        self.assertNotIn("other", body)

    def test_timestamp_is_iso_not_epoch(self):
        self._seed_audit()
        h = self._export()
        body = h.wfile.text()
        self.assertIn("2023-11-14T", body)
        self.assertNotIn("1700000000", body)

    def test_non_admin_is_refused(self):
        self._seed_audit()
        h = self._export(actor=_user(2, tier="pro"))
        self.assertEqual(h.status, 403)
        body = h.wfile.text()
        self.assertEqual(body, '{"error": "admin only"}')
        self.assertNotIn("order.place", body, "audit rows leaked to a non-admin")

    def test_filename_cannot_inject_a_header(self):
        self._seed_audit()
        h = FakeHandler()
        self.router._admin_audit_csv(self.conn, h, {})
        disp = h.header("Content-Disposition") or ""
        self.assertEqual(disp, 'attachment; filename="audit-export.csv"')
        self.assertNotIn("\r", disp)
        self.assertNotIn("\n", disp)


class TestCsvFormulaInjection(unittest.TestCase):
    """The export's cells carry operator text; Excel evaluates =+-@ prefixes.

    A cell like `=cmd|'/c calc'!A1` executes on open. Prefixing a quote
    makes the cell inert text while leaving the value a human reads intact.
    """

    def test_neutralises_every_trigger_prefix(self):
        for prefix in ("=", "+", "-", "@", "\t", "\r"):
            out = _csv_field(prefix + "cmd")
            self.assertTrue(out.startswith("'"), "prefix %r not neutralised" % prefix)

    def test_leaves_ordinary_text_alone(self):
        for v in ("order.place", "1700000000", "", "pro", "a-b_c"):
            self.assertEqual(_csv_field(v), v)

    def test_none_becomes_empty(self):
        self.assertEqual(_csv_field(None), "")

    def test_injection_survives_as_inert_text(self):
        out = _csv_field("=1+1")
        self.assertEqual(out, "'=1+1")
        self.assertFalse(out.startswith("="))


class TestRoutesAreDispatched(unittest.TestCase):
    """Wiring contract.

    The handlers existing proves nothing about the table -- the failure this
    project keeps rediscovering is a function that is defined and never
    called. These assert the dispatch entries exist.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "api", "routes.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_user_disable_route_registered(self):
        self.assertIn('path.startswith("/api/admin/users/")', self.src)
        self.assertIn("path.endswith(\"/disable\")", self.src)

    def test_symbol_disable_route_registered(self):
        self.assertIn('path.startswith("/api/admin/symbols/")', self.src)

    def test_csv_route_registered(self):
        self.assertIn('path == "/api/admin/audit.csv"', self.src)

    def test_each_handler_is_actually_called(self):
        for fn in ("_admin_user_disable", "_admin_symbol_disable",
                   "_admin_audit_csv"):
            self.assertEqual(
                self.src.count("self." + fn + "("), 1,
                "%s is referenced %d times; expected exactly one call site"
                % (fn, self.src.count("self." + fn + "(")))


if __name__ == "__main__":
    unittest.main(verbosity=2)

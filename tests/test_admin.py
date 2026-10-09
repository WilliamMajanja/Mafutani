"""Tests for application administration tools.

The failure modes here are specific:
* a backup that is not restorable
* a restore that writes a partial backup
* a config lint that misses a real problem
* an audit view that leaks a credential
* a health check that hides a failure
"""
import json
import os
import sys
import tempfile
import unittest
import zipfile

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from core.admin import (backup, restore, lint_config, audit_view,  # noqa: E402
                        health, export_journal, AdminError)
from data.nosql import DocStore  # noqa: E402


class TestBackupRestore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.root = os.path.join(self._tmp, "app")
        os.makedirs(os.path.join(self.root, "data"))
        with open(os.path.join(self.root, "data", "trading.db"), "w") as f:
            f.write("db contents")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_backup_creates_file(self):
        path = backup(self.root)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(path.endswith(".zip"))

    def test_backup_contains_files(self):
        path = backup(self.root)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            self.assertIn("data/trading.db", names)
            self.assertIn("manifest.json", names)

    def test_backup_manifest(self):
        path = backup(self.root)
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(manifest["files"], 1)

    def test_restore_round_trip(self):
        path = backup(self.root)
        # Corrupt the original.
        with open(os.path.join(self.root, "data", "trading.db"), "w") as f:
            f.write("corrupted")
        manifest = restore(self.root, path)
        self.assertEqual(manifest["files"], 1)
        with open(os.path.join(self.root, "data", "trading.db")) as f:
            self.assertEqual(f.read(), "db contents")

    def test_restore_missing_backup(self):
        with self.assertRaises(AdminError):
            restore(self.root, "/nonexistent.zip")

    def test_restore_incomplete_backup(self):
        # A zip with a manifest that lies about the file count.
        path = os.path.join(self._tmp, "bad.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"files": 5}))
            zf.writestr("data/trading.db", "x")
        with self.assertRaises(AdminError):
            restore(self.root, path)

    def test_restore_no_manifest(self):
        path = os.path.join(self._tmp, "nomanifest.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("data/trading.db", "x")
        with self.assertRaises(AdminError):
            restore(self.root, path)

    def test_backup_does_not_back_up_itself(self):
        path = backup(self.root)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        self.assertNotIn("backups", names)


class TestLintConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.root = os.path.join(self._tmp, "app")
        os.makedirs(os.path.join(self.root, "data"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_db_is_warning(self):
        findings = lint_config(self.root)
        levels = [f["level"] for f in findings]
        self.assertIn("warning", levels)

    def test_no_findings_when_healthy(self):
        # Create the db file so the warning goes away.
        with open(os.path.join(self.root, "data", "trading.db"), "w") as f:
            f.write("x")
        findings = lint_config(self.root)
        # There may still be findings, but not the db one.
        db_findings = [f for f in findings if "trading.db" in f.get("where", "")]
        self.assertEqual(db_findings, [])

    def test_unwritable_docs_is_error(self):
        # os.access returns True for root regardless of permissions, so
        # this test only applies to non-root users.
        if os.geteuid() == 0:
            self.skipTest("running as root; permission checks do not apply")
        docs = os.path.join(self.root, "data", "docs")
        os.makedirs(docs)
        os.chmod(docs, 0o444)
        try:
            findings = lint_config(self.root)
            errors = [f for f in findings if f["level"] == "error"]
            self.assertTrue(any("docs" in f["where"] for f in errors))
        finally:
            os.chmod(docs, 0o755)


class TestAuditView(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = DocStore(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_returns_documents(self):
        self.store.put("audit", {"id": "1", "event": "login"})
        view = audit_view(self.store)
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["event"], "login")

    def test_redacts_credentials(self):
        self.store.put("audit", {"id": "1", "note": "key=SECRETVALUE"})
        view = audit_view(self.store)
        self.assertNotIn("SECRETVALUE", view[0]["note"])

    def test_empty_collection(self):
        self.assertEqual(audit_view(self.store), [])


class TestHealth(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.root = os.path.join(self._tmp, "app")
        os.makedirs(os.path.join(self.root, "data"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_health_returnsed(self):
        h = health(self.root)
        self.assertIn("status", h)
        self.assertIn("checks", h)
        self.assertIn(h["status"], ("ok", "degraded", "down"))

    def test_health_with_db(self):
        import sqlite3
        db_path = os.path.join(self.root, "data", "trading.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        h = health(self.root, conn=conn)
        db_check = [c for c in h["checks"] if c["name"] == "database"][0]
        self.assertEqual(db_check["status"], "ok")
        conn.close()


class TestExportJournal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        import sqlite3
        self.db_path = os.path.join(self._tmp, "test.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, side TEXT);
            CREATE TABLE fills (id INTEGER PRIMARY KEY, order_id INTEGER,
                                user_id INTEGER, symbol TEXT, qty REAL,
                                price REAL, ts INTEGER);
            INSERT INTO orders (id, user_id, side) VALUES (1, 1, 'buy');
            INSERT INTO fills (order_id, user_id, symbol, qty, price, ts)
            VALUES (1, 1, 'BTCUSD', 1.0, 100.0, 1000);
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_export_json(self):
        out = export_journal(self.conn, user_id=1, format="json")
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["symbol"], "BTCUSD")

    def test_export_csv(self):
        out = export_journal(self.conn, user_id=1, format="csv")
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "symbol,qty,price,ts,side")

    def test_export_only_own_data(self):
        self.conn.execute(
            "INSERT INTO orders (id, user_id, side) VALUES (2, 2, 'sell')")
        self.conn.execute(
            "INSERT INTO fills (order_id, user_id, symbol, qty, price, ts) "
            "VALUES (2, 2, 'ETHUSD', 2.0, 200.0, 2000)")
        self.conn.commit()
        out = export_journal(self.conn, user_id=1, format="json")
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["symbol"], "BTCUSD")

    def test_export_bad_format(self):
        with self.assertRaises(AdminError):
            export_journal(self.conn, user_id=1, format="xml")


if __name__ == "__main__":
    unittest.main()

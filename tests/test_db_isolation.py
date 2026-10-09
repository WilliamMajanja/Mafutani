"""The test suite must not be able to write to the live database.

This file exists because it happened three times. Each time, a test
constructed something that opens "the default database" -- a lazily-created
PaperBroker connection, or a module that read an env var name that did not
match the one ``data/db.py`` actually reads -- and wrote rows into
``data/trading.db`` while the suite reported OK.

A guard in ``tests/__init__.py`` redirects ``DB_PATH`` to a sandbox. This
file exists to prove that guard still works, because a guard that silently
stops working is worse than no guard: it would restore the exact condition
it was added to prevent while looking like protection.

Three checks, deliberately independent:

1. the bootstrap took effect (``DB_PATH`` is not the live file)
2. the live file is genuinely untouched by a real broker write
3. no test module hardcodes a DB env var that ``data/db.py`` ignores

The third is the one that catches the original bug class by name. It is a
grep, and greps rot -- so it reads ``data/db._default_db_path`` to learn
which variable is authoritative rather than hardcoding ``DB_PATH`` again.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import LIVE_DB, SANDBOX_DB
from data import db as db_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(ROOT, "tests")


class TestBootstrapIsActive(unittest.TestCase):
    def test_db_path_is_not_the_live_database(self):
        self.assertNotEqual(
            os.path.abspath(os.environ.get("DB_PATH", "")),
            os.path.abspath(LIVE_DB),
            "DB_PATH points at the live database; the sandbox guard is "
            "not active and tests can write to it")

    def test_sandbox_file_exists_and_is_writable(self):
        self.assertTrue(SANDBOX_DB, "MAFUTANI_TEST_SANDBOX was never recorded")
        self.assertTrue(os.path.exists(SANDBOX_DB))
        self.assertTrue(os.access(SANDBOX_DB, os.W_OK))

    def test_data_db_reads_the_variable_we_set(self):
        """The variable under test must be the one data/db.py consults.

        Checked by asking the module, not by asserting a literal -- so this
        still passes if someone renames DB_PATH and updates the guard, and
        fails if they rename it without updating the guard.
        """
        resolved = os.path.abspath(db_mod._default_db_path())
        self.assertEqual(resolved, os.path.abspath(os.environ["DB_PATH"]))


class TestLiveDatabaseIsUnreachable(unittest.TestCase):
    """The property that matters, tested the way it actually broke.

    Not "does the guard exist" but "can a broker write land in the live
    file". This is the exact shape of the test_vibe_interactive bug:
    construct a PaperBroker with no conn= and place an order.
    """

    def test_paper_broker_write_lands_in_sandbox_not_live(self):
        """A broker with no conn= -- the exact shape that caused the bug.

        Constructs it, forces the lazy connection, and places a real order.
        Then checks the ledger tables, not the file.

        File-level checking (size, mtime) does not work here: the feed
        thread in the running server writes quotes_l1 continuously, so the
        live file's mtime changes whether or not a test touched it. A
        fingerprint test would either fail forever or -- worse, if someone
        loosened the tolerance -- pass for the wrong reason. Ledger row
        counts are the honest signal: nothing but the feed writes quotes,
        but only an order writes an order.
        """
        from data.db import connect
        live = connect(LIVE_DB)

        def ledger():
            return tuple(live.execute(
                "SELECT (SELECT COUNT(*) FROM orders),"
                "       (SELECT COUNT(*) FROM fills),"
                "       (SELECT COUNT(*) FROM positions)"
            ).fetchone())

        before = ledger()
        try:
            from brokers.paper import PaperBroker
            b = PaperBroker(user_id=1, cash=100000.0)   # no conn=
            self.assertIsNotNone(b.conn)                 # force lazy open
            try:
                b.place({"symbol": "BTCUSD", "side": "buy", "qty": 0.001,
                         "type": "market", "mode": "paper"})
            except Exception:
                # The sandbox may lack the rows a real match needs. The
                # assertion below is about *where* it wrote, not whether
                # it succeeded, so an unmatched order is still a valid
                # probe -- as long as it did not land in the live file.
                pass
            finally:
                b.close()
            after = ledger()
        finally:
            live.close()

        self.assertEqual(before, after,
                         "a PaperBroker built without conn= wrote to the "
                         "live database (orders/fills/positions changed)")

    def test_connect_reports_the_sandbox(self):
        conn = db_mod.connect()
        try:
            path = conn.execute("PRAGMA database_list").fetchone()[2]
        finally:
            conn.close()
        self.assertNotEqual(os.path.abspath(path), os.path.abspath(LIVE_DB))


class TestNoStrayDbEnvVars(unittest.TestCase):
    """Catch the MAFUTANI_DB mistake at the source.

    That bug was a test setting an env var that nothing reads. The symptom
    was invisible -- the suite passed, the guard did not exist yet, and the
    only clue was rows appearing in a database no test claimed to touch. So
    the check is on the *name*: any DB-ish env var a test sets must be one
    data/db.py actually consults.
    """

    def _authoritative_names(self):
        """Env var names data/db.py reads, read from its own source.

        Parsed rather than grepped for a literal, because hardcoding DB_PATH
        here would just re-create the bug this file exists to catch: if the
        variable is ever renamed, a hardcoded test keeps passing while the
        guard in tests/__init__.py quietly stops protecting anything.

        Matches both call styles in the file:
            os.environ.get("DB_PATH", ...)   -> "DB_PATH"
            os.environ["DB_PATH"] = ...     -> "DB_PATH"
        """
        with open(db_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        found = set()
        for m in re.finditer(r"os\.environ(?:\.get)?\(?[\(\[]?"
                             r'[\'"]([A-Za-z_][A-Za-z0-9_]*)[\'"]', src):
            name = m.group(1)
            if any(k in name.upper() for k in ("DB", "PATH", "DATABASE")):
                found.add(name)
        return found

    def test_tests_only_set_env_vars_the_db_layer_reads(self):
        authoritative = self._authoritative_names()
        self.assertTrue(authoritative,
                        "could not determine which env vars data/db.py reads")

        # This file and tests/__init__.py are exempt: the first defines the
        # allowlist, the second is the guard itself and sets its own marker.
        exempt = {"test_db_isolation.py", "__init__.py"}
        suspicious = []
        for name in sorted(os.listdir(TESTS_DIR)):
            if not name.endswith(".py") or name in exempt:
                continue
            with open(os.path.join(TESTS_DIR, name), encoding="utf-8") as fh:
                src = fh.read()
            for var in re.findall(r"environ(?:\.get)?\(?[\(\[]?\"([A-Z_]+)\"", src):
                if not any(k in var for k in ("DB", "PATH", "DATABASE")):
                    continue
                if var not in authoritative:
                    suspicious.append("%s sets %s" % (name, var))

        self.assertEqual(suspicious, [],
                         "these tests set DB-ish env vars that data/db.py "
                         "does not read, so they silently hit the default "
                         "database: %s" % ", ".join(suspicious))

    def test_known_bad_name_is_rejected(self):
        """Pin the specific regression: MAFUTANI_DB is not a thing."""
        authoritative = self._authoritative_names()
        self.assertNotIn("MAFUTANI_DB", authoritative)
        self.assertIn("DB_PATH", authoritative)


if __name__ == "__main__":
    unittest.main(verbosity=2)
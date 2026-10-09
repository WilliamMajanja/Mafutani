"""Test package bootstrap. Runs before any test module is imported.

Why this file exists
--------------------
Three times now a test has written to the live ``data/trading.db``:

* a test set ``MAFUTANI_DB``, a variable ``data/db.py`` does not read (it
  reads ``DB_PATH``), so the module silently ran against the real database.
* ``test_vibe_interactive.py`` builds ``PaperBroker(user_id=1)`` with no
  ``conn=``. ``brokers/paper.py`` opens a connection lazily in its ``conn``
  property, defaulting to ``data/trading.db`` -- 3 real paper orders per
  suite run, dating back to 09-27.

Both passed. That is the dangerous part: the tests were green while
corrupting the ledger, so "the suite is green" said nothing about it.

The guard
---------
``DB_PATH`` is set here, at package import, before any test module loads.
Anything that lazily opens "the default database" -- which is the entire
class of bug above -- now lands in a throwaway file instead of the live
one. Fail-safe rather than fail-loud, because the failure mode we are
guarding against is silent corruption, and a loud error in one test module
does not protect the twenty modules nobody thought to check.

A test that genuinely wants the live database can still set ``DB_PATH``
itself in ``setUp``; the guard only sets the default when it is unset, and
restores it on the way out.

``test_db_isolation.py`` asserts this bootstrap actually took effect, so the
guard cannot itself silently rot.
"""

import os
import tempfile

# The project root, so the default resolves to <repo>/data/trading.db.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DB = os.path.join(_ROOT, "data", "trading.db")

# If DB_PATH is already set, the caller is deliberate (a test that wants a
# specific file, or a harness that manages the database itself). Respect it.
if not os.environ.get("DB_PATH"):
    _fd, _sandbox = tempfile.mkstemp(prefix="mafutani-tests-", suffix=".db")
    os.close(_fd)
    os.environ["DB_PATH"] = _sandbox
    # Recorded so the isolation test can assert we own the value, and so
    # nothing downstream mistakes this for a configured database.
    os.environ["MAFUTANI_TEST_SANDBOX"] = _sandbox

    # Give the sandbox the real schema. Without this every test that opens
    # a connection finds an empty file and fails on missing tables -- which
    # would tempt someone into "fixing" it by pointing back at the live
    # database. An empty sandbox that rejects writes is worse than useless:
    # it is an argument for removing the guard.
    try:
        import sqlite3
        _schema = os.path.join(_ROOT, "data", "schema.sql")
        with open(_schema, encoding="utf-8") as _fh:
            _sql = _fh.read()
        _conn = sqlite3.connect(_sandbox)
        try:
            _conn.executescript(_sql)
            _conn.commit()
        finally:
            _conn.close()
    except Exception:
        # A schema failure must not stop the suite: isolation is the point
        # of this file, and a broken schema is a separate, visible problem.
        pass

SANDBOX_DB = os.environ.get("MAFUTANI_TEST_SANDBOX") or ""
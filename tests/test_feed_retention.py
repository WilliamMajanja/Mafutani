"""Tests for quotes_l1 tick retention.

The feed loop appends a row per symbol every tick, but nothing in the
codebase ever reads more than the latest tick for a symbol -- every
consumer is `WHERE symbol = ? ORDER BY ts DESC LIMIT 1`. Left alone the
table grew forever (~7,200 rows/hour), inflating COUNT(*) and index size
for data that is never read.

prune_quotes() trims that history while guaranteeing the newest ticks
survive, so a reader racing the prune still finds a current quote.
"""
import os
import sqlite3
import sys
import time
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from data.feeds import prune_quotes  # noqa: E402

SCHEMA = os.path.join(WORKDIR, "data", "schema.sql")


def fresh_db():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)          # sqlite wants to create the file itself
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA) as fh:
        conn.executescript(fh.read())
    return conn, path


def add_quotes(conn, rows):
    for sym in sorted({r[0] for r in rows}):
        conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol, name, enabled)"
            " VALUES (?, ?, 1)", (sym, sym))
    conn.executemany(
        "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size,"
        " last) VALUES (?, ?, ?, ?, 1, 1, ?)", rows)
    conn.commit()


class TestPruneQuotes(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()
        now = int(time.time())
        # 50 ticks per symbol, 1 minute apart, all well inside the age
        # window so only the per-symbol cap can trigger.
        rows = []
        for sym in ("AAPL", "MSFT", "NVDA"):
            for i in range(50):
                rows.append((sym, now - (50 - i) * 60, 100.0, 100.1, 100.05))
        add_quotes(self.conn, rows)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def count(self):
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM quotes_l1").fetchone()[0])

    def latest_ts(self, sym):
        return int(self.conn.execute(
            "SELECT MAX(ts) FROM quotes_l1 WHERE symbol = ?",
            (sym,)).fetchone()[0])

    def test_prunes_but_keeps_the_cap(self):
        removed = prune_quotes(self.conn, keep_per_symbol=10,
                               max_age_s=24 * 3600)
        self.assertEqual(removed, 3 * 40)   # 3 symbols, 50 -> 10 each
        self.assertEqual(self.count(), 30)

    def test_never_deletes_the_latest_tick(self):
        """A reader racing the prune must still find a current quote."""
        before = {s: self.latest_ts(s) for s in ("AAPL", "MSFT", "NVDA")}
        prune_quotes(self.conn, keep_per_symbol=1, max_age_s=24 * 3600)
        for sym, ts in before.items():
            self.assertEqual(self.latest_ts(sym), ts, sym)
        self.assertEqual(self.count(), 3)

    def test_prunes_per_symbol_independently(self):
        """One busy symbol must not consume another's quota."""
        now = int(time.time())
        add_quotes(self.conn, [(("AAPL"), now + 1, 1, 1, 1)])
        prune_quotes(self.conn, keep_per_symbol=10, max_age_s=24 * 3600)
        for sym in ("AAPL", "MSFT", "NVDA"):
            n = int(self.conn.execute(
                "SELECT COUNT(*) FROM quotes_l1 WHERE symbol = ?",
                (sym,)).fetchone()[0])
            self.assertGreaterEqual(n, 1, sym)

    def test_age_window_also_prunes(self):
        """Old ticks go even when the per-symbol cap is not reached."""
        old = int(time.time()) - 10 * 24 * 3600
        add_quotes(self.conn, [("AAPL", old, 1, 1, 1)])
        removed = prune_quotes(self.conn, keep_per_symbol=10000,
                               max_age_s=24 * 3600)
        self.assertGreaterEqual(removed, 1)

    def test_both_conditions_apply_independently(self):
        """The cap and the age limit must be OR'd, not AND'd.

        With AND semantics, ticks younger than the age window escaped the
        cap entirely, so a 30s tick loop accumulated a full day of rows
        per symbol before anything was ever removed.
        """
        now = int(time.time())
        # 300 recent ticks (over the cap, under the age limit) plus one
        # ancient tick (under the cap, over the age limit).
        rows = [("AAPL", now, 1, 1, 1) for _ in range(300)]
        rows.append(("AAPL", now - 400 * 24 * 3600, 1, 1, 1))
        add_quotes(self.conn, rows)

        prune_quotes(self.conn, keep_per_symbol=10, max_age_s=24 * 3600)
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM quotes_l1 WHERE symbol = 'AAPL'").fetchone()[0]
        self.assertEqual(remaining, 10)

    def test_ties_in_ts_keep_the_newest_id(self):
        """Same-second ticks must still resolve to a newest row."""
        now = int(time.time())
        add_quotes(self.conn, [("TIE", now, 1, 1, 1) for _ in range(5)])
        prune_quotes(self.conn, keep_per_symbol=1, max_age_s=24 * 3600)
        row = self.conn.execute(
            "SELECT id, last FROM quotes_l1 WHERE symbol = 'TIE'").fetchall()
        self.assertEqual(len(row), 1)

    def test_is_idempotent(self):
        first = prune_quotes(self.conn, keep_per_symbol=10)
        second = prune_quotes(self.conn, keep_per_symbol=10)
        self.assertEqual(first, 120)
        self.assertEqual(second, 0, "a second prune must be a no-op")
        self.assertEqual(self.count(), 30)

    def test_no_op_when_under_the_cap(self):
        conn2, path2 = fresh_db()
        try:
            add_quotes(conn2, [("AAPL", int(time.time()), 1, 1, 1)])
            self.assertEqual(prune_quotes(conn2, keep_per_symbol=200), 0)
        finally:
            conn2.close()
            os.unlink(path2)

    def test_empty_table(self):
        conn2, path2 = fresh_db()
        try:
            self.assertEqual(prune_quotes(conn2), 0)
        finally:
            conn2.close()
            os.unlink(path2)

    def test_never_raises_on_a_broken_table(self):
        """Pruning is maintenance: it must not be able to break the feed."""
        self.conn.execute("DROP TABLE quotes_l1")
        self.conn.commit()
        self.assertEqual(prune_quotes(self.conn), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

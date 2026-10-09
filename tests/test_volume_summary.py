"""Tests for the materialised per-symbol volume summary.

Motivation: the screener ranks by total candle volume, which meant
`SUM(volume) GROUP BY symbol` over ~1.5M rows on *every* request. On the
development machine (/mnt/c, a Windows mount) that cost 17-18s, making
/api/screener effectively unusable.

These tests pin the behaviour that fixed it -- the summary is read, it is
rebuilt when stale, and a bulk import cannot leave it silently wrong --
and guard the fallback path for databases predating the table.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from api import routes  # noqa: E402
from data.db import _migrate  # noqa: E402


SCHEMA = os.path.join(WORKDIR, "data", "schema.sql")


def fresh_db():
    """A small, isolated database with the real schema."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA) as fh:
        conn.executescript(fh.read())
    _migrate(conn)
    return conn, path


def add_candles(conn, rows):
    # candles_1m.symbol is a FK to symbols, so the symbols must exist first.
    for sym in sorted({r[0] for r in rows}):
        conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol, name, enabled)"
            " VALUES (?, ?, 1)", (sym, sym))
    conn.executemany(
        "INSERT OR REPLACE INTO candles_1m (symbol, ts, open, high, low,"
        " close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()


class TestVolumeSummary(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()
        routes.invalidate_volume_cache()
        add_candles(self.conn, [
            ("AAPL", 100, 1, 1, 1, 1, 10),
            ("AAPL", 160, 1, 1, 1, 1, 20),
            ("MSFT", 100, 1, 1, 1, 1, 5),
        ])

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        routes.invalidate_volume_cache()

    def test_totals_are_correct(self):
        vols = routes.volume_by_symbol(self.conn)
        self.assertEqual(vols["AAPL"], 30)
        self.assertEqual(vols["MSFT"], 5)

    def test_summary_is_persisted_not_just_cached(self):
        """The point of the change: the answer lives in a table."""
        routes.volume_by_symbol(self.conn)
        rows = {r["symbol"]: r for r in
                self.conn.execute("SELECT * FROM symbol_volume")}
        self.assertIn("AAPL", rows)
        self.assertEqual(rows["AAPL"]["total_volume"], 30)
        self.assertEqual(rows["AAPL"]["candle_rows"], 2)
        self.assertEqual(rows["AAPL"]["stale"], 0, "freshly built")

    def test_second_read_is_served_from_the_summary(self):
        routes.volume_by_symbol(self.conn)
        routes.invalidate_volume_cache()
        # A sentinel proves the aggregation is not re-run: it would
        # overwrite this with the real total.
        self.conn.execute("UPDATE symbol_volume SET total_volume = 424242")
        self.conn.commit()
        self.assertEqual(routes.volume_by_symbol(self.conn)["AAPL"], 424242)

    def test_stale_summary_is_rebuilt(self):
        routes.volume_by_symbol(self.conn)
        add_candles(self.conn, [("AAPL", 220, 1, 1, 1, 1, 7)])
        self.conn.execute("UPDATE symbol_volume SET stale = 1")
        self.conn.commit()
        routes.invalidate_volume_cache()
        self.assertEqual(routes.volume_by_symbol(self.conn)["AAPL"], 37)

    def test_refresh_symbol_volume_rebuilds(self):
        routes.volume_by_symbol(self.conn)
        add_candles(self.conn, [("NVDA", 100, 1, 1, 1, 1, 99)])
        routes.refresh_symbol_volume(self.conn)
        self.assertEqual(routes.volume_by_symbol(self.conn)["NVDA"], 99)

    def test_invalidate_flags_stale(self):
        routes.volume_by_symbol(self.conn)
        routes.invalidate_symbol_volume(self.conn)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(stale),0) s FROM symbol_volume").fetchone()
        self.assertEqual(int(row["s"]), len(
            self.conn.execute("SELECT * FROM symbol_volume").fetchall()))

    def test_empty_database(self):
        conn2, path2 = fresh_db()
        try:
            self.assertEqual(routes.volume_by_symbol(conn2), {})
        finally:
            conn2.close()
            os.unlink(path2)

    def test_falls_back_when_summary_table_missing(self):
        """An old database without the table must still work."""
        self.conn.execute("DROP TABLE symbol_volume")
        self.conn.commit()
        routes.invalidate_volume_cache()
        vols = routes.volume_by_symbol(self.conn)
        self.assertEqual(vols["AAPL"], 30)
        self.assertEqual(vols["MSFT"], 5)

    def test_returns_a_copy(self):
        """Callers must not be able to corrupt the cache."""
        vols = routes.volume_by_symbol(self.conn)
        vols["AAPL"] = -1
        self.assertEqual(routes.volume_by_symbol(self.conn)["AAPL"], 30)

    def test_null_volumes_handled(self):
        add_candles(self.conn, [("NULLVOL", 100, 1, 1, 1, 1, None)])
        routes.invalidate_symbol_volume(self.conn)
        self.assertIn("NULLVOL", routes.volume_by_symbol(self.conn))


class TestVolumeSummaryPerf(unittest.TestCase):
    """Guard the actual regression: per-request aggregation cost."""

    def setUp(self):
        self.conn, self.path = fresh_db()
        routes.invalidate_volume_cache()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        routes.invalidate_volume_cache()

    def test_repeated_reads_do_not_rescan_candles(self):
        # 120k rows across 36 symbols -- small enough to build fast here,
        # large enough that a per-request re-aggregation would dominate.
        rows = []
        for s in range(36):
            sym = "SYM{:02d}".format(s)
            for i in range(3000):
                rows.append((sym, 1000 + i * 60, 1, 1, 1, 1, 1))
        add_candles(self.conn, rows)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0],
            36 * 3000)

        routes.volume_by_symbol(self.conn)          # builds the summary
        routes.invalidate_volume_cache()
        t0 = time.time()
        for _ in range(20):
            routes.volume_by_symbol(self.conn)
        elapsed = time.time() - t0

        # A full re-aggregation per read would be orders of magnitude
        # slower. 20 reads must stay comfortably under a second.
        self.assertLess(elapsed, 1.0,
                        "volume_by_symbol is re-aggregating per call "
                        "({:.3f}s for 20 reads)".format(elapsed))

    def test_covering_index_present(self):
        """The index is what keeps the rebuild itself affordable."""
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='candles_1m'")}
        self.assertIn("idx_candles_sym_vol", names)

    def test_aggregation_plan_uses_covering_index(self):
        plan = " ".join(r[3] for r in self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT symbol, COALESCE(SUM(volume),0)"
            " FROM candles_1m GROUP BY symbol"))
        self.assertIn("COVERING INDEX idx_candles_sym_vol", plan, plan)


class TestCandleRowTotal(unittest.TestCase):
    """_candle_row_total must be exact AND must not rescan the table.

    The status and admin endpoints render this number; getting it wrong
    either lies to the operator or reintroduces the multi-second scan.
    """

    def setUp(self):
        self.conn, self.path = fresh_db()
        routes.invalidate_volume_cache()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)
        routes.invalidate_volume_cache()

    def _truth(self):
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM candles_1m").fetchone()[0])

    def test_matches_real_count_when_fresh(self):
        add_candles(self.conn, [
            ("AAPL", 100, 1, 1, 1, 1, 10),
            ("AAPL", 160, 1, 1, 1, 1, 20),
            ("MSFT", 100, 1, 1, 1, 1, 5),
        ])
        routes.volume_by_symbol(self.conn)
        self.assertEqual(routes._candle_row_total(self.conn), 3)
        self.assertEqual(routes._candle_row_total(self.conn), self._truth())

    def test_falls_back_when_summary_missing(self):
        add_candles(self.conn, [
            ("AAPL", 100, 1, 1, 1, 1, 10),
            ("MSFT", 100, 1, 1, 1, 1, 5),
        ])
        self.conn.execute("DROP TABLE symbol_volume")
        self.conn.commit()
        self.assertEqual(routes._candle_row_total(self.conn), 2)

    def test_falls_back_when_summary_stale(self):
        """A stale summary must not be reported as the truth."""
        add_candles(self.conn, [("AAPL", 100, 1, 1, 1, 1, 10)])
        routes.volume_by_symbol(self.conn)
        add_candles(self.conn, [("MSFT", 100, 1, 1, 1, 1, 5)])
        self.conn.execute("UPDATE symbol_volume SET stale = 1")
        self.conn.commit()
        self.assertEqual(routes._candle_row_total(self.conn), 2)

    def test_empty_database(self):
        self.assertEqual(routes._candle_row_total(self.conn), 0)

    def test_no_table_at_all(self):
        self.conn.execute("DROP TABLE candles_1m")
        self.conn.execute("DROP TABLE symbol_volume")
        self.conn.commit()
        self.assertEqual(routes._candle_row_total(self.conn), -1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

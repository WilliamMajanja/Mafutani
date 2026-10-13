"""scripts/fetch_yahoo.py -- symbol mapping, bad-print detection, degeneracy.

The failures this module has to catch are quiet ones. A wrong ticker, a
stale flat block, or a dead exchange series all produce rows that look
exactly like real bars. Every test here is about a shape that would pass a
naive ingest and poison a backtest later.

Three real faults found by running it, all recorded below because the naive
version of each looked correct:
  * ZNC=F serves 3660.00 flat for six sessions against a true 2297.00, which
    gave ZINC a raw kurtosis of 1205 from a single bad print.
  * ZNC=F is more broadly dead: 2297.00 for 1171 consecutive sessions, one
    distinct close. Stored, it becomes a real-looking instrument with exactly
    zero volatility.
  * A 35% deletion threshold removed 179 of NGAS's 1258 bars, including
    both halves of the January 2022 European gas spike (+46%, -47%). The
    same threshold flagged EURGBP's +11.6%, the October 2022 UK mini-budget.
    Both real. Detection and deletion are now separate operations.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.fetch_yahoo import (DROP_THRESHOLD, IMPLAUSIBLE_DAILY_MOVE,
                                MIN_DISTINCT_CLOSES, UNRESOLVED, YAHOO_MAP,
                                find_bad_prints, is_degenerate,
                                strip_bad_prints)


def bar(ts, close, volume=1000.0, open_=None):
    return {"ts": ts, "open": open_ if open_ is not None else close,
            "high": close * 1.01, "low": close * 0.99, "close": close,
            "volume": volume}


class TestSymbolMap(unittest.TestCase):
    def test_every_mapped_symbol_is_distinct(self):
        self.assertEqual(len(YAHOO_MAP), len(set(YAHOO_MAP)))

    def test_no_symbol_is_mapped_and_also_declared_unresolved(self):
        """A symbol cannot be both available and admitted to be absent."""
        both = set(YAHOO_MAP) & set(UNRESOLVED)
        self.assertEqual(both, set(),
                         "declared unresolved but also mapped: %r" % both)

    def test_unresolved_entries_explain_themselves(self):
        for sym, why in UNRESOLVED.items():
            self.assertTrue(why.strip(), "%s has no explanation" % sym)

    def test_mapping_covers_the_expected_asset_classes(self):
        for expected in ("AAPL", "EURUSD", "XAUUSD", "BRENT", "WTI", "ALUM"):
            self.assertIn(expected, YAHOO_MAP)

    def test_no_lowercase_or_whitespace_in_keys(self):
        for k in YAHOO_MAP:
            self.assertEqual(k, k.strip().upper(), "bad key %r" % k)


class TestBadPrintDetection(unittest.TestCase):
    def test_a_clean_series_flags_nothing(self):
        rows = [bar(i * 86400, 100.0 + i * 0.1) for i in range(50)]
        self.assertEqual(find_bad_prints(rows), [])

    def test_a_fifty_percent_jump_is_flagged(self):
        rows = [bar(i * 86400, 100.0) for i in range(5)]
        rows += [bar((5 + i) * 86400, 150.0) for i in range(5)]
        self.assertTrue(find_bad_prints(rows))

    def test_euromini_budget_is_not_flagged(self):
        """+11.6% GBP in a day (October 2022).

        Not flagged, and correctly so: the 35% reporting threshold is well
        above a real currency crisis. An earlier draft of this test asserted
        it *should* be flagged, which would only have been true of a
        threshold low enough to be useless.
        """
        rows = [bar(i * 86400, 0.90) for i in range(10)]
        rows.append(bar(10 * 86400, 1.01))
        self.assertEqual(find_bad_prints(rows), [])

    def test_euromini_budget_is_not_deleted_by_default(self):
        """The lesson. It must survive an ordinary run."""
        rows = [bar(i * 86400, 0.90) for i in range(10)]
        rows.append(bar(10 * 86400, 1.01))
        kept, dropped = strip_bad_prints(rows)
        self.assertEqual(dropped, [], "deleted a real market move")
        self.assertEqual(len(kept), len(rows))

    def test_gas_spike_is_not_deleted_by_default(self):
        rows = [bar(i * 86400, 4.0) for i in range(10)]
        rows.append(bar(10 * 86400, 5.9))     # +47%
        rows.append(bar(11 * 86400, 3.1))     # -47%
        kept, dropped = strip_bad_prints(rows)
        self.assertEqual(dropped, [])
        self.assertEqual(len(kept), len(rows))

    def test_a_doubling_is_deleted_under_the_drop_threshold(self):
        rows = [bar(i * 86400, 100.0) for i in range(10)]
        rows.append(bar(10 * 86400, 400.0))   # +300%
        kept, dropped = strip_bad_prints(rows)
        self.assertEqual(len(dropped), 1, "a 300% print survived")
        self.assertEqual(len(kept), 10)

    def test_drop_threshold_is_stricter_than_the_report_threshold(self):
        """Otherwise 'report only' would still be deleting what it reports."""
        self.assertGreater(DROP_THRESHOLD, IMPLAUSIBLE_DAILY_MOVE)

    def test_a_sustained_level_shift_goes_as_a_block(self):
        """Iterated, so five identical bars do not survive the one that moved."""
        rows = [bar(i * 86400, 100.0) for i in range(5)]
        rows += [bar((5 + i) * 86400, 400.0) for i in range(5)]
        rows += [bar((10 + i) * 86400, 100.0) for i in range(5)]
        kept, dropped = strip_bad_prints(rows)
        self.assertEqual(len(dropped), 5,
                         "the flat run at the wrong level was left behind")
        self.assertTrue(all(abs(r["close"] - 100.0) < 1e-9 for r in kept))


class TestDegeneracy(unittest.TestCase):
    """A dead series stored as real is worse than an absent one."""

    def test_the_zinc_shape_is_caught(self):
        """The real case: 1171 sessions, one distinct close."""
        rows = [bar(i * 86400, 2297.0) for i in range(1171)]
        dead, why = is_degenerate(rows)
        self.assertTrue(dead)
        self.assertIn("distinct close", why)

    def test_a_real_series_passes(self):
        rows = [bar(i * 86400, 100.0 + (i % 97) * 1.3) for i in range(500)]
        dead, why = is_degenerate(rows)
        self.assertFalse(dead, why)

    def test_a_series_that_barely_moves_is_caught(self):
        """Many distinct closes, but almost no session-to-session change."""
        # Needs both gates satisfied so the *second* one is what fires:
        # a price that changes every 25th session over 1000 bars gives 40
        # distinct closes (clearing MIN_DISTINCT_CLOSES=30) while only 4% of
        # sessions move (under the 5% movement floor). A shorter series or a
        # shorter repeat trips the distinct-close check first, and this test
        # would then pass without exercising what it was written for.
        rows = []
        price = 100.0
        for i in range(1000):
            rows.append(bar(i * 86400, price))
            if i % 25 == 0:
                price += 0.5
        dead, why = is_degenerate(rows)
        self.assertTrue(dead, why)
        self.assertIn("moved", why)

    def test_a_low_volatility_but_live_series_passes(self):
        """Tiny daily moves on a market that is open every session."""
        rows = [bar(i * 86400, 100.0 + (i % 200) * 0.001) for i in range(500)]
        dead, why = is_degenerate(rows)
        self.assertFalse(dead, why)

    def test_threshold_is_configurable(self):
        rows = [bar(i * 86400, 100.0 + i * 0.01) for i in range(100)]
        self.assertFalse(is_degenerate(rows, min_distinct=10)[0])
        self.assertTrue(is_degenerate(rows, min_distinct=200)[0])

    def test_constant_is_named(self):
        self.assertGreater(MIN_DISTINCT_CLOSES, 1)


class TestRepositoryInvariants(unittest.TestCase):
    def test_fetcher_is_stdlib_only(self):
        import ast
        path = os.path.join(ROOT, "scripts", "fetch_yahoo.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    mods.add(node.module.split(".")[0])
        project = {"data", "core", "engines", "scripts", "api", "brokers"}
        bad = [m for m in sorted(mods)
               if m not in sys.stdlib_module_names and m not in project]
        self.assertEqual(bad, [], "third-party import: %r" % bad)

    def test_never_writes_to_candles_1m(self):
        """Daily bars in the 1m table would be fabricated intraday data.

        Checked against the SQL rather than the whole file: the module
        docstring has to *name* candles_1m to explain why it is not used,
        so a blanket substring check is wrong. An earlier version of this
        test stripped "candles_1d" out of the source to make that point and
        produced an unreadable failure message that looked like corruption.
        """
        import re
        path = os.path.join(ROOT, "scripts", "fetch_yahoo.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("candles_1d", src)
        statements = re.findall(
            r"(?:INTO|FROM|UPDATE|JOIN)\s+candles_1m", src)
        self.assertEqual(statements, [],
                         "fetcher executes SQL against candles_1m: %r"
                         % statements)

    def test_schema_declares_a_separate_daily_table(self):
        with open(os.path.join(ROOT, "data", "schema.sql"),
                  encoding="utf-8") as f:
            schema = f.read()
        self.assertIn("CREATE TABLE IF NOT EXISTS candles_1d", schema)
        self.assertIn("idx_candles_1d_sym_ts", schema)


if __name__ == "__main__":
    unittest.main(verbosity=2)

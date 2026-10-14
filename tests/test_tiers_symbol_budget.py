"""RFC-001 max_symbols enforcement.

`max_symbols` sat in ENTITLEMENTS for every tier from the first commit and
was read by nothing. `/api/me/entitlements` told a Free user they could
track 10 symbols; the server enforced no number at all.

The enforcement point is watchlist creation, which is the only place a user
asserts a symbol set. Two decisions are pinned here because both are easy to
get wrong and neither is visible from the happy path:

* the count is **distinct symbols across all watchlists**, not symbols
  summed per list, so the same symbol on three lists is one symbol;
* the check is **cumulative** -- a second watchlist cannot be used to
  sidestep a limit the first one already reached.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.tiers import (check_symbol_budget, max_symbols,  # noqa: E402
                        tracked_symbols, ENTITLEMENTS)


class TestMaxSymbolsValue(unittest.TestCase):
    def test_matches_the_entitlement_table(self):
        for tier, ent in ENTITLEMENTS.items():
            self.assertEqual(max_symbols(tier), ent["max_symbols"],
                             "%s: helper disagrees with ENTITLEMENTS" % tier)

    def test_free_is_ten_and_admin_is_hundred(self):
        self.assertEqual(max_symbols("Free"), 10)
        self.assertEqual(max_symbols("Admin"), 100)

    def test_unknown_tier_falls_back_to_free(self):
        self.assertEqual(max_symbols("Platinum"), max_symbols("Free"))

    def test_never_zero(self):
        """A zero limit would refuse every symbol, including an empty
        request, which reads as a broken endpoint rather than a tier cap."""
        self.assertGreaterEqual(max_symbols("anything"), 1)


class _Conn(object):
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        return _Rows(self._rows)


class _Rows(object):
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def _syms(*lists):
    return [(json.dumps(x),) for x in lists]


class TestTrackedSymbols(unittest.TestCase):
    def test_union_across_watchlists(self):
        c = _Conn(_syms(["A", "B"], ["B", "C"]))
        self.assertEqual(tracked_symbols(c, 1), {"A", "B", "C"})

    def test_case_and_whitespace_normalised(self):
        c = _Conn(_syms(["a", " btcusd ", "ETHUSD"]))
        self.assertEqual(tracked_symbols(c, 1), {"A", "BTCUSD", "ETHUSD"})

    def test_blank_entries_dropped(self):
        c = _Conn(_syms(["A", "", "  ", None]))
        self.assertEqual(tracked_symbols(c, 1), {"A"})

    def test_malformed_json_is_skipped_not_fatal(self):
        c = _Conn([("{not json",), (json.dumps(["B"]),)])
        self.assertEqual(tracked_symbols(c, 1), {"B"})

    def test_non_list_json_skipped(self):
        c = _Conn([(json.dumps({"A": 1}),)])
        self.assertEqual(tracked_symbols(c, 1), set())

    def test_db_failure_degrades_to_empty(self):
        """A missing table must not turn the budget check into a crash or,
        worse, into an unconditional allow."""
        class Boom(object):
            def execute(self, *a, **k):
                raise RuntimeError("no such table: watchlists")
        self.assertEqual(tracked_symbols(Boom(), 1), set())


class TestCheckSymbolBudget(unittest.TestCase):
    def _c(self, *lists):
        return _Conn(_syms(*lists))

    def test_under_limit_passes(self):
        ok, why = check_symbol_budget(self._c(), 1, "Free", ["A", "B", "C"])
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_exactly_at_limit_passes(self):
        ok, _ = check_symbol_budget(self._c(), 1, "Free", list("ABCDEFGHIJ"))
        self.assertTrue(ok, "10 symbols is the limit, not one over it")

    def test_one_over_limit_fails(self):
        ok, _ = check_symbol_budget(self._c(), 1, "Free", list("ABCDEFGHIJK"))
        self.assertFalse(ok)

    def test_counts_distinct_not_occurrences(self):
        """The same 10 symbols twice is still 10 symbols tracked."""
        ok, _ = check_symbol_budget(self._c(), 1, "Free", list("ABCDEFGHIJ"))
        self.assertTrue(ok)
        ok2, _ = check_symbol_budget(
            self._c(), 1, "Free", list("ABCDEFGHIJ") + list("ABCDEFGHIJ"))
        self.assertTrue(ok2)

    def test_duplicate_across_existing_lists_is_one(self):
        """Already holding A-J, re-adding A-J must not look like 20."""
        ok, _ = check_symbol_budget(self._c(list("ABCDEFGHIJ")), 1, "Free",
                                    list("ABCDEFGHIJ"))
        self.assertTrue(ok)

    def test_cumulative_across_watchlists(self):
        """The bypass this closes: hold 10 on one list, 10 more on another."""
        c = self._c(list("ABCDEFGHIJ"))
        ok, _ = check_symbol_budget(c, 1, "Free", list("KLMNOPQRST"))
        self.assertFalse(ok, "a second watchlist sidestepped the limit")

    def test_second_watchlist_within_budget_passes(self):
        c = self._c(list("ABCDE"))
        ok, _ = check_symbol_budget(c, 1, "Free", list("FGHIJ"))
        self.assertTrue(ok)

    def test_higher_tier_gets_a_bigger_budget(self):
        c = self._c(list("ABCDEFGHIJ"))
        ok, _ = check_symbol_budget(c, 1, "Pro", list("KLMNOPQRST"))
        self.assertTrue(ok, "Pro allows 50 and holds only 20")

    def test_free_refuses_what_pro_allows(self):
        c = self._c(list("ABCDEFGHIJ"))
        self.assertFalse(check_symbol_budget(c, 1, "Free",
                                             list("KLMNOPQRST"))[0])
        self.assertTrue(check_symbol_budget(c, 1, "Pro",
                                            list("KLMNOPQRST"))[0])

    def test_empty_request_always_passes(self):
        ok, _ = check_symbol_budget(self._c(list("ABCDEFGHIJ")), 1, "Free", [])
        self.assertTrue(ok)

    def test_none_request_passes(self):
        ok, _ = check_symbol_budget(self._c(), 1, "Free", None)
        self.assertTrue(ok)

    def test_reason_names_the_limit_and_tier(self):
        _, why = check_symbol_budget(self._c(), 1, "Free", list("ABCDEFGHIJK"))
        self.assertIn("Free", why)
        self.assertIn("10", why)
        self.assertIn("11", why)


class TestEnforcementIsWired(unittest.TestCase):
    """A helper nothing calls is a helper that does nothing."""

    def test_watchlist_create_calls_the_budget(self):
        with open(os.path.join(ROOT, "api", "routes.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("check_symbol_budget", src)

    def test_refusal_is_403(self):
        with open(os.path.join(ROOT, "api", "routes.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("return _err(h, why, 403", src)

    def test_entitlement_is_advertised(self):
        """RFC-001's acceptance: adding a tier is a DB row + map change.
        The advertised value and the enforced value must come from the same
        map, or /api/me/entitlements starts lying again."""
        with open(os.path.join(ROOT, "core", "tiers.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"max_symbols"', src)
        from core.tiers import get_entitlements as ge
        self.assertEqual(ge("Free")["max_symbols"], max_symbols("Free"))


class TestNoRegressionForExistingUsers(unittest.TestCase):
    """Users who already hold more than their new limit.

    Enforcement starts at the write boundary, so an over-limit user can
    still read everything they had. Creating a *new* watchlist is refused,
    which is the intended behaviour -- but it must not delete or truncate
    what they already have.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.conn = sqlite3.connect(os.path.join(self._tmp, "t.db"))
        self.conn.executescript("""
            CREATE TABLE watchlists (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              name TEXT NOT NULL DEFAULT 'default',
              symbols_json TEXT NOT NULL DEFAULT '[]'
            );
        """)
        self.conn.execute(
            "INSERT INTO watchlists (user_id, name, symbols_json) VALUES (1,'big',?)",
            (json.dumps(list("ABCDEFGHIJKLMNOP")),))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_existing_rows_are_untouched(self):
        rows = self.conn.execute("SELECT symbols_json FROM watchlists").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(json.loads(rows[0][0])), 16)

    def test_reads_still_see_everything(self):
        self.assertEqual(len(tracked_symbols(self.conn, 1)), 16)

    def test_a_new_watchlist_is_refused(self):
        ok, _ = check_symbol_budget(self.conn, 1, "Free", ["ZZZ"])
        self.assertFalse(ok)


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


class _FakeHandler(object):
    """Enough of http.server's handler for _read_json and _send_json."""

    def __init__(self, body):
        raw = json.dumps(body).encode()
        self.status = None
        self.command = "POST"
        self.path = "/api/watchlists"
        self.headers = {"Content-Type": "application/json",
                        "Content-Length": str(len(raw))}
        self.rfile = _Sink(raw)
        self.wfile = _Sink()

    def send_response(self, code):
        self.status = int(code)

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.text())


class TestWatchlistEndpointEnforcesIt(unittest.TestCase):
    """Drive POST /api/watchlists for real.

    The tests above prove the helper computes the right answer and that the
    string `check_symbol_budget` appears in routes.py. Neither proves the
    endpoint calls it -- and "the value is present in the source" is the one
    assertion this project has repeatedly caught itself relying on. So this
    builds a real connection, a real Router and a real handler, and sends
    actual requests.
    """

    def setUp(self):
        from api.routes import Router
        self._tmp = tempfile.mkdtemp()
        self.conn = sqlite3.connect(os.path.join(self._tmp, "t.db"))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE watchlists (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              name TEXT NOT NULL DEFAULT 'default',
              symbols_json TEXT NOT NULL DEFAULT '[]',
              created_at INTEGER NOT NULL DEFAULT 0
            );
        """)
        self.router = Router()
        self.tier = "Free"
        self.router._require_user = lambda conn, h: (
            {"id": 1, "email": "u@x.test", "tier": self.tier, "disabled": 0}, None)
        self.router._require_csrf = lambda conn, h, sess: True

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def post(self, symbols, name="wl"):
        h = _FakeHandler({"name": name, "symbols": symbols})
        self.router._wl_create(self.conn, h)
        return h

    def stored(self):
        return [json.loads(r[0]) for r in
                self.conn.execute("SELECT symbols_json FROM watchlists").fetchall()]

    def test_ten_symbols_are_accepted(self):
        h = self.post(list("ABCDEFGHIJ"))
        self.assertEqual(h.status, 201)
        self.assertEqual(len(self.stored()), 1)

    def test_eleven_symbols_are_refused(self):
        h = self.post(list("ABCDEFGHIJK"))
        self.assertEqual(h.status, 403)
        self.assertEqual(self.stored(), [], "the refused list was stored anyway")

    def test_refusal_names_the_limit(self):
        h = self.post(list("ABCDEFGHIJK"))
        self.assertIn("10", h.json()["error"])
        self.assertIn("Free", h.json()["error"])

    def test_refusal_reports_the_limit_numerically(self):
        h = self.post(list("ABCDEFGHIJK"))
        self.assertEqual(h.json().get("max_symbols"), 10)

    def test_a_second_list_cannot_bypass_the_first(self):
        self.assertEqual(self.post(list("ABCDEFGHIJ"), "one").status, 201)
        self.assertEqual(self.post(list("KLMNOPQRST"), "two").status, 403)
        self.assertEqual(len(self.stored()), 1)

    def test_repeating_the_same_symbols_still_passes(self):
        self.assertEqual(self.post(list("ABCDEFGHIJ"), "one").status, 201)
        self.assertEqual(self.post(list("ABCDEFGHIJ"), "two").status, 201)

    def test_promoting_the_tier_lifts_the_cap(self):
        self.post(list("ABCDEFGHIJ"), "one")
        self.tier = "Pro"
        self.assertEqual(self.post(list("KLMNOPQRST"), "two").status, 201)

    def test_empty_watchlist_is_fine(self):
        self.assertEqual(self.post([], "empty").status, 201)

    def test_lowercase_does_not_buy_extra_slots(self):
        """Case is normalised before counting, so 'btcusd' and 'BTCUSD'
        are one symbol and cannot be used to spend two slots."""
        mixed = ["btcusd", "BTCUSD", "ethusd", "ETHUSD", "solusd",
                 "SOLUSD", "xrpusd", "XRPUSD", "adausd", "ADAUSD", "dogeusd"]
        self.assertEqual(len(set(s.upper() for s in mixed)), 6)
        self.assertEqual(self.post(mixed, "dupes").status, 201)

    def test_eleven_distinct_symbols_are_refused_regardless_of_case(self):
        mixed = ["a", "B", "c", "D", "e", "F", "g", "H", "i", "J", "k"]
        self.assertEqual(self.post(mixed, "eleven").status, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)

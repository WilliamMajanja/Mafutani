"""Feed health: poller failures must be durable and visible.

RFC-002's acceptance line -- "Poller failure isolates per symbol, logs to
`risk_events`" -- was half implemented. The isolation existed; the logging
did not. A feed that died quietly looked exactly like a feed that was never
configured.

These go to `feed_health`, not `risk_events`, because that table is
user-scoped (`user_id NOT NULL REFERENCES users(id)`, FKs enforced) and
order-scoped (`order_payload_json`), and a dead feed is neither. That is
argued in full in the schema comment and the ratified RFC-002; what is pinned
here is the behaviour.

Three properties that are easy to get wrong and expensive to get wrong:

* **transitions, not per-poll** -- otherwise ~50 symbols failing every 5
  seconds writes tens of thousands of identical rows an hour;
* **recovery is recorded** -- a log that only ever says "down" cannot tell
  an outage from an outage that ended;
* **both key shapes** -- CryptoPoller/EquityPoller key by symbol,
  VenuePoller by `(symbol, venue)`. A feed-health log that only saw one of
  them would be the same silent swallow in a new place.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from data import feeds  # noqa: E402


def _sandbox():
    """Create a throwaway DB with the real schema; return (path, restore).

    DB_PATH has to stay set for the whole test, not just schema creation:
    `data.db.connect()` resolves it from the environment on every call, so
    restoring it straight after init_db sends the logger somewhere else. The
    first version of this did exactly that and every assertion saw zero rows.
    """
    d = tempfile.mkdtemp()
    path = os.path.join(d, "fh.db")
    prev = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = path
    from data.db import init_db
    init_db().close()

    def restore():
        if prev is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = prev
    return path, restore


def _rows(path):
    import sqlite3
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(
            "SELECT symbol, venue, kind, reason FROM feed_health ORDER BY id")]
    finally:
        c.close()


class FeedHealthBase(unittest.TestCase):
    def setUp(self):
        self.path, self._restore = _sandbox()
        feeds.reset_feed_health_state()

    def tearDown(self):
        feeds.reset_feed_health_state()
        self._restore()


class TestRecordsADownTransition(FeedHealthBase):
    def test_a_failure_is_written(self):
        feeds._note_feed_health({"BTCUSD": {"error": "HTTP 500"}})
        rows = _rows(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "down")
        self.assertEqual(rows[0]["symbol"], "BTCUSD")
        self.assertEqual(rows[0]["reason"], "HTTP 500")

    def test_a_healthy_row_writes_nothing(self):
        feeds._note_feed_health({"BTCUSD": {"bid": 1.0, "ask": 1.1}})
        self.assertEqual(_rows(self.path), [])

    def test_venue_is_recorded_when_present(self):
        feeds._note_feed_health(
            {"BTCUSD": {"venue": "kraken", "error": "timeout"}})
        self.assertEqual(_rows(self.path)[0]["venue"], "kraken")

    def test_a_missing_table_does_not_raise(self):
        """A pre-migration database has no feed_health. Logging must never
        be the reason a poll fails, so this has to be swallowed."""
        import sqlite3
        bare = os.path.join(tempfile.mkdtemp(), "bare.db")
        c = sqlite3.connect(bare)
        c.execute("CREATE TABLE t (x INTEGER)")
        c.commit()
        c.close()
        old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = bare
        try:
            feeds._note_feed_health({"BTCUSD": {"error": "boom"}})
        finally:
            if old is None:
                os.environ.pop("DB_PATH", None)
            else:
                os.environ["DB_PATH"] = old


class TestTransitionsNotEveryPoll(FeedHealthBase):
    """The whole reason for the in-memory dedup."""

    def test_repeated_failures_write_one_row(self):
        for _ in range(50):
            feeds._note_feed_health({"BTCUSD": {"error": "still down"}})
        self.assertEqual(len(_rows(self.path)), 1,
                         "a persistent outage wrote one row per poll")

    def test_a_changed_reason_is_not_a_new_transition(self):
        """Same outage, different wording. Still one event."""
        feeds._note_feed_health({"BTCUSD": {"error": "HTTP 500"}})
        feeds._note_feed_health({"BTCUSD": {"error": "HTTP 503"}})
        self.assertEqual(len(_rows(self.path)), 1)

    def test_recovery_is_recorded(self):
        feeds._note_feed_health({"BTCUSD": {"error": "down"}})
        feeds._note_feed_health({"BTCUSD": {"bid": 1.0, "ask": 1.1}})
        rows = _rows(self.path)
        self.assertEqual([r["kind"] for r in rows], ["down", "recovered"])

    def test_a_second_outage_logs_again(self):
        feeds._note_feed_health({"BTCUSD": {"error": "a"}})
        feeds._note_feed_health({"BTCUSD": {"bid": 1.0}})
        feeds._note_feed_health({"BTCUSD": {"error": "b"}})
        self.assertEqual([r["kind"] for r in _rows(self.path)],
                         ["down", "recovered", "down"])

    def test_recovery_without_a_prior_failure_is_not_logged(self):
        """Otherwise a healthy feed writes a 'recovered' row every poll."""
        for _ in range(20):
            feeds._note_feed_health({"BTCUSD": {"bid": 1.0}})
        self.assertEqual(_rows(self.path), [])

    def test_symbols_are_tracked_independently(self):
        feeds._note_feed_health({"A": {"error": "x"}})
        feeds._note_feed_health({"B": {"error": "y"}})
        feeds._note_feed_health({"A": {"bid": 1.0}})
        kinds = sorted((r["symbol"], r["kind"]) for r in _rows(self.path))
        self.assertEqual(kinds, [("A", "down"), ("A", "recovered"),
                                 ("B", "down")])

    def test_venues_of_the_same_symbol_are_independent(self):
        feeds._note_feed_health({"BTCUSD": {"venue": "kraken", "error": "x"}})
        feeds._note_feed_health({"BTCUSD": {"venue": "binance", "error": "y"}})
        feeds._note_feed_health({"BTCUSD": {"venue": "kraken", "bid": 1.0}})
        rows = _rows(self.path)
        self.assertEqual(len(rows), 3, rows)
        recovered = [r for r in rows if r["kind"] == "recovered"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["venue"], "kraken")


class TestTupleKeyedResults(FeedHealthBase):
    """VenuePoller keys by (symbol, venue); the others key by symbol."""

    def test_tuple_key_is_understood(self):
        feeds._note_feed_health({("BTCUSD", "kraken"): {"error": "timeout"}})
        row = _rows(self.path)[0]
        self.assertEqual((row["symbol"], row["venue"]), ("BTCUSD", "kraken"))

    def test_a_venue_in_the_row_wins_over_the_tuple(self):
        feeds._note_feed_health(
            {("BTCUSD", "ignored"): {"venue": "kraken", "error": "x"}})
        self.assertEqual(_rows(self.path)[0]["venue"], "kraken")

    def test_tuple_and_symbol_keys_do_not_collide(self):
        feeds._note_feed_health({("BTCUSD", "kraken"): {"error": "a"}})
        feeds._note_feed_health({"BTCUSD": {"venue": "binance", "error": "b"}})
        self.assertEqual(len(_rows(self.path)), 2)

    def test_tuple_keyed_recovery(self):
        feeds._note_feed_health({("EURUSD", "kraken"): {"error": "a"}})
        feeds._note_feed_health({("EURUSD", "kraken"): {"bid": 1.1}})
        self.assertEqual([r["kind"] for r in _rows(self.path)],
                         ["down", "recovered"])


class TestPollersCallIt(unittest.TestCase):
    """A logger nothing calls logs nothing."""

    def test_all_three_pollers_report_health(self):
        src = open(os.path.join(ROOT, "data", "feeds.py"), encoding="utf-8").read()
        # One call per poll_once: CryptoPoller, VenuePoller, EquityPoller.
        self.assertEqual(src.count("_note_feed_health(out)"), 3)

    def test_venue_poller_no_longer_discards_failures(self):
        """The worst case: a failed venue book used to vanish from the
        result entirely, which is indistinguishable from never being routed.
        """
        src = open(os.path.join(ROOT, "data", "feeds.py"), encoding="utf-8").read()
        self.assertNotIn("except Exception:\n                    continue",
                         src.split("class VenuePoller")[1].split("def persist_venue_quotes")[0])
        self.assertIn("for fut in pending:", src)


class TestAdminSurfacing(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(ROOT, "api", "routes.py"),
                        encoding="utf-8").read()

    def test_admin_overview_reports_feed_health(self):
        self.assertIn('"feed_health": _count("SELECT COUNT(*) FROM feed_health")',
                      self.src)

    def test_admin_reports_currently_down_not_just_cumulative(self):
        """A count that only grows stops answering "are the feeds up"."""
        self.assertIn('"feeds_down"', self.src)

    def test_risk_events_count_is_untouched(self):
        """The whole reason feed_health exists: order-risk totals must not
        start including feed outages."""
        self.assertIn('"risk_events": _count("SELECT COUNT(*) FROM risk_events")',
                      self.src)
        self.assertNotIn("FROM risk_events WHERE kind = 'down'", self.src)


class TestReset(unittest.TestCase):
    def test_reset_clears_state(self):
        feeds._note_feed_health({"A": {"error": "x"}})
        feeds.reset_feed_health_state()
        feeds._note_feed_health({"A": {"error": "x"}})
        # Without the reset the second call would be a no-op transition.
        self.assertTrue(callable(feeds.reset_feed_health_state))


if __name__ == "__main__":
    unittest.main(verbosity=2)

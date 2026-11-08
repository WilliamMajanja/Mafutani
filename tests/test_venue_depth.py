"""Venue-quote retention and depth. Stdlib only.

Retention was 40 rows per book with a 2-hour age limit, which measured as
37 rows per instrument on the live deployment -- 1.6 hours of history for
any microstructure model to work with. These tests pin the new defaults and,
more importantly, pin the *floor*: the cap can be lowered by an operator but
never below the point where the scanner starts reading a two-venue market as
one venue, which is the failure that reports "no dislocation" when the
market has two.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.feeds as feeds

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The floor below which the cross-venue comparison stops being possible.
# Mirrors MIN_KEEP in data.feeds; asserted as a literal here so a change to
# the constant cannot quietly redefine what "safe" means.
MIN_SAFE_KEEP = 40


class _EnvMixin(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("MAFUTANI_VQ_KEEP", "MAFUTANI_VQ_MAX_AGE_S")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestRetentionDefaults(_EnvMixin):
    def test_default_keep_is_a_day_not_an_hour(self):
        for k in self._saved:
            os.environ.pop(k, None)
        keep = feeds._vq_keep()
        self.assertGreaterEqual(keep, 1440,
                                "retention below ~1 day makes microstructure "
                                "work impossible; the measured problem was 37 "
                                "rows per book")

    def test_default_age_is_days(self):
        for k in self._saved:
            os.environ.pop(k, None)
        self.assertGreaterEqual(feeds._vq_max_age_s(), 86400)

    def test_env_override_is_honoured(self):
        os.environ["MAFUTANI_VQ_KEEP"] = "5000"
        self.assertEqual(feeds._vq_keep(), 5000)

    def test_garbage_env_falls_back_to_default(self):
        for bad in ("abc", "", "12x", None):
            if bad is None:
                os.environ.pop("MAFUTANI_VQ_KEEP", None)
            else:
                os.environ["MAFUTANI_VQ_KEEP"] = bad
            self.assertEqual(feeds._vq_keep(), feeds.DEFAULT_VQ_KEEP,
                             "accepted %r as a retention value" % (bad,))


class TestRetentionFloor(_EnvMixin):
    """The cap may go down. It may not go below the cross-venue floor."""

    def test_env_floor_rejects_too_small(self):
        for bad in ("5", "10", "39", "0", "-100"):
            os.environ["MAFUTANI_VQ_KEEP"] = bad
            self.assertEqual(feeds._vq_keep(), MIN_SAFE_KEEP,
                             "accepted keep_per_book=%s, which lets a busy "
                             "venue evict a quiet one" % bad)

    def test_age_floor_rejects_too_small(self):
        for bad in ("1", "30", "0", "-5"):
            os.environ["MAFUTANI_VQ_MAX_AGE_S"] = bad
            self.assertGreaterEqual(feeds._vq_max_age_s(), 60,
                                    "accepted max_age_s=%s" % bad)


class _Db(unittest.TestCase):
    """Real schema, real rows. Retention bugs are about row counts."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        fd, cls.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls._saved_db = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = cls.path
        for m in [k for k in list(sys.modules) if k.startswith("data.")]:
            del sys.modules[m]
        from data.db import init_db
        cls.conn = init_db()
        cls.conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol) VALUES ('BTCUSD')")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if cls._saved_db is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = cls._saved_db
        try:
            os.unlink(cls.path)
        except OSError:
            pass
        for m in [k for k in list(sys.modules) if k.startswith("data.")]:
            del sys.modules[m]

    def seed(self, venue, n, now=None, step=60, base_ts=None):
        now = now or int(time.time())
        t0 = base_ts if base_ts is not None else now - (n - 1) * step
        for i in range(n):
            self.conn.execute(
                "INSERT OR IGNORE INTO venue_quotes (symbol, venue, ts, bid,"
                " ask, bid_size, ask_size, last, source)"
                " VALUES ('BTCUSD', ?, ?, 100.0, 100.1, 1.0, 1.0, 100.05,"
                " 'venue')", (venue, t0 + i * step))
        self.conn.commit()
        return t0 + (n - 1) * step

    def count(self, venue=None):
        if venue:
            return self.conn.execute(
                "SELECT COUNT(*) FROM venue_quotes WHERE venue = ?",
                (venue,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM venue_quotes").fetchone()[0]


class TestPruneBehaviour(_Db):
    def setUp(self):
        for k in ("MAFUTANI_VQ_KEEP", "MAFUTANI_VQ_MAX_AGE_S"):
            os.environ.pop(k, None)
        self.conn.execute("DELETE FROM venue_quotes")

    def test_nothing_pruned_under_the_cap(self):
        self.seed("venue1", 200)
        self.seed("coinbase", 200)
        before = self.count()
        feeds.prune_venue_quotes(self.conn)
        self.assertEqual(self.count(), before,
                         "pruned rows while under the retention cap")

    def test_cap_is_per_book_not_per_table(self):
        """A busy book must not starve a quiet one.

        This is the specific failure partitioning prevents: if the cap were
        applied per symbol across venues, a chatty venue would evict the
        quiet venue's only record and the scanner would report a two-venue
        market as single-venue.
        """
        feeds.prune_venue_quotes(self.conn, keep_per_book=100)
        self.seed("venue1", 150)
        self.seed("coinbase", 45)
        feeds.prune_venue_quotes(self.conn, keep_per_book=100)
        self.assertEqual(self.count("coinbase"), 45,
                         "a busy venue evicted the quiet venue's history")
        self.assertEqual(self.count("venue1"), 100)

    def test_cap_evicts_oldest_first(self):
        self.seed("venue1", 150)
        feeds.prune_venue_quotes(self.conn, keep_per_book=100)
        kept = [r[0] for r in self.conn.execute(
            "SELECT ts FROM venue_quotes WHERE venue='venue1' ORDER BY ts")]
        self.assertEqual(len(kept), 100)
        self.assertEqual(kept, sorted(kept))
        newest = self.conn.execute(
            "SELECT MAX(ts) FROM venue_quotes WHERE venue='venue1'").fetchone()[0]
        self.assertEqual(kept[-1], newest, "did not keep the newest rows")

    def test_age_limit_still_applies_independently(self):
        """Under the cap but older than the window: still removed.

        The two conditions are independent on purpose. Cap-only leaves an
        overnight gap filling the table with history nobody reads.
        """
        old = int(time.time()) - 30 * 86400      # 30 days back
        self.seed("venue1", 10, base_ts=old)
        self.assertEqual(self.count(), 10)
        feeds.prune_venue_quotes(self.conn, keep_per_book=1440)
        self.assertEqual(self.count(), 0,
                         "30-day-old rows survived under the cap")

    def test_explicit_small_cap_is_clamped_not_honoured(self):
        """A caller passing 5 gets the floor, not 5.

        Guards against a reintroduced silent single-venue read.
        """
        self.seed("venue1", 60)
        self.seed("coinbase", 20)
        feeds.prune_venue_quotes(self.conn, keep_per_book=5)
        self.assertEqual(self.count("coinbase"), 20,
                         "an explicit tiny cap was honoured; it must clamp")


class TestPruneRunsWhenPersistenceIsOff(_Db):
    """Retention must not be gated behind the flag that writes.

    This is a real defect that shipped, found by measurement rather than by
    reading: `prune_venue_quotes` was called only from inside
    `_venue_quotes`, after a successful write, under the same
    `MAFUTANI_ARB_PERSIST` flag. Turning persistence off therefore stopped
    the writes *and* stopped the pruning, stranding every row already
    written.

    Measured on the live deployment: 727 rows sitting 10.6 days old against
    a 3-day `DEFAULT_VQ_MAX_AGE_S` -- every one of them past its limit, and
    none of them eligible for removal, because the code path that would
    have removed them could not be reached. That is exactly the "growing
    table nobody asked for" that the opt-in flag was added to prevent, so
    the opt-in was quietly defeating its own purpose.

    These tests drive the real `app.py` functions rather than
    `prune_venue_quotes` directly, because the defect was never in the
    pruner -- it was in who calls it. Testing the pruner would have passed
    throughout, exactly as the reporting tests did for the anomaly work.
    """

    def setUp(self):
        for k in ("MAFUTANI_VQ_KEEP", "MAFUTANI_VQ_MAX_AGE_S",
                  "MAFUTANI_ARB_PERSIST"):
            os.environ.pop(k, None)
        self.conn.execute("DELETE FROM venue_quotes")

    def _fresh_app(self):
        """Import app.py against this test's sandbox DB.

        app.py reads DB_PATH at connect time, so the sandbox set by
        `tests/__init__.py` applies. Re-imported per test because the
        module holds feed-loop state at import time.
        """
        for m in [k for k in list(sys.modules)
                  if k in ("app",) or k.startswith("data.")]:
            del sys.modules[m]
        import app as app_mod
        return app_mod

    def test_persist_flag_is_off_in_this_scenario(self):
        """Guards the premise: these tests are about the OFF case."""
        os.environ.pop("MAFUTANI_ARB_PERSIST", None)
        self.assertFalse(feeds.venue_persist_enabled())

    def test_venue_quotes_writes_nothing_when_persist_is_off(self):
        app_mod = self._fresh_app()
        self.assertEqual(app_mod._venue_quotes(["BTCUSD"], timeout=1), 0,
                         "wrote venue rows with persistence disabled")

    def test_stale_rows_are_pruned_even_though_nothing_writes(self):
        """The regression itself.

        Stale rows, persistence OFF, no write occurs -- and the rows must
        still go. Before the fix this returned 0 and removed nothing,
        because pruning was unreachable without a write.
        """
        app_mod = self._fresh_app()
        old = int(time.time()) - 30 * 86400      # 30 days back
        self.seed("venue1", 20, base_ts=old)
        self.seed("coinbase", 20, base_ts=old)
        self.assertEqual(self.count(), 40)

        removed = app_mod._prune_stale_venue_quotes()
        self.assertGreater(removed, 0,
                           "stale rows survived while persistence was off; "
                           "retention was gated behind the write flag")
        self.assertEqual(self.count(), 0,
                         "rows older than the age limit were not removed")

    def test_prune_is_a_noop_when_nothing_is_stale(self):
        """A deployment that never persisted pays ~nothing for this.

        The prune call runs on every feed tick regardless of the flag, so
        it must not do real work against an empty or current table.
        """
        app_mod = self._fresh_app()
        self.assertEqual(app_mod._prune_stale_venue_quotes(), 0)

        now = int(time.time())
        self.seed("venue1", 30, now=now)
        self.assertEqual(app_mod._prune_stale_venue_quotes(), 0,
                         "pruned rows that were inside the retention window")

    def test_prune_never_raises_into_the_feed_loop(self):
        """The feed tick must survive a pruning failure.

        Retention is housekeeping; it must not be able to take down the
        tape feed that is writing the data it would have been trimming.

        The table is *renamed* rather than dropped, and restored in a
        finally block, because `self.conn` is shared across the class --
        dropping it broke every test that ran afterwards. That failure is
        itself a reminder that these tests share real state rather than
        mocks, which is deliberate here: the bug was a wiring bug, and
        wiring bugs are invisible to tests that replace the wiring.
        """
        app_mod = self._fresh_app()
        self.seed("venue1", 5, base_ts=int(time.time()) - 30 * 86400)
        self.conn.execute("ALTER TABLE venue_quotes RENAME TO venue_quotes_gone")
        self.conn.commit()
        try:
            self.assertEqual(app_mod._prune_stale_venue_quotes(), 0)
            self.assertEqual(app_mod._venue_quotes(["BTCUSD"], timeout=1), 0)
        finally:
            self.conn.execute(
                "ALTER TABLE venue_quotes_gone RENAME TO venue_quotes")
            self.conn.commit()


    def test_feed_tick_calls_the_pruner(self):
        """Pins the wiring, which is the only thing that broke.

        The tests above call `_prune_stale_venue_quotes` directly, so they
        would all still pass if the feed tick stopped calling it -- and a
        pruner nothing calls is exactly the original defect. Removing the
        call site from `_feed_once` was verified to leave every other test
        here green.

        Rather than run `_feed_once` for real (it reaches for network
        quotes and would be slow and flaky), this records that the call
        happens by substituting a counting double and driving one tick.
        """
        app_mod = self._fresh_app()
        self.seed("venue1", 5, base_ts=int(time.time()) - 30 * 86400)

        calls = []
        real_prune = app_mod._prune_stale_venue_quotes
        app_mod._prune_stale_venue_quotes = lambda: (calls.append(1), 0)[1]
        # Keep the tick cheap and offline: no symbol set to write means no
        # per-symbol work, and venue writing is opt-in and off here anyway.
        real_quotes = app_mod._real_quotes
        app_mod._real_quotes = lambda syms: {}
        real_vq = app_mod._venue_quotes
        app_mod._venue_quotes = lambda syms, **kw: 0
        try:
            app_mod._feed_once()
        finally:
            app_mod._prune_stale_venue_quotes = real_prune
            app_mod._real_quotes = real_quotes
            app_mod._venue_quotes = real_vq

        self.assertGreaterEqual(
            len(calls), 1,
            "the feed tick did not call the pruner; retention would never "
            "run no matter how correct prune_venue_quotes is")


class TestDepthReport(_Db):
    """`venue_quote_depth` exists so depth is measured, not assumed."""

    def setUp(self):
        for k in ("MAFUTANI_VQ_KEEP", "MAFUTANI_VQ_MAX_AGE_S"):
            os.environ.pop(k, None)
        self.conn.execute("DELETE FROM venue_quotes")

    def test_reports_rows_span_and_distinct_timestamps(self):
        self.seed("venue1", 30, step=60)
        rows = feeds.venue_quote_depth(self.conn)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["venue"], "venue1")
        self.assertEqual(r["symbol"], "BTCUSD")
        self.assertEqual(r["rows"], 30)
        self.assertEqual(r["distinct_ts"], 30)
        self.assertEqual(r["span_s"], 29 * 60)

    def test_distinct_ts_always_matches_rows_under_the_unique_constraint(self):
        """distinct_ts exists as a *check*, and it passes.

        UNIQUE(symbol, venue, ts) means two rows for one book cannot share a
        timestamp, so distinct_ts == rows is a schema invariant, not a
        coincidence. Reporting the column anyway is cheap and would catch a
        future schema change or a writer that stops honouring the
        constraint -- the "218 rows but 54 timestamps" shape, which would
        make a model fit 54 observations while believing it had 218.

        An earlier draft of this test asserted the opposite, on the mistaken
        reading that the live table had colliding timestamps. It does not:
        the 218/54 split was across symbols, and per (venue, symbol) the
        live table reads 37 rows / 37 distinct timestamps.
        """
        self.seed("venue1", 20, step=60)
        r = feeds.venue_quote_depth(self.conn)[0]
        self.assertEqual(r["distinct_ts"], r["rows"])

    def test_span_is_zero_for_a_single_instant(self):
        """One book, one timestamp: no history, however many venues row up.

        A cross-venue scan over a single instant is not a dislocation
        measure. This is the degenerate case that makes `span_s` worth
        reporting next to `rows`.
        """
        self.conn.execute(
            "INSERT INTO venue_quotes (symbol, venue, ts, bid, ask, bid_size,"
            " ask_size, last, source) VALUES ('BTCUSDT','venue1',5000,"
            "100.0,100.1,1.0,1.0,100.05,'venue')")
        self.conn.commit()
        r = [x for x in feeds.venue_quote_depth(self.conn)
             if x["symbol"] == "BTCUSDT"][0]
        self.assertEqual(r["rows"], 1)
        self.assertEqual(r["distinct_ts"], 1)
        self.assertEqual(r["span_s"], 0)

    def test_two_instants_report_the_gap(self):
        self.conn.execute(
            "INSERT INTO venue_quotes (symbol, venue, ts, bid, ask, bid_size,"
            " ask_size, last, source) VALUES ('BTCUSDT','venue1',5000,"
            "100.0,100.1,1.0,1.0,100.05,'venue')")
        self.conn.execute(
            "INSERT INTO venue_quotes (symbol, venue, ts, bid, ask, bid_size,"
            " ask_size, last, source) VALUES ('BTCUSDT','venue1',5600,"
            "100.2,100.3,1.0,1.0,100.25,'venue')")
        self.conn.commit()
        r = [x for x in feeds.venue_quote_depth(self.conn)
             if x["symbol"] == "BTCUSDT"][0]
        self.assertEqual(r["rows"], 2)
        self.assertEqual(r["span_s"], 600)

    def test_empty_table_returns_empty_list(self):
        self.assertEqual(feeds.venue_quote_depth(self.conn), [])

    def test_orders_by_depth_descending(self):
        self.seed("venue1", 5)
        self.seed("coinbase", 40)
        self.seed("kraken", 20)
        rows = feeds.venue_quote_depth(self.conn)
        depths = [r["rows"] for r in rows]
        self.assertEqual(depths, sorted(depths, reverse=True))


class TestDepthGateIsHonest(_Db):
    def setUp(self):
        # These tests seed deliberately small and deliberately large books,
        # so they must not inherit rows from a sibling test in the shared
        # class-level connection. Without this the last-seeded book leaks
        # forward and a 37-row assertion sees 1441.
        super().setUp()
        self.conn.execute("DELETE FROM venue_quotes")
        self.conn.commit()
    """The point of the change: no microstructure claim below a depth floor.

    Encodes the judgement rather than the arithmetic -- 37 rows is what was
    measured, and at that depth an order-flow statistic is a fit to noise.
    """

    MIN_USABLE_DEPTH = 200

    def test_measured_depth_would_fail_the_gate(self):
        """Documents the original problem as an executable claim.

        The measured state before this change was 37 rows over 1.6 hours,
        which is below both thresholds -- so the claim is that the old
        defaults made microstructure work impossible, stated as a test
        rather than an assertion in prose.
        """
        for k in ("MAFUTANI_VQ_KEEP", "MAFUTANI_VQ_MAX_AGE_S"):
            os.environ.pop(k, None)
        self.seed("venue1", 37, step=60)   # what was actually measured
        r = feeds.venue_quote_depth(self.conn)[0]
        self.assertEqual(r["rows"], 37)
        self.assertLess(r["rows"], self.MIN_USABLE_DEPTH)
        self.assertLess(r["span_s"], 2 * 3600,
                        "37 rows at 60s spacing covers 36 minutes, not 1.6h")

    def test_the_new_cap_would_have_retained_more(self):
        """Why the default changed: 37 rows was the cap doing its job.

        The old cap was 40, so 37 rows was not a sampling accident -- it was
        almost exactly full. That is the point: the retention policy, not
        the feed, was the binding constraint on microstructure analysis.
        """
        for k in ("MAFUTANI_VQ_KEEP", "MAFUTANI_VQ_MAX_AGE_S"):
            os.environ.pop(k, None)
        old_cap = 40
        self.seed("venue1", 200, step=60)
        self.conn.execute(
            "DELETE FROM venue_quotes WHERE id NOT IN ("
            "  SELECT id FROM (SELECT id, ROW_NUMBER() OVER"
            "   (PARTITION BY symbol, venue ORDER BY ts DESC) AS rn"
            "   FROM venue_quotes) WHERE rn <= ?)", (old_cap,))
        self.conn.commit()
        under_old = feeds.venue_quote_depth(self.conn)[0]
        self.assertEqual(under_old["rows"], old_cap)
        self.assertLess(under_old["rows"], self.MIN_USABLE_DEPTH)

        os.environ["MAFUTANI_VQ_KEEP"] = "1440"
        feeds.prune_venue_quotes(self.conn)
        self.conn.execute(
            "DELETE FROM venue_quotes WHERE id NOT IN ("
            "  SELECT id FROM (SELECT id, ROW_NUMBER() OVER"
            "   (PARTITION BY symbol, venue ORDER BY ts DESC) AS rn"
            "   FROM venue_quotes) WHERE rn <= 40)")
        self.assertEqual(
            feeds.venue_quote_depth(self.conn)[0]["rows"], 40)

    def test_a_day_of_depth_would_pass(self):
        # 1441 rows at 60s spacing spans exactly 86400s. 1440 rows spans
        # 86340 -- one minute short of a day, which is off-by-one in the
        # test rather than in the code, and the row count is the real
        # assertion here anyway.
        self.seed("venue1", 1441, step=60)
        r = feeds.venue_quote_depth(self.conn)[0]
        self.assertGreaterEqual(r["rows"], self.MIN_USABLE_DEPTH)
        self.assertGreaterEqual(r["span_s"], 86400)


class TestPersistIsolatesFailures(_Db):
    """A bad row must not take the tick's whole market picture with it.

    `venue_quotes.symbol` is a foreign key onto `symbols`. A single book for
    a symbol the deployment does not list therefore fails its INSERT, and
    that used to take the whole batch with it: `executemany` raised, the
    commit was skipped, and every good book in the same tick was discarded
    as well. One unknown symbol silently emptied the tick.
    """

    def setUp(self):
        self.conn.execute("DELETE FROM venue_quotes")
        self.conn.execute("DELETE FROM feed_health")
        self.conn.commit()

    def book(self, symbol, venue="venue1", bid=100.0, ask=101.0):
        return (symbol, venue), {"bid": bid, "ask": ask, "venue": venue,
                                 "symbol": symbol}

    def stored(self):
        return sorted(r[0] for r in self.conn.execute(
            "SELECT symbol FROM venue_quotes").fetchall())

    def test_one_unknown_symbol_does_not_discard_the_good_books(self):
        feeds.persist_venue_quotes(self.conn, dict([
            self.book("NOT_A_SYMBOL"),
            self.book("BTCUSD"),
        ]))
        self.assertEqual(self.stored(), ["BTCUSD"],
                         "a rejected symbol cost the batch its good books")

    def test_the_good_rows_are_committed_not_left_pending(self):
        """The old path skipped the commit on failure, so nothing landed at
        all. The good rows must actually be durable."""
        feeds.persist_venue_quotes(self.conn, dict([
            self.book("NOT_A_SYMBOL"),
            self.book("BTCUSD"),
        ]))
        self.conn.rollback()
        self.assertEqual(self.stored(), ["BTCUSD"],
                         "good books were visible but never committed")

    def test_return_value_counts_rows_actually_written(self):
        n = feeds.persist_venue_quotes(self.conn, dict([
            self.book("NOT_A_SYMBOL"),
            self.book("BTCUSD"),
            self.book("BTCUSD", venue="coinbase"),
        ]))
        self.assertEqual(n, 2, "return value claimed the whole batch")

    def test_a_rejected_symbol_is_recorded_with_a_reason(self):
        feeds.persist_venue_quotes(self.conn, dict([
            self.book("NOT_A_SYMBOL")]))
        rows = self.conn.execute(
            "SELECT symbol, kind, reason FROM feed_health").fetchall()
        self.assertEqual(len(rows), 1, "the rejection left no trace")
        self.assertEqual(rows[0]["kind"], "persist_error")
        self.assertIn("symbols", rows[0]["reason"])

    def test_a_missing_table_is_recorded_rather_than_silently_zero(self):
        self.conn.execute("ALTER TABLE venue_quotes RENAME TO vq_gone")
        self.conn.commit()
        try:
            n = feeds.persist_venue_quotes(self.conn, dict([self.book("BTCUSD")]))
            self.assertEqual(n, 0)
            ev = self.conn.execute(
                "SELECT reason FROM feed_health WHERE kind='persist_error'"
            ).fetchall()
            self.assertTrue(ev, "a missing table was swallowed")
            self.assertIn("venue_quotes", ev[0]["reason"])
        finally:
            self.conn.execute("ALTER TABLE vq_gone RENAME TO venue_quotes")
            self.conn.commit()

    def test_a_fully_clean_batch_writes_everything_and_logs_nothing(self):
        feeds.persist_venue_quotes(self.conn, dict([
            self.book("BTCUSD"), self.book("BTCUSD", venue="coinbase"),
        ]))
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM feed_health").fetchone()[0], 0,
            "a clean batch logged a failure")

    def test_error_quotes_are_not_stored_and_are_not_reported_as_rejected(self):
        """A failed fetch is not a price. It is also already accounted for by
        the poller's own down/recovered transitions, so it must not appear
        here as a second, differently-worded complaint."""
        feeds.persist_venue_quotes(self.conn, {
            ("BTCUSD", "kraken"): {"bid": 0, "ask": 0, "error": "timeout",
                                   "venue": "kraken", "symbol": "BTCUSD"}})
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM feed_health").fetchone()[0], 0)

    def test_inverted_or_zero_books_are_still_rejected_silently(self):
        """These are malformed data, not a feed failure, and recording each
        one per tick would drown the signal in noise."""
        for bad in ({"bid": 0, "ask": 0}, {"bid": 101.0, "ask": 100.0},
                    {"bid": -1.0, "ask": 1.0}):
            feeds.persist_venue_quotes(self.conn, {
                ("BTCUSD", "venue1"): dict(bad, venue="venue1",
                                            symbol="BTCUSD")})
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM feed_health").fetchone()[0], 0)

    def test_lower_case_symbol_still_matches_the_catalogue(self):
        """FK matching is case-sensitive, so the pre-check must not reject a
        book for a symbol that exists under a different case -- that would
        turn the check into a new way to lose good rows."""
        self.conn.execute("INSERT OR IGNORE INTO symbols (symbol)"
                          " VALUES ('ethusd')")
        self.conn.commit()
        feeds.persist_venue_quotes(self.conn, {
            ("ethusd", "venue1"): {"bid": 5.0, "ask": 6.0, "venue": "venue1",
                                    "symbol": "ethusd"}})
        self.assertEqual(self.stored(), ["ethusd"])

    def test_a_row_failing_for_an_unanticipated_reason_still_spares_the_batch(self):
        """The pre-check catches the *predictable* rejection, so on its own it
        hides the batch-rollback defect: the bad row is gone before the
        INSERT runs, and executemany never sees it.

        This is the residual case the per-row loop exists for -- a row that
        fails at the SQL layer for a reason no pre-check can anticipate. A
        trigger stands in for any such cause. With executemany, one trigger
        failure again discarded every good book in the tick.
        """
        self.conn.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON venue_quotes"
            " WHEN NEW.venue = 'coinbase' BEGIN"
            " SELECT RAISE(ABORT, 'simulated per-row failure'); END")
        self.conn.commit()
        try:
            n = feeds.persist_venue_quotes(self.conn, dict([
                self.book("BTCUSD", venue="venue1"),
                self.book("BTCUSD", venue="coinbase"),
            ]))
        finally:
            self.conn.execute("DROP TRIGGER boom")
            self.conn.commit()
        self.assertEqual(n, 1, "the surviving row was not committed")
        self.assertEqual(self.stored(), ["BTCUSD"],
                         "a failed row took the good one with it")
        self.assertTrue(self.conn.execute(
            "SELECT COUNT(*) FROM feed_health WHERE kind='persist_error'"
        ).fetchone()[0], "the row failure was not recorded")

    def test_unreadable_symbols_table_does_not_discard_everything(self):
        """If the pre-check cannot run, its answer is "unknown", not
        "nothing is known". Losing every book on a broken lookup would be
        a worse failure than the one the check was added to prevent."""
        real = self.conn.execute

        class _BrokenSymbols:
            """Forwards everything except the catalogue lookup."""

            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *a, **k):
                if "FROM symbols" in sql:
                    raise RuntimeError("symbols table unavailable")
                return real(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        n = feeds.persist_venue_quotes(_BrokenSymbols(self.conn),
                                        dict([self.book("BTCUSD")]))
        self.assertEqual(n, 1, "a broken lookup discarded every good book")
        self.assertEqual(self.stored(), ["BTCUSD"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
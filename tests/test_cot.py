"""CFTC Commitments of Traders: parsing, identities, analytics, storage.

Stdlib only, no network. Every fixture is either hand-built to the exact
column layout the CFTC publishes or lifted verbatim from a real weekly
file, so a column shift is caught rather than absorbed.

The tests are weighted toward the three ways this data lies:

  1. **Layout drift.** The layouts are pinned by column index, not read
     from the file's header, because the weekly files have no header and
     the annual ones rename columns between families. A shifted index
     would silently write the swap dealer's short into the managed money
     column and every downstream number would be plausible. So a
     deliberately broken layout must *raise*, and the two CFTC accounting
     identities must hold on every parsed row.

  2. **False precision.** One observation has no percentile, a flat window
     has no z-score. Both must be None, never 0.0 -- a zero reads as "the
     bottom of the range" and would be a confident claim about a market
     where one thing is known.

  3. **Being read as current.** COT is a weekly count of positions,
     published three days after the Tuesday it describes. Every read
     carries the report date and its age.
"""

import os
import sqlite3
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from data import cot as C          # noqa: E402
from data import cotstore as S     # noqa: E402
from engines import cot as E       # noqa: E402


# --------------------------------------------------------------- fixtures
#
# Real rows, copied from the CFTC weekly files, padded to the family's
# column count. Field order:
#   0 market, 1 as_of YYMMDD, 2 report_date, 3 contract, 4 market_code,
#   5 region, 6 commodity, 7 open interest, then long/short/spread triples.

# "WHEAT-HRW - CHICAGO BOARD OF TRADE",260929,2026-09-29,001612,CBT ,00,001
_WHEAT = ["WHEAT-HRW - CHICAGO BOARD OF TRADE", "260929", "2026-09-29",
          "001612", "CBT ", "00", "001 ",
          "306523",  # 7 open interest
          "18373", "117250",        # prod_merc long/short (no spread col)
          "81210", "2608", "9344",   # swap
          "71011", "40334", "77946",  # managed money
          "10630", "20343", "17192",  # other reportable
          "285706", "285017",       # total reportable
          "20817", "21506"]         # non-reportable

# "GOLD - COMMODITY EXCHANGE INC.",260929,2026-09-29,088691,CMX ,00,061
_GOLD_LEGACY = ["GOLD - COMMODITY EXCHANGE INC.", "260929", "2026-09-29",
                "088691", "CMX ", "00", "061",
                "406456",
                "249736", "31104", "49105",   # non-commercial
                "85476", "87951",             # commercial (no spread col)
                "335212", "119055",
                "25392", "24531"]

# "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",260929,2026-09-29,13881A,CME ,00,138
_ESPY_TFF = ["E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE", "260929",
             "2026-09-29", "13881A", "CME ", "00", "138",
             "1543200",
             "12043", "22140", "3100",      # dealer
             "881204", "12044", "6100",     # asset manager
             "390115", "498877", "20250",   # leveraged money
             "15432", "9210", "4310",       # other reportable
             "1301794", "551271",
             "241406", "261929"]


def row(base, date=None, oi=None):
    """Copy a fixture with an overridden date / open interest."""
    r = list(base)
    if date:
        r[2] = date
    if oi is not None:
        r[7] = str(oi)
    return r


def build(family_cols, rows, report):
    """Pad rows to the family's real width and join as a CSV document."""
    width = C.LAYOUTS[C.REPORTS[report]["family"]]["oi"] + 200
    out = []
    for r in rows:
        out.append(",".join(str(x) for x in (list(r) + ["0"] *
                                             (width - len(r)))))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ parse
class TestParseDisaggregated(unittest.TestCase):

    def setUp(self):
        self.rows = C.parse(build(None, [_WHEAT], "disagg_comb"),
                            "disagg_comb")

    def test_six_categories(self):
        cats = sorted({r["category"] for r in self.rows})
        self.assertEqual(cats, ["m_money", "nonrept", "other_rept",
                                "prod_merc", "swap", "total_rept"])

    def test_identity_fields(self):
        r = self.rows[0]
        self.assertEqual(r["market"], "WHEAT-HRW - CHICAGO BOARD OF TRADE")
        self.assertEqual(r["report_date"], "2026-09-29")
        self.assertEqual(r["as_of_yymmdd"], "260929")
        self.assertEqual(r["contract_code"], "001612")
        self.assertEqual(r["market_code"], "CBT")
        self.assertEqual(r["open_interest"], 306523)
        self.assertEqual(r["family"], "disagg")
        self.assertEqual(r["combined"], 1)

    def test_managed_money_lands_in_managed_money(self):
        """The load-bearing test. Managed money long is the 13th field;
        if anything shifts, this is the number that silently absorbs it."""
        mm = [r for r in self.rows if r["category"] == "m_money"][0]
        self.assertEqual(mm["long_pos"], 71011)
        self.assertEqual(mm["short_pos"], 40334)
        self.assertEqual(mm["spread_pos"], 77946)

    def test_swap_lands_in_swap(self):
        sw = [r for r in self.rows if r["category"] == "swap"][0]
        self.assertEqual((sw["long_pos"], sw["short_pos"], sw["spread_pos"]),
                         (81210, 2608, 9344))

    def test_producer_merchant_has_no_spread_column(self):
        """It genuinely does not exist in this report. Reporting 0 as if
        it were measured would be inventing a number."""
        pm = [r for r in self.rows if r["category"] == "prod_merc"][0]
        self.assertEqual(pm["spread_pos"], 0)
        self.assertEqual(pm["has_spread"], 0)

    def test_cftc_identity_total_reportable(self):
        """CFTC: total reportable = sum of the four categories + spread.

        Spreading has to be added to *both* sides -- the CFTC says so
        explicitly in the report's own FAQ, and it is the single easiest
        thing to get wrong when reconstructing the total. This test
        passes only when every index is right.
        """
        g = {r["category"]: r for r in self.rows}
        cats = ("prod_merc", "swap", "m_money", "other_rept")
        spread = sum(g[c]["spread_pos"] for c in cats)
        lng = sum(g[c]["long_pos"] for c in cats) + spread
        sht = sum(g[c]["short_pos"] for c in cats) + spread
        self.assertEqual(lng, g["total_rept"]["long_pos"])
        self.assertEqual(sht, g["total_rept"]["short_pos"])

    def test_cftc_identity_open_interest(self):
        """CFTC: open interest = total reportable + non-reportable."""
        g = {r["category"]: r for r in self.rows}
        self.assertEqual(
            g["total_rept"]["long_pos"] + g["nonrept"]["long_pos"],
            g["total_rept"]["open_interest"])

    def test_omitting_spread_breaks_the_identity(self):
        """Why the previous test needs the spread. Guards against someone
        'simplifying' the sum and not noticing the total no longer ties."""
        g = {r["category"]: r for r in self.rows}
        cats = ("prod_merc", "swap", "m_money", "other_rept")
        lng = sum(g[c]["long_pos"] for c in cats)
        self.assertNotEqual(lng, g["total_rept"]["long_pos"])


class TestParseLegacyAndTff(unittest.TestCase):

    def test_legacy_categories(self):
        rows = C.parse(build(None, [_GOLD_LEGACY], "legacy_fut"), "legacy_fut")
        self.assertEqual(sorted({r["category"] for r in rows}),
                         ["comm", "noncomm", "nonrept", "total_rept"])
        nc = [r for r in rows if r["category"] == "noncomm"][0]
        self.assertEqual((nc["long_pos"], nc["short_pos"], nc["spread_pos"]),
                         (249736, 31104, 49105))
        cm = [r for r in rows if r["category"] == "comm"][0]
        self.assertEqual((cm["long_pos"], cm["short_pos"]), (85476, 87951))
        self.assertEqual(cm["has_spread"], 0)

    def test_tff_categories(self):
        rows = C.parse(build(None, [_ESPY_TFF], "tff_comb"), "tff_comb")
        self.assertEqual(sorted({r["category"] for r in rows}),
                         ["asset_mgr", "dealer", "lev_money", "nonrept",
                          "other_rept", "total_rept"])
        lm = [r for r in rows if r["category"] == "lev_money"][0]
        self.assertEqual((lm["long_pos"], lm["short_pos"], lm["spread_pos"]),
                         (390115, 498877, 20250))
        am = [r for r in rows if r["category"] == "asset_mgr"][0]
        self.assertEqual((am["long_pos"], am["short_pos"]), (881204, 12044))

    def test_families_do_not_collide_on_trader_categories(self):
        """The *distinct* classifications are what must never be merged.

        `other_rept` is deliberately not in this set: both the
        disaggregated and TFF reports carry an "Other Reportable" bucket
        and mean the same thing by it -- reportable traders outside the
        report's named groups. Renaming it per family would have been the
        wrong fix; sharing it is correct. `total_rept` and `nonrept` are
        arithmetic totals with one meaning everywhere.

        What must stay apart is the classification that actually differs:
        legacy's commercial/non-commercial versus the disaggregated's four
        groups, and TFF's four financial groups.
        """
        trader = {"m_money", "prod_merc", "swap", "dealer", "asset_mgr",
                  "lev_money", "noncomm", "comm", "cit", "noncomm_no_cit",
                  "comm_no_cit"}
        legacy = {r["category"] for r in
                  C.parse(build(None, [_GOLD_LEGACY], "legacy_fut"),
                          "legacy_fut")} & trader
        disagg = {r["category"] for r in
                  C.parse(build(None, [_WHEAT], "disagg_comb"),
                          "disagg_comb")} & trader
        tff = {r["category"] for r in
               C.parse(build(None, [_ESPY_TFF], "tff_comb"), "tff_comb")} & trader
        self.assertFalse(legacy & disagg,
                         "legacy and disagg share trader categories: %s"
                         % (legacy & disagg,))
        self.assertFalse(tff & disagg,
                         "tff and disagg share trader categories: %s"
                         % (tff & disagg,))
        # ...and each family did produce its own.
        self.assertIn("noncomm", legacy)
        self.assertIn("m_money", disagg)
        self.assertIn("lev_money", tff)

    def test_shared_bucket_keeps_one_name(self):
        """'Other Reportable' means the same thing in disagg and TFF, so
        one canonical name -- not a per-family alias."""
        a = {r["category"] for r in
             C.parse(build(None, [_WHEAT], "disagg_comb"), "disagg_comb")}
        b = {r["category"] for r in
             C.parse(build(None, [_ESPY_TFF], "tff_comb"), "tff_comb")}
        self.assertIn("other_rept", a & b)


class TestParseRefuses(unittest.TestCase):

    def test_shifted_layout_raises_not_silently_misreads(self):
        """A layout the module does not recognise must refuse. Producing
        plausible numbers from a shifted column is the failure mode this
        whole design is built to avoid."""
        broken = list(_WHEAT)
        broken[2] = "20260929"          # date moved / reformatted
        with self.assertRaises(C.CotError):
            C.parse(build(None, [broken], "disagg_comb"), "disagg_comb")

    def test_error_message_names_the_report(self):
        broken = list(_WHEAT)
        broken[2] = "garbage"
        try:
            C.parse(build(None, [broken], "disagg_comb"), "disagg_comb")
            self.fail("expected CotError")
        except C.CotError as e:
            self.assertIn("disagg_comb", str(e))

    def test_empty_document(self):
        with self.assertRaises(C.CotError):
            C.parse("", "disagg_comb")

    def test_unknown_report(self):
        with self.assertRaises(C.CotError):
            C.parse("a,b,c\n", "no_such_report")

    def test_odd_contract_code_skips_the_row_not_the_file(self):
        """One unparseable code must cost one row, not the whole year --
        which is what a real 2018 'ZB9105' would have done before."""
        odd = list(_WHEAT)
        odd[3] = "N/A"
        stats = {}
        rows = C.parse(build(None, [odd, _WHEAT], "disagg_comb"),
                       "disagg_comb", stats=stats)
        # One bad *row* costs one row, and that row carried six categories.
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["kept"], 6)
        self.assertEqual(len(rows), 6)

    def test_alphanumeric_contract_code_is_kept(self):
        """ZB9105 is real data, not a malformed row. The guard is isalnum()
        precisely so it survives."""
        r = list(_WHEAT)
        r[3] = "ZB9105"
        stats = {}
        rows = C.parse(build(None, [r], "disagg_comb"), "disagg_comb",
                       stats=stats)
        self.assertEqual(len(rows), 6)
        self.assertEqual(stats["skipped"], 0)

    def test_short_lines_are_skipped(self):
        rows = C.parse('"X",1,2,3\n' + build(None, [_WHEAT], "disagg_comb"),
                       "disagg_comb")
        self.assertEqual(len(rows), 6)

    def test_header_row_is_detected_not_parsed_as_data(self):
        """Annual archives carry a header; weekly files do not. Detected
        from content, so one parse serves both."""
        doc = (",".join('"Col_%d"' % i for i in range(230)) + "\n"
               + build(None, [_WHEAT], "disagg_comb"))
        rows = C.parse(doc, "disagg_comb")
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["contract_code"], "001612")


# ---------------------------------------------------------------- storage
def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(WORKDIR, "data", "schema.sql"),
              encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


class TestStore(unittest.TestCase):

    def setUp(self):
        self.conn = fresh_db()
        self.rows = C.parse(build(None, [_WHEAT], "disagg_comb"),
                            "disagg_comb")

    def tearDown(self):
        self.conn.close()

    def test_store_and_read_back(self):
        res = S.store(self.conn, self.rows, report="disagg_comb")
        self.assertTrue(res["ok"])
        self.assertEqual(res["written"], 6)
        self.assertEqual(res["report_date"], "2026-09-29")
        back = S.positions(self.conn, "disagg_comb",
                           contract_code="001612")
        self.assertEqual(len(back), 6)
        self.assertEqual(back[0]["market"],
                         "WHEAT-HRW - CHICAGO BOARD OF TRADE")

    def test_reingesting_is_idempotent(self):
        """Weekly pulls re-download the same Tuesday's file. Without an
        upsert the counts double every Friday."""
        S.store(self.conn, self.rows, report="disagg_comb")
        S.store(self.conn, self.rows, report="disagg_comb")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM cot_positions"
                              ).fetchone()["n"], 6)

    def test_reingest_updates_rather_than_duplicates(self):
        changed = C.parse(build(None, [row(_WHEAT, oi=999999)],
                               "disagg_comb"), "disagg_comb")
        S.store(self.conn, self.rows, report="disagg_comb")
        S.store(self.conn, changed, report="disagg_comb")
        r = self.conn.execute("SELECT open_interest o FROM cot_positions"
                              " WHERE category='m_money'").fetchone()
        self.assertEqual(r["o"], 999999)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM cot_positions"
                              ).fetchone()["n"], 6)

    def test_empty_store_is_a_failure_not_a_success(self):
        """A 200 carrying an unparseable body is not data. Recording it as
        a successful sync would overwrite 'last good' with nothing."""
        res = S.store(self.conn, [], report="disagg_comb")
        self.assertFalse(res["ok"])
        self.assertEqual(res["written"], 0)

    def test_failed_store_keeps_existing_rows(self):
        """The rule that matters: a failed fetch must not damage history."""
        S.store(self.conn, self.rows, report="disagg_comb")
        S.store(self.conn, [], report="disagg_comb", error="network down")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM cot_positions"
                              ).fetchone()["n"], 6)

    def test_fetches_are_recorded_for_provenance(self):
        S.store(self.conn, self.rows, report="disagg_comb", kind="weekly",
                url="https://example")
        S.store(self.conn, [], report="disagg_comb", error="timeout")
        h = S.fetch_history(self.conn, "disagg_comb")
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["ok"], 0)
        self.assertEqual(h[0]["error"], "timeout")
        self.assertTrue(S.last_success(self.conn, "disagg_comb"))

    def test_latest_report_date(self):
        S.store(self.conn, self.rows, report="disagg_comb")
        S.store(self.conn, C.parse(build(None, [row(_WHEAT, date="2026-09-22")],
                                        "disagg_comb"), "disagg_comb"),
                report="disagg_comb")
        self.assertEqual(S.latest_report_date(self.conn, "disagg_comb"),
                         "2026-09-29")

    def test_contract_code_is_not_assumed_numeric(self):
        r = list(_WHEAT)
        r[3] = "ZB9105"
        rows = C.parse(build(None, [r], "disagg_comb"), "disagg_comb")
        res = S.store(self.conn, rows, report="disagg_comb")
        self.assertTrue(res["ok"])
        got = S.contract_meta(self.conn, "ZB9105", "disagg_comb")
        self.assertIsNotNone(got)

    def test_search_finds_by_market_and_code(self):
        S.store(self.conn, self.rows, report="disagg_comb")
        self.assertEqual(len(S.search_markets(self.conn, "wheat",
                                              "disagg_comb")), 1)
        self.assertEqual(len(S.search_markets(self.conn, "001612",
                                              "disagg_comb")), 1)
        self.assertEqual(len(S.search_markets(self.conn, "gold",
                                              "disagg_comb")), 0)

    def test_empty_store_answers_empty_not_error(self):
        self.assertEqual(S.table_summary(self.conn), [])
        self.assertEqual(S.list_markets(self.conn, "disagg_comb"), [])
        self.assertIsNone(S.latest_report_date(self.conn, "disagg_comb"))
        self.assertIsNone(S.last_success(self.conn, "disagg_comb"))


# -------------------------------------------------------------- analytics
def series(values, start_year=2020):
    """Fake weekly series: [(date, net), ...] ascending."""
    import datetime
    out = []
    d = datetime.date(start_year, 1, 7)
    for i, v in enumerate(values):
        out.append((d.isoformat(), v))
        d += datetime.timedelta(days=7)
    return out


class TestNetAndBasics(unittest.TestCase):

    def test_net(self):
        self.assertEqual(E.net_position(100, 40), 60)
        self.assertEqual(E.net_position(40, 100), -60)

    def test_net_refuses_missing_sides(self):
        self.assertIsNone(E.net_position(None, 10))
        self.assertIsNone(E.net_position(10, None))

    def test_pct_of_open_interest(self):
        self.assertAlmostEqual(E.pct_of_open_interest(60, 40, 1000), 2.0)

    def test_pct_of_open_interest_refuses_zero_oi(self):
        self.assertIsNone(E.pct_of_open_interest(60, 40, 0))
        self.assertIsNone(E.pct_of_open_interest(60, 40, None))

    def test_cot_ratio(self):
        self.assertAlmostEqual(E.cot_ratio(3, 1), 3.0)

    def test_cot_ratio_refuses_zero_side(self):
        """A ratio against zero is an infinity, and an infinite ratio is
        not a number anyone should trade on."""
        self.assertIsNone(E.cot_ratio(0, 100))
        self.assertIsNone(E.cot_ratio(100, 0))

    def test_weekly_change(self):
        self.assertEqual(E.weekly_change(series([10, 25])), 15)

    def test_weekly_change_needs_a_prior_week(self):
        self.assertIsNone(E.weekly_change(series([10])))


class TestRefusalsOnThinData(unittest.TestCase):

    """The false-precision guards. Each of these could return 0.0 and
    produce a confident wrong answer; none may."""

    def test_one_observation_has_no_percentile(self):
        self.assertIsNone(E.percentile(series([500])))

    def test_one_observation_has_no_index(self):
        self.assertIsNone(E.cot_index(series([500])))

    def test_one_observation_has_no_zscore(self):
        self.assertIsNone(E.zscore(series([500])))

    def test_empty_series(self):
        self.assertIsNone(E.percentile([]))
        self.assertIsNone(E.cot_index([]))
        self.assertIsNone(E.zscore([]))

    def test_flat_window_zscore_is_none_not_infinity(self):
        self.assertIsNone(E.zscore(series([7] * 40)))

    def test_flat_window_index_is_the_middle(self):
        """A contract that has held one net position for the whole window
        genuinely is at the centre of its range. 50 says that; None would
        hide a real fact."""
        self.assertEqual(E.cot_index(series([7] * 40)), 50.0)

    def test_flat_window_percentile_is_100(self):
        self.assertEqual(E.percentile(series([7] * 40)), 100.0)


class TestMeasures(unittest.TestCase):

    def test_index_spans_zero_to_hundred(self):
        s = series(list(range(-100, 100)))
        self.assertEqual(E.cot_index(s), 100.0)     # latest is the max
        self.assertEqual(E.cot_index(series(list(range(100, 0, -1)))), 0.0)

    def test_index_is_monotone_in_the_latest_value(self):
        vals = [E.cot_index(series(list(range(-50, n)))) for n in range(0, 100, 10)]
        for a, b in zip(vals, vals[1:]):
            self.assertLessEqual(a, b)

    def test_percentile_is_bounded(self):
        rnd = [3, -7, 12, 0, 55, -91, 8, 4, 4, -4]
        p = E.percentile(series(rnd))
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 100.0)

    def test_percentile_ranks_within_the_window(self):
        s = series([10] * 9 + [1000])
        self.assertEqual(E.percentile(s), 100.0)
        s2 = series([10] * 9 + [-1000])
        self.assertEqual(E.percentile(s2), 10.0)

    def test_zscore_sign_follows_the_deviation(self):
        self.assertGreater(E.zscore(series([0] * 20 + [500])), 0)
        self.assertLess(E.zscore(series([0] * 20 + [-500])), 0)

    def test_window_length_changes_the_answer(self):
        """A 1-year and a 5-year percentile are different claims, and the
        caller has to be able to tell them apart.

        The latest value is mid-history, so a short window that excludes
        the run-up ranks it near the bottom while a long one that
        includes it ranks it mid-range. Same data, same latest value, two
        different honest answers -- which is exactly why the window is
        reported next to the result rather than assumed.
        """
        s = series(list(range(300)) + [150])
        short, long = E.percentile(s, weeks=52), E.percentile(s, weeks=260)
        self.assertNotEqual(short, long)
        self.assertLess(short, long)
        # Both windows are longer than the series would otherwise be, so
        # this is a real difference and not a truncation artefact.
        self.assertEqual(len(E.window(s, 260)), 260)

    def test_window_respects_its_length(self):
        s = series(list(range(300)))
        self.assertEqual(len(E.window(s, weeks=10)), 10)
        self.assertEqual(len(E.window(s, weeks=10_000)), 300)


class TestCrowding(unittest.TestCase):

    def _series(self, latest):
        return series([0] * 9 + [latest])

    def test_bands(self):
        # crowded-* is the outer 10%, extended-* the next 15%.
        self.assertEqual(E.crowding(1, 0, 1, series([0] * 9 + [1000])),
                         "crowded-long")
        self.assertEqual(E.crowding(1, 0, 1, series([0] * 9 + [-1000])),
                         "crowded-short")
        self.assertEqual(
            E.crowding(1, 0, 1, series([1000] + [0] * 7 + [5])),
            "extended-long")
        self.assertEqual(
            E.crowding(1, 0, 1, series([-1000] + [0] * 7 + [-5])),
            "extended-short")
        self.assertEqual(E.crowding(1, 0, 1, series([-9, 9, -8, 8, -7, 7, 1])),
                         "neutral")

    def test_needs_history(self):
        self.assertIsNone(E.crowding(1, 0, 1, series([5])))

    def test_no_series_is_none(self):
        self.assertIsNone(E.crowding(1, 0, 1, None))


class TestNetSeriesOrdering(unittest.TestCase):

    def test_unsorted_input_is_sorted(self):
        """A percentile over an unsorted window is a percentile of the
        wrong sample -- 'latest' has to actually be latest."""
        rows = [{"report_date": "2026-01-07", "category": "c",
                 "long_pos": 1, "short_pos": 0},
                {"report_date": "2026-03-07", "category": "c",
                 "long_pos": 5, "short_pos": 0},
                {"report_date": "2026-02-07", "category": "c",
                 "long_pos": 3, "short_pos": 0}]
        got = E.net_series(rows, "c")
        self.assertEqual([d for d, _n in got],
                         ["2026-01-07", "2026-02-07", "2026-03-07"])
        self.assertEqual(got[-1][1], 5)

    def test_filters_by_category(self):
        rows = [{"report_date": "2026-01-07", "category": "a",
                 "long_pos": 1, "short_pos": 0},
                {"report_date": "2026-01-07", "category": "b",
                 "long_pos": 99, "short_pos": 0}]
        self.assertEqual(len(E.net_series(rows, "a")), 1)

    def test_malformed_dates_are_dropped(self):
        rows = [{"report_date": "nonsense", "category": "a",
                 "long_pos": 1, "short_pos": 0},
                {"report_date": "2026-01-07", "category": "a",
                 "long_pos": 2, "short_pos": 0}]
        self.assertEqual(len(E.net_series(rows, "a")), 1)


class TestConsensus(unittest.TestCase):

    def test_averages_percentiles_across_categories(self):
        out = E.consensus([
            {"category": "a", "percentile": 90.0, "cot_index": 80.0,
             "zscore": 2.0, "observations": 100},
            {"category": "b", "percentile": 70.0, "cot_index": 60.0,
             "zscore": 1.0, "observations": 100}])
        self.assertEqual(out["percentile"], 80.0)
        self.assertEqual(out["cot_index"], 70.0)
        self.assertEqual(out["band"], "extended-long")

    def test_ignores_categories_without_history(self):
        out = E.consensus([
            {"category": "a", "percentile": 50.0, "cot_index": 50.0,
             "zscore": 0.0, "observations": 100},
            {"category": "b", "percentile": None, "cot_index": None,
             "zscore": None, "observations": 1}])
        self.assertEqual(out["categories"], ["a"])

    def test_nothing_usable_is_none_not_zero(self):
        """An average of nothing is an absence of a reading, and 0.0 or
        'neutral' would present it as a confident midpoint."""
        self.assertIsNone(E.consensus([]))
        self.assertIsNone(E.consensus([{"category": "a", "percentile": None,
                                        "cot_index": None, "zscore": None,
                                        "observations": 0}]))


class TestStaleness(unittest.TestCase):

    def test_reports_age(self):
        # 2026-10-08T00:00Z, ten days after the 2026-09-29 report.
        import calendar
        now = calendar.timegm((2026, 10, 8, 0, 0, 0, 0, 0, 0))
        s = E.staleness("2026-09-29", now_ts=now)
        self.assertEqual(s["age_days"], 9)
        self.assertEqual(s["publication_lag_days"], 3)
        self.assertIn("2026-09-29", s["note"])

    def test_age_is_never_negative_for_a_released_report(self):
        """A report dated in the future means the date or the clock is
        wrong; returning a negative age would hide that."""
        import calendar
        now = calendar.timegm((2020, 1, 1, 0, 0, 0, 0, 0, 0))
        self.assertLess(E.staleness("2026-09-29", now_ts=now)["age_days"], 0)

    def test_malformed_date(self):
        self.assertIsNone(E.staleness("nonsense"))
        self.assertIsNone(E.staleness(None))


class TestDivergence(unittest.TestCase):

    def _rows(self, nets):
        return [{"report_date": d, "category": "c", "long_pos": n,
                 "short_pos": 0} for d, n in nets]

    def test_agreement(self):
        rows = self._rows([("2026-01-07", 10), ("2026-02-07", 100)])
        out = E.divergence(rows, "c", [100.0, 110.0])
        self.assertTrue(out["same_direction"])
        self.assertEqual(out["quadrant"], "confirming-long")

    def test_specs_short_into_a_rally(self):
        """The quadrant a positioning report is most read for."""
        rows = self._rows([("2026-01-07", 100), ("2026-02-07", 10)])
        out = E.divergence(rows, "c", [100.0, 110.0])
        self.assertFalse(out["same_direction"])
        self.assertEqual(out["quadrant"], "shorts-building-into-rally")

    def test_longs_into_a_decline(self):
        rows = self._rows([("2026-01-07", 10), ("2026-02-07", 100)])
        out = E.divergence(rows, "c", [110.0, 100.0])
        self.assertEqual(out["quadrant"], "longs-building-into-decline")

    def test_needs_both_series(self):
        self.assertIsNone(E.divergence([], "c", [1, 2, 3]))
        self.assertIsNone(E.divergence(self._rows([("2026-01-07", 1)]),
                                       "c", [1, 2, 3]))
        self.assertIsNone(E.divergence(self._rows([("2026-01-07", 1),
                                                   ("2026-02-07", 2)]),
                                       "c", []))


class TestAnalyseIntegration(unittest.TestCase):

    def setUp(self):
        self.conn = fresh_db()
        rows = []
        import datetime
        d = datetime.date(2024, 1, 9)
        for i in range(200):
            net = int(500 * (i / 199.0)) - 100
            rows.append({
                "report": "disagg_comb", "family": "disagg",
                "combined": 1, "market": "WHEAT-HRW - CHICAGO BOARD OF TRADE",
                "market_code": "CBT", "region_code": "00",
                "commodity_code": "001", "contract_code": "001612",
                "report_date": d.isoformat(), "as_of_yymmdd": "",
                "open_interest": 306523, "category": "m_money",
                "long_pos": max(0, net), "short_pos": max(0, -net),
                "spread_pos": 0, "has_spread": 1})
            d += datetime.timedelta(days=7)
        S.store(self.conn, rows, report="disagg_comb")

    def tearDown(self):
        self.conn.close()

    def test_analyse_contract_shape(self):
        a = S.analyse_contract(self.conn, "001612", "disagg_comb")
        self.assertEqual(a["market"], "WHEAT-HRW - CHICAGO BOARD OF TRADE")
        self.assertEqual(a["family"], "disagg")
        self.assertEqual(a["window_weeks"], 156)
        self.assertEqual(a["window_years"], 3.0)
        cats = {c["category"] for c in a["categories"]}
        # Only m_money was stored in setUp; a category with no rows is
        # omitted rather than reported as a zero-percentile reading.
        self.assertEqual(cats, {"m_money"})

    def test_absent_categories_are_omitted_not_zeroed(self):
        """A category with no stored rows must not appear as
        percentile=0 / band=neutral -- that is a claim about positioning
        that was never measured."""
        a = S.analyse_contract(self.conn, "001612", "disagg_comb")
        for c in a["categories"]:
            self.assertIsNotNone(c["percentile"])
        self.assertNotIn("swap", {c["category"] for c in a["categories"]})

    def test_analyse_carries_staleness(self):
        """Every read says how old it is. COT read as current is the
        fastest route to a wrong conclusion from correct data."""
        a = S.analyse_contract(self.conn, "001612", "disagg_comb")
        self.assertIsNotNone(a["staleness"])
        self.assertIn("age_days", a["staleness"])

    def test_window_is_reported_not_implied(self):
        a = S.analyse_contract(self.conn, "001612", "disagg_comb", weeks=52)
        self.assertEqual(a["window_weeks"], 52)
        self.assertEqual(a["window_years"], 1.0)
        self.assertEqual(a["categories"][0]["window_weeks"], 52)

    def test_history_is_oldest_first_and_limited(self):
        h = S.contract_history(self.conn, "001612", "disagg_comb",
                               category="m_money", limit=50)
        self.assertEqual(len(h), 50)
        dates = [x["date"] for x in h]
        self.assertEqual(dates, sorted(dates))
        # 200 weeks stored, 50 requested -> the most recent 50, in order.
        self.assertEqual(dates[0], dates[-1 - 49])
        self.assertEqual(h[0]["date"], dates[0])
        all_dates = [r["report_date"] for r in
                     S.positions(self.conn, "disagg_comb",
                                 contract_code="001612",
                                 category="m_money")]
        self.assertEqual(dates, all_dates[-50:])

    def test_history_carries_the_net_precomputed(self):
        h = S.contract_history(self.conn, "001612", "disagg_comb",
                               category="m_money", limit=5)
        for x in h:
            self.assertEqual(x["net"], x["long"] - x["short"])

    def test_unknown_contract(self):
        a = S.analyse_contract(self.conn, "ZZZZZZ", "disagg_comb")
        self.assertEqual(a["market"], "")
        self.assertIsNone(a["consensus"])


if __name__ == "__main__":
    unittest.main()

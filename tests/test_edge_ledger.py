"""Edge ledger, promotion gate, and forward shadow. Stdlib only.

The subject is a question mafutani had no way to answer before: *does this
strategy have an edge, and may it trade real money?* `strategies.mode` is
`wired`/`stub`, which is a statement about whether code exists. A wired
strategy with no edge and a wired strategy with a deflated Sharpe of 0.99
are the same row.

Three properties are load-bearing, and each is a bug that was easy to write:

**No evidence means not promoted, and the reason says so.** The obvious
failure is a gate that returns a bare `False`, so the operator cannot tell
"insufficient cycles" from "no record at all". Every failing condition is
reported, because a gate that takes four runs to explain gets bypassed.

**Evidence rows are never rewritten.** An in-place "corrected" number
destroys the only evidence of what was believed at the time. Stages are
appended, so the history of an edge is legible.

**Nothing promotes itself.** A stage change is an operator action, matching
`brokers/gods.py`. `set_stage(..., 'promoted')` re-checks and refuses, so
the refusal is recorded where the promotion was attempted rather than
discovered later.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engines import edgeledger as L   # noqa: E402
from engines import shadow as S       # noqa: E402


class _Db(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("DB_PATH")
        self._tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"] = os.path.join(self._tmp, "l.db")
        from data.db import init_db
        self.conn = init_db()

    def tearDown(self):
        self.conn.close()
        if self._prev is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._prev

    # -- fixtures ------------------------------------------------------
    def good_evidence(self, strategy="ema_trend", **over):
        """Evidence that passes every gate condition."""
        summary = {
            "significance": {"verdict": L.PROMOTING_VERDICT, "dsr": 0.97,
                             "n_trials": 42, "trades": 800},
            "walkforward": {"degradation": 0.85, "verdict": "holds up"},
        }
        summary.update(over.pop("summary", {}))
        return L.record_evidence(self.conn, strategy, summary,
                                 engine_version=L._current_engine_version(),
                                 **over)

    def enough_shadow(self, strategy="ema_trend", n=L.MIN_SHADOW_CYCLES):
        for _ in range(n):
            L.record_shadow(self.conn, strategy, 1)


# --------------------------------------------------------------------------
# Writing: append-only, attributable
# --------------------------------------------------------------------------


class TestRecordEvidence(_Db):
    def test_evidence_is_stored_and_readable(self):
        self.good_evidence()
        row = L.latest(self.conn, "ema_trend")
        self.assertIsNotNone(row)
        self.assertEqual(row["verdict"], L.PROMOTING_VERDICT)
        self.assertEqual(row["summary"]["significance"]["dsr"], 0.97)

    def test_an_unattributable_row_is_refused(self):
        """A record nobody can attribute to a strategy looks like evidence
        while being unusable."""
        self.assertIsNone(L.record_evidence(self.conn, "", {"verdict": "x"}))
        self.assertIsNone(L.record_evidence(self.conn, None, {"verdict": "x"}))
        self.assertIsNone(L.latest(self.conn, ""))

    def test_evidence_is_never_rewritten(self):
        """Two records of the same strategy are two records, not one
        overwritten -- otherwise the correction destroys the original."""
        self.good_evidence()
        self.good_evidence(summary={"significance": {"verdict": "no edge after costs"}})
        rows = L.history(self.conn, "ema_trend")
        self.assertEqual(len(rows), 2)
        verdicts = {r["verdict"] for r in rows}
        self.assertEqual(verdicts, {L.PROMOTING_VERDICT, "no edge after costs"})

    def test_the_latest_evidence_is_the_one_the_gate_reads(self):
        self.good_evidence()
        self.good_evidence(summary={"significance": {"verdict": "no edge after costs"}})
        self.assertEqual(L.latest(self.conn, "ema_trend")["verdict"],
                         "no edge after costs")

    def test_engine_version_is_recorded(self):
        self.good_evidence()
        row = L.latest(self.conn, "ema_trend")
        self.assertEqual(row["engine_version"], L._current_engine_version())

    def test_evidence_without_a_verdict_stores_none_rather_than_a_guess(self):
        L.record_evidence(self.conn, "x", {"significance": {"dsr": 0.99}})
        self.assertEqual(L.latest(self.conn, "x")["verdict"], "")

    def test_a_bad_stage_does_not_corrupt_the_record(self):
        L.record_evidence(self.conn, "x", {"verdict": "y"}, stage="nonsense")
        self.assertEqual(L.latest(self.conn, "x")["stage"], "candidate")


class TestShadowCounting(_Db):
    def test_cycles_accumulate(self):
        self.assertEqual(L.record_shadow(self.conn, "s", 1), 1)
        self.assertEqual(L.record_shadow(self.conn, "s", 1), 2)
        self.assertEqual(L.record_shadow(self.conn, "s", 3), 5)

    def test_cycles_are_per_strategy(self):
        L.record_shadow(self.conn, "a", 5)
        self.assertEqual(L.shadow_cycles(self.conn, "b"), 0)

    def test_a_negative_count_is_refused(self):
        """A cycle count cannot go backwards, and a negative row would let
        the total understate what was observed."""
        self.assertIsNone(L.record_shadow(self.conn, "s", -1))
        self.assertEqual(L.shadow_cycles(self.conn, "s"), 0)

    def test_a_junk_count_is_refused_rather_than_defaulted(self):
        for junk in ("many", None, [], {}):
            self.assertIsNone(L.record_shadow(self.conn, "s", junk), junk)

    def test_shadow_rows_are_not_counted_as_evidence(self):
        """Otherwise a strategy could acquire a verdict by accruing shadow
        cycles without ever being backtested."""
        self.enough_shadow()
        self.assertIsNone(L.latest(self.conn, "ema_trend", kind="evidence"))

    def test_shadow_count_reads_only_shadow_rows(self):
        """Regression, caught by mutation: widening the count's query to any
        row kind let an evidence row's shadow_cycles seed the total, so
        enough shadow cycles could be manufactured with no observation at
        all."""
        L.record_shadow(self.conn, "s", 2)
        L.record_evidence(self.conn, "s", {"verdict": "x"},
                          engine_version=L._current_engine_version())
        self.assertEqual(L.shadow_cycles(self.conn, "s"), 2,
                         "an evidence row contributed to the shadow total")

        L.record_evidence(self.conn, "s", {"verdict": "x"},
                          engine_version=L._current_engine_version())
        self.assertEqual(L.shadow_cycles(self.conn, "s"), 2)

    def test_the_cycle_total_survives_evidence_writes(self):
        L.record_shadow(self.conn, "s", 3)
        for _ in range(3):
            L.record_evidence(self.conn, "s", {"verdict": "x"})
        self.assertEqual(L.shadow_cycles(self.conn, "s"), 3)
        L.record_shadow(self.conn, "s", 1)
        self.assertEqual(L.shadow_cycles(self.conn, "s"), 4)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


class TestGateRefusesWithoutEvidence(_Db):
    def test_no_evidence_is_not_eligible(self):
        chk = L.promotion_check(self.conn, "unheard_of")
        self.assertFalse(chk["eligible"])

    def test_no_evidence_says_what_to_do(self):
        chk = L.promotion_check(self.conn, "unheard_of")
        self.assertTrue(any("no evidence" in r for r in chk["reasons"]), chk)

    def test_no_named_strategy_is_refused(self):
        self.assertFalse(L.promotion_check(self.conn, "")["eligible"])

    def test_no_reason_is_ever_empty(self):
        """A refusal with no reason is how a gate gets bypassed."""
        self.good_evidence()
        chk = L.promotion_check(self.conn, "ema_trend")
        for r in chk["reasons"]:
            self.assertTrue(str(r).strip(), "empty reason in %s" % chk)


class TestGateConditions(_Db):
    def test_full_evidence_and_cycles_promotes(self):
        self.good_evidence()
        self.enough_shadow()
        chk = L.promotion_check(self.conn, "ema_trend")
        self.assertTrue(chk["eligible"], chk["reasons"])
        self.assertEqual(chk["reasons"], [])

    def test_plausible_but_unproven_does_not_promote(self):
        """`edge plausible, unproven` is the verdict that exists to be read
        and then not acted on."""
        self.good_evidence(summary={"significance": {
            "verdict": "edge plausible, unproven", "dsr": 0.85}})
        self.enough_shadow()
        chk = L.promotion_check(self.conn, "ema_trend")
        self.assertFalse(chk["eligible"])
        self.assertTrue(any("not acted on" in r for r in chk["reasons"]), chk)

    def test_no_edge_after_costs_does_not_promote(self):
        self.good_evidence(summary={"significance": {
            "verdict": "no edge after costs"}})
        self.enough_shadow()
        self.assertFalse(L.promotion_check(self.conn, "ema_trend")["eligible"])

    def test_insufficient_data_does_not_promote(self):
        self.good_evidence(summary={"significance": {
            "verdict": "insufficient data"}})
        self.enough_shadow()
        self.assertFalse(L.promotion_check(self.conn, "ema_trend")["eligible"])

    def test_missing_out_of_sample_score_blocks(self):
        self.good_evidence(summary={"walkforward": {"verdict": "ok"}})
        self.enough_shadow()
        chk = L.promotion_check(self.conn, "ema_trend")
        self.assertFalse(chk["eligible"])
        self.assertTrue(any("out-of-sample" in r for r in chk["reasons"]), chk)

    def test_curve_fitting_out_of_sample_blocks(self):
        self.good_evidence(summary={"walkforward": {
            "degradation": 0.2, "verdict": "does not survive out-of-sample"}})
        self.enough_shadow()
        chk = L.promotion_check(self.conn, "ema_trend")
        self.assertFalse(chk["eligible"])
        self.assertTrue(any("20%" in r for r in chk["reasons"]), chk)

    def test_too_few_shadow_cycles_blocks(self):
        self.good_evidence()
        L.record_shadow(self.conn, "ema_trend", 1)
        chk = L.promotion_check(self.conn, "ema_trend")
        self.assertFalse(chk["eligible"])
        self.assertTrue(any("shadow cycle" in r for r in chk["reasons"]), chk)

    def test_every_failing_condition_is_reported_not_just_the_first(self):
        """Otherwise 'why was this refused' needs four runs to answer."""
        self.good_evidence(summary={"significance": {"verdict": "insufficient data"},
                                    "walkforward": {"degradation": 0.1}})
        chk = L.promotion_check(self.conn, "ema_trend")
        self.assertGreaterEqual(len(chk["reasons"]), 3, chk["reasons"])

    def test_superseded_engine_version_blocks(self):
        """Evidence from a different engine version describes a different
        experiment, so its numbers are not comparable."""
        L.record_evidence(self.conn, "s",
                          {"significance": {"verdict": L.PROMOTING_VERDICT},
                           "walkforward": {"degradation": 0.9}},
                          engine_version=L._current_engine_version() - 1)
        self.enough_shadow("s")
        chk = L.promotion_check(self.conn, "s")
        self.assertFalse(chk["eligible"])
        self.assertTrue(any("engine version" in r for r in chk["reasons"]), chk)

    def test_missing_engine_version_is_not_treated_as_a_mismatch(self):
        """An older row with no version stamp is unknown provenance, not
        proof of a different experiment. Blocking on it would make every
        pre-stamp record permanently ineligible with no way to fix it."""
        L.record_evidence(self.conn, "s",
                          {"significance": {"verdict": L.PROMOTING_VERDICT},
                           "walkforward": {"degradation": 0.9}},
                          engine_version=None)
        self.enough_shadow("s")
        self.assertTrue(L.promotion_check(self.conn, "s")["eligible"])


class TestExplicitAdoption(_Db):
    def test_promotion_is_refused_when_ineligible(self):
        ok, why = L.set_stage(self.conn, "nope", "promoted", decided_by="op")
        self.assertFalse(ok)
        self.assertIn("refused", why)

    def test_promotion_succeeds_once_eligible(self):
        self.good_evidence()
        self.enough_shadow()
        ok, why = L.set_stage(self.conn, "ema_trend", "promoted",
                              decided_by="operator")
        self.assertTrue(ok, why)
        self.assertEqual(L.current_stage(self.conn, "ema_trend"), "promoted")

    def test_the_refusal_is_recorded_where_it_was_attempted(self):
        """Not discovered later, after the reason has been forgotten."""
        L.set_stage(self.conn, "nope", "promoted", decided_by="op", note="try 1")
        rows = L.history(self.conn, "nope")
        self.assertEqual(len(rows), 1, "a refused promotion left no trace")
        self.assertEqual(rows[0]["kind"], "refused_promotion")
        self.assertIn("no evidence", " ".join(rows[0]["summary"]["reasons"]))

    def test_a_refusal_does_not_move_the_stage(self):
        """A strategy short of cycles is unproven, not rejected. Recording
        the attempt as the `rejected` stage would assert more than the
        evidence supports."""
        L.set_stage(self.conn, "nope", "promoted", decided_by="op")
        self.assertEqual(L.current_stage(self.conn, "nope"), "candidate")

    def test_a_refusal_does_not_block_a_later_genuine_promotion(self):
        L.set_stage(self.conn, "ema_trend", "candidate", decided_by="op")
        ok, _ = L.set_stage(self.conn, "ema_trend", "promoted", decided_by="op")
        self.assertFalse(ok)
        self.good_evidence()
        self.enough_shadow()
        ok, why = L.set_stage(self.conn, "ema_trend", "promoted",
                              decided_by="op")
        self.assertTrue(ok, why)
        self.assertEqual(L.current_stage(self.conn, "ema_trend"), "promoted")

    def test_an_unknown_stage_is_refused(self):
        ok, why = L.set_stage(self.conn, "s", "definitely-live", decided_by="op")
        self.assertFalse(ok)
        self.assertIn("unknown stage", why)

    def test_a_stage_change_needs_a_name(self):
        self.assertFalse(L.set_stage(self.conn, "", "candidate")[0])

    def test_promotion_records_who_decided(self):
        self.good_evidence()
        self.enough_shadow()
        L.set_stage(self.conn, "ema_trend", "promoted", decided_by="alice")
        rows = L.history(self.conn, "ema_trend")
        self.assertEqual(rows[0]["decided_by"], "alice")
        self.assertEqual(rows[0]["kind"], "stage")


# --------------------------------------------------------------------------
# Forward shadow
# --------------------------------------------------------------------------


class TestShadowCycles(_Db):
    def test_a_cycle_with_trades_is_recorded(self):
        ok, d = S.run_cycle(self.conn, "s", trade_pnl_bps=[10.0, -4.0, 6.0])
        self.assertTrue(ok)
        self.assertEqual(d["trades"], 3)
        self.assertEqual(d["wins"], 2)

    def test_a_cycle_that_did_not_fire_is_still_a_cycle(self):
        """"We looked and nothing happened" is a real observation.
        Conflating it with "nobody looked" would let a strategy accrue
        forward evidence by never being examined."""
        ok, d = S.run_cycle(self.conn, "s", trade_pnl_bps=[])
        self.assertTrue(ok)
        self.assertTrue(d["no_signal"])
        self.assertEqual(len(S.cycles(self.conn, "s")), 1)

    def test_junk_pnl_is_dropped_not_stored(self):
        ok, d = S.run_cycle(self.conn, "s", trade_pnl_bps=[1.0, "x", None, 3.0])
        self.assertTrue(ok)
        self.assertEqual(d["trades"], 2)

    def test_no_signal_never_has_an_expectancy(self):
        S.run_cycle(self.conn, "s", trade_pnl_bps=[])
        self.assertIsNone(S.summary(self.conn, "s")["expectancy_bps"])

    def test_a_cycle_needs_a_strategy(self):
        self.assertFalse(S.run_cycle(self.conn, "", trade_pnl_bps=[1.0])[0])

    def test_cycles_are_anchored_to_their_evidence(self):
        eid = self.good_evidence()
        S.run_cycle(self.conn, "ema_trend", trade_pnl_bps=[1.0], anchor_id=eid,
                    engine_version=L._current_engine_version())
        row = S.cycles(self.conn, "ema_trend")[0]
        self.assertEqual(row["anchor_id"], eid)
        self.assertEqual(row["engine_version"], L._current_engine_version())


class TestShadowSummary(_Db):
    def _run(self, n, pnl_each, predicted=None):
        for i in range(n):
            S.run_cycle(self.conn, "s", trade_pnl_bps=list(pnl_each),
                        predicted_bps=predicted)

    def test_never_run_is_distinct_from_everything_else(self):
        self.assertEqual(S.summary(self.conn, "s")["verdict"], "never run")

    def test_observed_but_silent_is_its_own_state(self):
        self._run(12, [])
        s = S.summary(self.conn, "s")
        self.assertEqual(s["verdict"], "observed, no signal")
        self.assertIsNone(s["expectancy_bps"])

    def test_too_few_cycles_with_trades_is_its_own_state(self):
        self._run(3, [5.0, -1.0])
        s = S.summary(self.conn, "s")
        self.assertEqual(s["verdict"], "too few cycles")
        self.assertTrue(any("smaller scale" in r for r in s["reasons"]))

    def test_enough_cycles_gets_a_verdict(self):
        self._run(S.MIN_CYCLES_FOR_VERDICT, [5.0, -1.0, 3.0])
        self.assertEqual(S.summary(self.conn, "s")["verdict"],
                         "holding up in forward observation")

    def test_expectancy_is_measured_across_all_trades(self):
        self._run(3, [10.0, 0.0])
        self.assertEqual(S.summary(self.conn, "s")["trades"], 6)
        self.assertEqual(S.summary(self.conn, "s")["expectancy_bps"], 5.0)

    def test_decay_against_a_prediction_is_reported(self):
        self._run(S.MIN_CYCLES_FOR_VERDICT, [-8.0], predicted=10.0)
        s = S.summary(self.conn, "s")
        self.assertTrue(s["deteriorated"])
        self.assertEqual(s["verdict"], "edge has decayed")

    def test_no_prediction_means_decay_is_unknown_not_false(self):
        """"We predicted nothing" is not "it is holding up"."""
        self._run(S.MIN_CYCLES_FOR_VERDICT, [-8.0])
        self.assertIsNone(S.summary(self.conn, "s")["deteriorated"])

    def test_a_negative_prediction_cannot_prove_decay(self):
        """The recorded expectancy was already negative, so the forward
        result is not a deterioration from it."""
        self._run(S.MIN_CYCLES_FOR_VERDICT, [-8.0], predicted=-10.0)
        self.assertFalse(S.deteriorated({"expectancy_bps": -8.0,
                                         "predicted_bps": -10.0}))

    def test_decay_requires_no_capital_and_demotes_nothing(self):
        """This module measures. Promotion is a separate, explicit action."""
        self._run(S.MIN_CYCLES_FOR_VERDICT, [-8.0], predicted=10.0)
        s = S.summary(self.conn, "s")
        self.assertEqual(s["verdict"], "edge has decayed")
        self.assertNotIn("stage", s)


class TestStdlibOnly(unittest.TestCase):
    def test_no_third_party_imports(self):
        import ast
        for name in ("edgeledger.py", "shadow.py"):
            with open(os.path.join(ROOT, "engines", name),
                      encoding="utf-8") as f:
                tree = ast.parse(f.read())
            found = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        found.add(a.name.split(".")[0])
                elif isinstance(n, ast.ImportFrom) and n.module:
                    found.add(n.module.split(".")[0])
            allowed = {"json", "time", "engines", "significance", "edgeledger",
                       "shadow"}
            self.assertEqual(found - allowed, set(),
                             "%s imports %s" % (name, sorted(found - allowed)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for the GODS layer: DNA rules, dissenter, veto, restraint log.

The failure modes here are specific:
* a mutated rule that the DNA does not describe (codegen risk)
* a dissenter that always objects, or never does
* a veto gate that can be overruled
* a restraint log that records wins but not refusals
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from brokers import gods          # noqa: E402
from brokers import vibe          # noqa: E402
from brokers.gods import GodsError  # noqa: E402


def make_signal(symbol="BTCUSD", rule_id="R1", ts=1000):
    return vibe.Signal(symbol, rule_id, "test rule", {}, 100.0, ts)


def make_profile(rules=None):
    rules = rules or [vibe.Rule("R1", "test", vibe.cmp("rsi14", "<", 30),
                                support=5, coverage=0.5)]
    return vibe.Profile("p", rules, 10, 5, covered=5)


class TestDnaRule(unittest.TestCase):
    def test_dna_to_condition(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
        ])
        cond = dna.to_condition()
        self.assertIsNotNone(cond)
        self.assertTrue(vibe.eval_condition(cond, {"rsi14": 20.0}))
        self.assertFalse(vibe.eval_condition(cond, {"rsi14": 40.0}))

    def test_dna_to_condition_multi_gene(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0),
            ("operator", ">"), ("indicator", "volume_ratio"), ("real", 1.0),
        ])
        cond = dna.to_condition()
        self.assertIsNotNone(cond)
        self.assertTrue(vibe.eval_condition(
            cond, {"rsi14": 20.0, "volume_ratio": 2.0}))
        self.assertFalse(vibe.eval_condition(
            cond, {"rsi14": 20.0, "volume_ratio": 0.5}))

    def test_dna_too_short(self):
        dna = gods.DnaRule([("operator", "<"), ("indicator", "rsi14")])
        self.assertIsNone(dna.to_condition())

    def test_dna_bad_operator(self):
        dna = gods.DnaRule([
            ("operator", "~"), ("indicator", "rsi14"), ("real", 30.0)
        ])
        with self.assertRaises(GodsError):
            dna.to_condition()

    def test_dna_bad_indicator(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "not_a_feature"), ("real", 30.0)
        ])
        with self.assertRaises(GodsError):
            dna.to_condition()

    def test_dna_bad_value(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", "thirty")
        ])
        with self.assertRaises(GodsError):
            dna.to_condition()

    def test_mutate_returns_new_rule(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
        ])
        mutated = dna.mutate(rate=1.0, rng=lambda: 0.0)
        self.assertIsNot(mutated, dna)
        self.assertIsInstance(mutated, gods.DnaRule)

    def test_mutate_changes_something(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
        ])
        # rate=1.0 with rng=0.0 always picks the first alternative.
        mutated = dna.mutate(rate=1.0, rng=lambda: 0.0)
        self.assertNotEqual(dna.dna, mutated.dna)

    def test_mutate_preserves_length(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0),
            ("operator", ">"), ("indicator", "volume_ratio"), ("real", 1.0),
        ])
        mutated = dna.mutate(rate=1.0, rng=lambda: 0.0)
        self.assertEqual(len(dna.dna), len(mutated.dna))

    def test_mutate_reproducible(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
        ])
        a = dna.mutate(rate=0.5, rng=lambda: 0.1)
        b = dna.mutate(rate=0.5, rng=lambda: 0.1)
        self.assertEqual(a.dna, b.dna)

    def test_crossover(self):
        a = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
        ])
        b = gods.DnaRule([
            ("operator", ">"), ("indicator", "volume_ratio"), ("real", 2.0)
        ])
        child = a.crossover(b)
        self.assertIsInstance(child, gods.DnaRule)
        self.assertEqual(len(child.dna), 3)

    def test_crossover_empty_raises(self):
        with self.assertRaises(GodsError):
            gods.DnaRule([]).crossover(gods.DnaRule([]))

    def test_to_dict(self):
        dna = gods.DnaRule([
            ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
        ])
        d = dna.to_dict()
        self.assertEqual(d["rule_id"], "R0")
        self.assertEqual(len(d["dna"]), 3)


class TestDissenter(unittest.TestCase):
    def test_passes_a_good_signal(self):
        sig = make_signal()
        prof = make_profile()
        d = gods.Dissenter(min_support=3)
        verdict, reasons = d.dissent(sig, prof)
        self.assertEqual(verdict, "pass")
        self.assertEqual(reasons, [])

    def test_objects_to_thin_support(self):
        sig = make_signal()
        rule = vibe.Rule("R1", "test", vibe.cmp("rsi14", "<", 30),
                         support=1, coverage=0.1)
        prof = vibe.Profile("p", [rule], 10, 5, covered=5)
        d = gods.Dissenter(min_support=3)
        verdict, reasons = d.dissent(sig, prof)
        self.assertEqual(verdict, "object")
        self.assertTrue(any("support" in r for r in reasons))

    def test_objects_to_zero_coverage(self):
        sig = make_signal()
        rule = vibe.Rule("R1", "test", vibe.cmp("rsi14", "<", 30),
                         support=5, coverage=0.0)
        prof = vibe.Profile("p", [rule], 10, 5, covered=5)
        d = gods.Dissenter(min_support=3)
        verdict, reasons = d.dissent(sig, prof)
        self.assertEqual(verdict, "object")
        self.assertTrue(any("coverage" in r for r in reasons))

    def test_objects_to_stale_features(self):
        sig = make_signal()
        prof = make_profile()
        # The newest non-None value is 9 bars from the end of the series.
        stale = {"rsi14": [50.0] + [None] * 9}
        d = gods.Dissenter(min_support=3, max_feature_age_bars=5)
        verdict, reasons = d.dissent(sig, prof, features=stale)
        self.assertEqual(verdict, "object")
        self.assertTrue(any("old" in r for r in reasons))

    def test_fresh_features_pass(self):
        # The newest non-None value is at the last bar: age 0.
        fresh = {"rsi14": [None] * 9 + [50.0]}
        d = gods.Dissenter(min_support=3, max_feature_age_bars=5)
        verdict, _ = d.dissent(make_signal(), make_profile(), features=fresh)
        self.assertEqual(verdict, "pass")

    def test_no_signal_objects(self):
        d = gods.Dissenter()
        verdict, reasons = d.dissent(None, make_profile())
        self.assertEqual(verdict, "object")
        self.assertTrue(any("no signal" in r for r in reasons))

    def test_deterministic(self):
        sig = make_signal()
        prof = make_profile()
        d = gods.Dissenter(min_support=3)
        a = d.dissent(sig, prof)
        b = d.dissent(sig, prof)
        self.assertEqual(a, b)


class TestVetoGate(unittest.TestCase):
    def test_risk_gate_passes(self):
        sig = make_signal()
        prof = make_profile()
        gate = gods.VetoGate("risk", gods.default_risk_gate)
        ok, reason = gate.evaluate(sig, prof)
        self.assertTrue(ok)

    def test_risk_gate_rejects_thin_support(self):
        sig = make_signal()
        rule = vibe.Rule("R1", "test", vibe.cmp("rsi14", "<", 30),
                         support=1, coverage=0.1)
        prof = vibe.Profile("p", [rule], 10, 5, covered=5)
        gate = gods.VetoGate("risk", gods.default_risk_gate)
        ok, reason = gate.evaluate(sig, prof)
        self.assertFalse(ok)
        self.assertIn("support", reason)

    def test_risk_gate_fails_closed_on_no_profile(self):
        sig = make_signal()
        gate = gods.VetoGate("risk", gods.default_risk_gate)
        ok, reason = gate.evaluate(sig, None)
        self.assertFalse(ok)

    def test_compliance_gate_rejects_unknown_symbol(self):
        sig = make_signal(symbol="UNKNOWN")
        gate = gods.VetoGate("compliance", gods.default_compliance_gate)
        ok, reason = gate.evaluate(sig, make_profile())
        self.assertFalse(ok)
        self.assertIn("allowlist", reason)

    def test_compliance_gate_passes_allowed_symbol(self):
        sig = make_signal(symbol="BTCUSD")
        gate = gods.VetoGate(
            "compliance",
            lambda s, p, f: (True, "") if s.symbol == "BTCUSD"
            else (False, "not allowed"))
        ok, _ = gate.evaluate(sig, make_profile())
        self.assertTrue(ok)

    def test_gate_exception_fails_closed(self):
        def bad_gate(s, p, f):
            raise RuntimeError("boom")
        sig = make_signal()
        gate = gods.VetoGate("bad", bad_gate)
        ok, reason = gate.evaluate(sig, make_profile())
        self.assertFalse(ok)
        self.assertIn("raised", reason)


class TestRestraintLog(unittest.TestCase):
    def test_records_rejection(self):
        log = gods.RestraintLog()
        sig = make_signal()
        log.record(sig, "risk", "support 1 < 3", rule_id="R1")
        self.assertEqual(len(log), 1)
        entry = log.recent(1)[0]
        self.assertEqual(entry["gate"], "risk")
        self.assertEqual(entry["reason"], "support 1 < 3")

    def test_recent_returns_last_n(self):
        log = gods.RestraintLog()
        for i in range(5):
            log.record(make_signal(ts=i), "risk", "reason {}".format(i))
        recent = log.recent(3)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[-1]["reason"], "reason 4")

    def test_limit_enforced(self):
        log = gods.RestraintLog(limit=3)
        for i in range(10):
            log.record(make_signal(ts=i), "risk", "r")
        self.assertEqual(len(log), 3)

    def test_to_dict(self):
        log = gods.RestraintLog()
        log.record(make_signal(), "risk", "reason")
        d = log.to_dict()
        self.assertEqual(d["count"], 1)
        self.assertEqual(len(d["recent"]), 1)


class TestGodsExports(unittest.TestCase):
    def test_exports_present(self):
        for name in ("DnaRule", "Dissenter", "VetoGate", "RestraintLog",
                     "mutate_rule", "crossover", "dissent", "GodsError"):
            self.assertTrue(hasattr(gods, name), "missing: " + name)

    def test_no_third_party(self):
        src = open(os.path.join(WORKDIR, "brokers", "gods.py")).read()
        for banned in ("numpy", "pandas", "scipy", "torch", "tensorflow"):
            self.assertNotIn(banned, src)

    def test_gods_error_is_broker_error(self):
        from brokers.base import BrokerError
        self.assertTrue(issubclass(gods.GodsError, BrokerError))


if __name__ == "__main__":
    unittest.main()

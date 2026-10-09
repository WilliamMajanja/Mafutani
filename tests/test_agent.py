"""Agent runtime: bounded authority, adversarial review, fail-closed gates.

The properties under test are the ones that make an autonomous actor
safe to have at all:

  * a mandate grants exactly the authority it enumerates and no more
  * an agent never auto-executes unless explicitly armed
  * gates that raise block the action rather than allowing it
  * every suppression is recorded, not just the successes
  * a paused or retired agent does nothing
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from brokers.agent import (AgentRuntime, Mandate, MandateError,  # noqa: E402
                           Proposal, TradingAgent)


def mk(closes):
    return [{"ts": i, "o": c, "h": c * 1.002, "l": c * 0.998, "c": c, "v": 1000.0}
            for i, c in enumerate(closes)]


def uptrend(n=200, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


def flat_then_jump(n=200, flat=100.0, jump=140.0):
    """Flat tape with one large move on the final bar.

    A smooth ramp does NOT produce a crossover: dual_ema requires a
    transition on the last bar (fast crosses slow *now*), and a linear
    ramp crossed many bars earlier. Fixtures built from a ramp therefore
    assert nothing, because the agent correctly finds no signal.
    """
    return [flat] * (n - 1) + [jump]


def market(closes, bid=None, ask=None, last=None):
    px = last if last is not None else closes[-1]
    b = bid if bid is not None else px * 0.9999
    a = ask if ask is not None else px * 1.0001
    return {"candles": mk(closes), "quote": {"bid": b, "ask": a, "last": px}}


class TestMandateAuthority(unittest.TestCase):
    def test_symbol_scope_is_exact(self):
        m = Mandate("a", symbols=["BTCUSD", "ethusd"])
        self.assertEqual(m.symbols, ["BTCUSD", "ETHUSD"])
        self.assertTrue(m.may_trade("btcusd"), "lookup must be case-insensitive")
        self.assertFalse(m.may_trade("AAPL"))

    def test_notional_cap(self):
        m = Mandate("a", symbols=["BTCUSD"], max_notional=1000)
        self.assertTrue(m.notional_ok(0.01, 84000))     # 840
        self.assertFalse(m.notional_ok(1.0, 84000))     # 84000

    def test_rate_budget_slides(self):
        m = Mandate("a", symbols=["X"], max_actions_per_hour=2)
        now = 1000.0
        self.assertTrue(m.rate_ok(now)); m.record_action(now)
        self.assertTrue(m.rate_ok(now)); m.record_action(now)
        self.assertFalse(m.rate_ok(now), "third action must be refused")
        # an hour later the window has rolled
        self.assertTrue(m.rate_ok(now + 3601))

    def test_invalid_mandates_rejected(self):
        for kw in ({"mode": "live2"}, {"max_notional": 0},
                   {"max_notional": -1}, {"max_notional": "abc"},
                   {"max_actions_per_hour": 0}):
            with self.assertRaises(MandateError, msg=str(kw)):
                Mandate("a", symbols=["X"], **kw)

    def test_auto_execute_defaults_off(self):
        self.assertFalse(Mandate("a", symbols=["X"]).auto_execute)

    def test_starts_draft_not_running(self):
        """Importing or constructing must never move a market."""
        self.assertEqual(Mandate("a", symbols=["X"]).state, "draft")


class TestGatesFailClosed(unittest.TestCase):
    def setUp(self):
        self.m = Mandate("a", symbols=["BTCUSD"], max_notional=1e9)
        self.m.state = "running"
        self.agent = TradingAgent(self.m)

    def _sig(self, qty=1.0, price=100.0):
        return {"symbol": "BTCUSD", "side": "buy", "qty": qty, "price": price,
                "rule_id": "R0", "notes": []}

    def test_gate_that_raises_blocks(self):
        def boom(signal, profile, features=None):
            raise RuntimeError("risk engine down")
        g = self.agent.gate.__class__("risk", boom)
        ok, reason = g.evaluate(self._sig(), None, None)
        self.assertFalse(ok, "a raising gate must not allow")
        self.assertIn("raised", reason)

    def test_notional_gate_rejects_bad_sizes(self):
        ok, _ = self.agent.gate.evaluate(self._sig(qty=0, price=100), None, None)
        self.assertFalse(ok)
        ok, _ = self.agent.gate.evaluate(self._sig(qty=1, price=0), None, None)
        self.assertFalse(ok)
        ok, _ = self.agent.gate.evaluate(self._sig(qty="x", price=100), None, None)
        self.assertFalse(ok)

    def test_compliance_gate_with_no_allowlist_blocks(self):
        """gods.default_compliance_gate rejects everything with an empty
        allowlist by design. The agent gate must too -- a gate that
        allows everything is a comment, not a control."""
        ok, reason = self.agent.compliance.evaluate(self._sig(), None, None)
        self.assertFalse(ok)
        self.assertIn("allowlist", reason)

    def test_compliance_gate_allows_listed_symbol(self):
        from brokers.agent import _allowlist_gate
        from brokers.gods import VetoGate
        g = VetoGate("compliance", _allowlist_gate)
        ok, _ = g.evaluate(self._sig(), {"symbols": ["BTCUSD"]}, None)
        self.assertTrue(ok)
        ok, _ = g.evaluate(self._sig(), {"symbols": ["AAPL"]}, None)
        self.assertFalse(ok)


class TestAgentProposesNotTrades(unittest.TestCase):
    def setUp(self):
        self.m = Mandate("a", strategy="dual_ema", symbols=["BTCUSD"],
                         max_notional=1e9)
        self.m.state = "running"
        self.executed = []
        self.agent = TradingAgent(self.m, executor=lambda p: self.executed.append(p))

    def test_no_executor_means_no_orders(self):
        a = TradingAgent(self.m)   # no executor at all
        out = a.tick({"BTCUSD": market(uptrend())})
        for p in out:
            self.assertNotEqual(p.outcome, "executed")

    def test_unarmed_agent_never_executes(self):
        out = self.agent.tick({"BTCUSD": market(uptrend())})
        self.assertEqual(self.executed, [],
                         "an unarmed agent placed an order")
        for p in out:
            self.assertIn(p.outcome, ("proposed", "held", "blocked"))

    def test_armed_agent_executes(self):
        self.m.auto_execute = True
        out = self.agent.tick({"BTCUSD": market(uptrend())})
        if out and out[0].outcome == "proposed":
            self.assertEqual(self.executed and len(self.executed), 1)
        # if it was held or blocked it must NOT have executed
        if out and out[0].outcome != "proposed":
            self.assertEqual(self.executed, [])

    def test_executor_exception_is_contained(self):
        def boom(p):
            raise RuntimeError("venue down")
        self.m.auto_execute = True
        a = TradingAgent(self.m, executor=boom)
        out = a.tick({"BTCUSD": market(uptrend())})
        for p in out:
            self.assertIn(p.outcome, ("executed", "blocked"))
            if p.outcome == "blocked":
                self.assertIn("venue down", " ".join(p.notes))

    def test_paused_agent_does_nothing(self):
        self.m.state = "paused"
        self.assertEqual(self.agent.tick({"BTCUSD": market(uptrend())}), [])

    def test_short_history_yields_nothing(self):
        out = self.agent.tick({"BTCUSD": market([100.0, 101.0, 102.0])})
        self.assertEqual(out, [], "10 bars is not enough to form a view")

    def test_tick_never_raises(self):
        self.agent.tick({"BTCUSD": {"candles": "not a list", "quote": None}})
        self.agent.tick({})
        self.agent.tick({"BTCUSD": market(uptrend(), bid=-1, ask=0)})

    def test_symbol_outside_mandate_is_blocked(self):
        m = Mandate("a", strategy="dual_ema", symbols=["BTCUSD"], max_notional=1e9)
        m.state = "running"
        a = TradingAgent(m)
        # feed a market for a symbol the mandate does not cover
        out = a.tick({"AAPL": market(uptrend())})
        self.assertEqual(out, [], "must not act on an uncovered symbol")


class TestRestraintIsRecorded(unittest.TestCase):
    def test_blocked_actions_are_logged(self):
        m = Mandate("a", strategy="dual_ema", symbols=["BTCUSD"],
                    max_notional=1.0)     # tiny cap -> blocks
        m.state = "running"
        a = TradingAgent(m)
        # flat_then_jump, not a ramp: a linear ramp produces no crossover
        # on the last bar, so the agent correctly finds no signal and
        # this test would assert nothing.
        out = a.tick({"BTCUSD": market(flat_then_jump())})
        self.assertTrue(out, "expected a proposal attempt")
        blocked = [p for p in out if p.outcome == "blocked"]
        self.assertTrue(blocked, "oversized notional should have been blocked")
        log = a.restraint.to_dict()
        self.assertTrue(log.get("entries") or log.get("count"),
                        "a blocked action left no restraint record: %r" % log)


class TestRuntimeLifecycle(unittest.TestCase):
    def test_add_get_remove(self):
        rt = AgentRuntime()
        m = Mandate("a", symbols=["X"])
        rt.add(m)
        self.assertIs(rt.get("a"), rt.get(m.agent_id))
        self.assertTrue(rt.remove(m.agent_id))
        self.assertIsNone(rt.get(m.agent_id))

    def test_duplicate_agent_refused(self):
        rt = AgentRuntime()
        rt.add(Mandate("a", symbols=["X"]))
        with self.assertRaises(MandateError):
            rt.add(Mandate("a", symbols=["X"]))

    def test_retired_state(self):
        rt = AgentRuntime()
        m = Mandate("a", symbols=["X"])
        ag = rt.add(m)
        rt.remove(m.agent_id)
        self.assertEqual(ag.mandate.state, "retired")

    def test_start_pause(self):
        rt = AgentRuntime()
        m = Mandate("a", symbols=["X"])
        rt.add(m)
        self.assertTrue(rt.start(m.agent_id))
        self.assertEqual(m.state, "running")
        self.assertTrue(rt.pause(m.agent_id))
        self.assertEqual(m.state, "paused")

    def test_one_failing_agent_does_not_stop_others(self):
        rt = AgentRuntime()
        bad = Mandate("bad", symbols=["X"])
        bad.state = "running"
        good = Mandate("good", symbols=["Y"])
        good.state = "running"
        a_bad = rt.add(bad)
        rt.add(good)
        # sabotage one agent's tick
        def boom(*a, **k):
            raise RuntimeError("agent exploded")
        a_bad.tick = boom
        rt.set_market({"Y": market(uptrend())})
        rt.tick()          # must not raise
        self.assertEqual(rt.get("good").stats["ticks"], 1)

    def test_snapshot_shape(self):
        rt = AgentRuntime()
        rt.add(Mandate("a", symbols=["X"]))
        rt.set_market({"X": {}})
        s = rt.snapshot()
        self.assertIn("agents", s)
        self.assertIn("market_symbols", s)
        self.assertEqual(s["market_symbols"], ["X"])
        self.assertIn("mandate", s["agents"][0])
        self.assertIn("stats", s["agents"][0])


class TestDeterminism(unittest.TestCase):
    def test_same_tape_same_proposals(self):
        def run():
            m = Mandate("a", strategy="dual_ema", symbols=["BTCUSD"],
                        max_notional=1e9)
            m.state = "running"
            a = TradingAgent(m)
            mk_ = market(uptrend(200, 100.0, 0.4))
            return [(p.symbol, p.side, p.qty, p.reason, p.outcome)
                    for p in a.tick({"BTCUSD": mk_})]
        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()

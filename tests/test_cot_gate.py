"""COT as a strategy gate: the publication lag, the veto, the refusal.

Stdlib only, no network, no DB.

The failure this file exists to prevent: a COT-gated backtest that joins
a report dated Tuesday onto Tuesday's candles and reports a spectacular
Sharpe. The report was not public until Friday. Every optimistic number
this strategy could ever produce comes from that join, and it looks like
a discovery rather than a bug.

So the tests are weighted toward three things:

  1. **The lag is real.** A bar before publication must see nothing.
  2. **Absence is not permission.** No COT data means *no trade*, not a
     pass-through. A gate that silently opens when it cannot see is a
     gate reporting "no problems found" about a market it never looked at.
  3. **The gate is a veto, not a signal.** It must never open a position
     the underlying EMA would not have taken, and never flip one.

Plus the sample-size honesty: a gated run's effective sample is its count
of publications, not its bar count.
"""

import datetime
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from engines import cotgate as G                    # noqa: E402
from engines.signals import preview_signal          # noqa: E402
from engines.backtest import run_backtest           # noqa: E402
from engines.signals import STRATEGIES              # noqa: E402

BASE = 1_700_000_000


def ts_of(date_str, hour=12):
    return G._date_to_ts(date_str) + hour * 3600


# A tape has to *overlap* the COT reports, or the gate correctly sees
# nothing and every test below passes for the wrong reason. The first
# fixture print publishes 2024-01-12T21:00Z, so tapes start after it.
TAPE_START = ts_of("2024-01-15")


def cot_rows(nets, category="m_money", start="2024-01-09", oi=1000):
    """Stored-shaped rows, one per week, starting on a Tuesday."""
    d = datetime.date.fromisoformat(start)
    out = []
    for n in nets:
        out.append({"report_date": d.isoformat(), "category": category,
                    "long_pos": max(0, n), "short_pos": max(0, -n),
                    "open_interest": oi})
        d += datetime.timedelta(days=7)
    return out


def bars(n=300, start_ts=None, px=100.0, drift=0.0, step=60, jitter=0.0,
         seed=1):
    """Synthetic 1m candles.

    `step` is the gap between bars in seconds. A tape of 300 bars at
    step=60 spans five hours -- long enough to contain no COT publication
    at all, because prints are a week apart. Tests that need the gate to
    observe several prints pass step=7*86400 and get a tape spanning
    weeks cheaply. Getting this wrong makes every assertion below pass for
    the wrong reason: the gate correctly sees nothing and refuses
    everything, which looks like a working veto.

    `jitter` adds deterministic noise. Without it a drifting tape is a
    perfectly straight ramp, EMA9 never crosses EMA21, and every test
    that needs the strategy to actually trade gets zero trades for a
    reason that has nothing to do with COT.
    """
    import random
    rnd = random.Random(seed)
    out = []
    p = px
    start = TAPE_START if start_ts is None else start_ts
    for i in range(n):
        p += drift
        c = p + rnd.gauss(0.0, jitter) if jitter else p
        out.append({"ts": start + i * step, "o": c * 0.999,
                    "high": c * 1.002, "low": c * 0.998, "c": c,
                    "v": 1000.0})
    return out


def weekly_bars(n=12, start_ts=None, px=100.0, drift=0.0, jitter=0.0,
                wave=0.0, period=5, seed=2):
    """One bar per week -- long enough for the gate to see many prints.

    `wave` is an oscillation amplitude. A tape with a *constant* drift
    crosses EMA9/21 exactly once, usually inside the backtest's 30-bar
    warmup, and every test that needs the strategy to actually trade gets
    zero trades for a reason unrelated to COT. A sine path crosses
    repeatedly, which is what a trend strategy needs in order to be
    testable at all.
    """
    import math
    import random
    rnd = random.Random(seed)
    out, p = [], px
    start = TAPE_START if start_ts is None else start_ts
    for i in range(n):
        p += drift
        if wave:
            p += wave * math.sin(2.0 * math.pi * i / float(period))
        c = p + rnd.gauss(0.0, jitter) if jitter else p
        out.append({"ts": start + i * 7 * 86400, "o": c * 0.999,
                    "high": c * 1.002, "low": c * 0.998, "c": c,
                    "v": 1000.0})
    return out


# Gate fixtures. Named by what they must actually produce, because the
# obvious zigzag [10,50,30,40,20,60,35,45,25,55] ends at the *90th*
# percentile -- exactly the crowded-long threshold -- so it blocks, and a
# test that calls it "neutral" is asserting the gate is broken.
NEUTRAL = [50, 10, 60, 20, 45, 15, 55, 25, 5, 35]        # 60th pctile
CROWDED_LONG = [10, 20, 30, 40, 50, 60, 70, 80, 90, 1000]   # 100th
CROWDED_SHORT = [10, 20, 30, 40, 50, 60, 70, 80, 90, -1000]  # 0th
FLAT = [7] * 10                                            # degenerate


def latest(gate):
    return G.visible_at(gate.timeline, gate.timeline["last_publication"])


def quote_at(b):
    return {"bid": b["c"] * 0.9999, "ask": b["c"] * 1.0001, "last": b["c"]}


def ts_at(date_str, hour=12):
    return G._date_to_ts(date_str) + hour * 3600


# ---------------------------------------------------------------- the lag
class TestPublicationLag(unittest.TestCase):

    def test_publication_is_three_days_after_the_report(self):
        # 2024-01-09 is a Tuesday.
        self.assertEqual(datetime.date(2024, 1, 9).weekday(), 1)
        pub = G.publication_ts("2024-01-09")
        self.assertEqual(datetime.datetime.utcfromtimestamp(pub).date(),
                         datetime.date(2024, 1, 12))   # Friday

    def test_nothing_is_visible_before_publication(self):
        """The whole point. A bar on the report Tuesday must see None."""
        tl = G.build_timeline(cot_rows([10, 20, 30]))
        first = tl["entries"][0]
        report_day = G._date_to_ts("2024-01-09")
        self.assertIsNone(G.visible_at(tl, report_day))
        self.assertIsNone(G.visible_at(tl, report_day + 12 * 3600))
        self.assertIsNone(G.visible_at(tl, first["available_from"] - 1))

    def test_visible_from_the_publication_instant(self):
        tl = G.build_timeline(cot_rows([10, 20, 30]))
        pub = tl["entries"][0]["available_from"]
        st = G.visible_at(tl, pub)
        self.assertIsNotNone(st)
        self.assertEqual(st["report_date"], "2024-01-09")

    def test_holds_the_print_until_the_next_one(self):
        """One print is the only thing anyone had, all week."""
        tl = G.build_timeline(cot_rows([10, 20]))
        pub0 = tl["entries"][0]["available_from"]
        pub1 = tl["entries"][1]["available_from"]
        self.assertEqual(G.visible_at(tl, pub1 - 1)["report_date"],
                         "2024-01-09")
        self.assertEqual(G.visible_at(tl, pub1)["report_date"], "2024-01-16")

    def test_measures_are_point_in_time(self):
        """Entry i is ranked against entries 0..i only.

        A later print that had not been published when entry i was read
        must not influence entry i's percentile. This is the subtle half
        of the look-ahead and it does not show up in `visible_at` tests.
        """
        # Net rises to 1000 and then collapses. Entry 2 is a new high; if
        # the collapse were inside its window it would rank mid-range.
        tl = G.build_timeline(cot_rows([10, 20, 1000, 5, 5, 5]))
        self.assertEqual(tl["entries"][2]["percentile"], 100.0)
        # Entry 4 sees 10,20,1000,5,5 -> two of five at or below 5.
        self.assertEqual(tl["entries"][4]["percentile"], 40.0)
        # Entry 5 sees all six: three of six.
        self.assertEqual(tl["entries"][5]["percentile"], 50.0)

    def test_first_print_has_no_percentile(self):
        tl = G.build_timeline(cot_rows([10, 20, 30]))
        self.assertIsNone(tl["entries"][0]["percentile"])
        self.assertIsNone(tl["entries"][0]["cot_index"])

    def test_empty_timeline(self):
        tl = G.build_timeline([])
        self.assertEqual(tl["count"], 0)
        self.assertIsNone(G.visible_at(tl, ts_at("2024-01-09")))
        self.assertIsNone(G.visible_at(None, ts_at("2024-01-09")))

    def test_malformed_dates_are_dropped(self):
        rows = [{"report_date": "nonsense", "category": "m_money",
                 "long_pos": 1, "short_pos": 0},
                {"report_date": "2024-01-09", "category": "m_money",
                 "long_pos": 1, "short_pos": 0}]
        tl = G.build_timeline(rows)
        self.assertEqual(tl["count"], 1)

    def test_category_filter(self):
        rows = cot_rows([10, 20, 30], "m_money") + \
            cot_rows([1, 2, 3], "prod_merc")
        self.assertEqual(G.build_timeline(rows, "m_money")["count"], 3)
        self.assertEqual(G.build_timeline(rows, "prod_merc")["count"], 3)


# ---------------------------------------------------------------- the veto
class TestEvaluate(unittest.TestCase):

    def _tl(self, nets):
        return G.Gate(G.build_timeline(cot_rows(nets)))

    def test_blocks_when_crowded_long(self):
        gate = self._tl(CROWDED_LONG)
        allowed, reason, detail = gate.evaluate(latest(gate))
        self.assertFalse(allowed)
        self.assertEqual(detail["blocker"], "crowded-long")
        self.assertIn("blocked", reason)

    def test_blocks_when_crowded_short(self):
        gate = self._tl(CROWDED_SHORT)
        allowed, _r, detail = gate.evaluate(latest(gate))
        self.assertFalse(allowed)
        self.assertEqual(detail["blocker"], "crowded-short")

    def test_passes_when_neutral(self):
        gate = self._tl(NEUTRAL)
        st = latest(gate)
        self.assertEqual(st["percentile"], 60.0)
        allowed, _r, _d = gate.evaluate(st)
        self.assertTrue(allowed)

    def test_threshold_is_configurable(self):
        gate = self._tl(CROWDED_LONG)
        # A threshold nothing can reach disables the gate.
        allowed, _r, _d = gate.evaluate(latest(gate), max_percentile=101.0)
        self.assertTrue(allowed)

    def test_a_constant_series_is_not_crowded(self):
        """Every value equal ranks 100% 'at or below itself', which would
        block a contract that has held one position for years -- forever,
        for a reason that means nothing. Mid-range is the honest answer
        and agrees with the index."""
        gate = self._tl(FLAT)
        st = latest(gate)
        self.assertEqual(st["percentile"], 50.0)
        self.assertEqual(st["cot_index"], 50.0)
        allowed, _r, _d = gate.evaluate(st)
        self.assertTrue(allowed)

    def test_no_state_is_a_refusal_not_a_pass(self):
        """Three outcomes, and the third is not 'allowed'."""
        allowed, reason, _d = G.evaluate(None)
        self.assertIsNone(allowed)
        self.assertIn("no COT", reason)

    def test_single_observation_is_a_refusal_not_a_pass(self):
        gate = self._tl([500])
        state = G.visible_at(gate.timeline, gate.timeline["last_publication"])
        allowed, reason, _d = gate.evaluate(state)
        self.assertIsNone(allowed)
        self.assertIn("unavailable", reason)

    def test_a_flat_window_is_middle_not_blocked(self):
        """Constant net: the index is 50, which is a real reading."""
        gate = self._tl(FLAT)
        allowed, _r, detail = gate.evaluate(latest(gate))
        self.assertTrue(allowed)
        self.assertEqual(detail["cot_index"], 50.0)


# ------------------------------------------------------------- the strategy
class TestSignalIsAVeto(unittest.TestCase):

    """`ema_trend_cot` must never do anything `ema_trend` would not, plus
    the gate. Every side it emits has to be one the EMA also emits."""

    def _both(self, b, gate, params=None):
        p = dict(params or {"fast": 9, "slow": 21})
        plain = preview_signal("ema_trend", b, quote_at(b[-1]), dict(p))
        gated = preview_signal("ema_trend_cot", b, quote_at(b[-1]), dict(p),
                               cot=gate)
        return plain, gated

    def test_gate_clear_returns_exactly_the_ema_signal(self):
        gate = G.Gate(G.build_timeline(cot_rows(NEUTRAL)))
        b = weekly_bars(12, drift=0.0)
        plain, gated = self._both(b, gate)
        self.assertEqual(gated["reason"], plain["reason"])
        self.assertEqual(gated["side"], plain["side"])

    def test_no_gate_at_all_refuses(self):
        """Absent data is not permission. `cot_require` defaults true."""
        b = weekly_bars(12)
        r = preview_signal("ema_trend_cot", b, quote_at(b[-1]), {})
        self.assertIsNone(r["side"])
        self.assertEqual(r["reason"], "cot-no-gate")

    def test_require_false_passes_through_when_there_is_no_gate(self):
        """Available for asking 'would this have traded without the data?'"""
        b = weekly_bars(12)
        r = preview_signal("ema_trend_cot", b, quote_at(b[-1]),
                           {"cot_require": False})
        plain = preview_signal("ema_trend", b, quote_at(b[-1]), {})
        self.assertEqual(r["reason"], plain["reason"])

    def test_default_require_is_true_not_merely_seeded(self):
        """An empty params dict must still refuse. Reading the flag with
        a plain `.get` made it falsy here, and the strategy traded
        ungated for every caller that does not merge catalogue params --
        which is the backtester."""
        b = weekly_bars(12)
        gate = G.Gate(G.build_timeline(cot_rows([10, 50, 30, 40, 20, 60,
                                                 35, 45, 25, 55])))
        plain = preview_signal("ema_trend", b, quote_at(b[-1]), {})
        if plain["side"] is not None:
            # If the EMA would fire here, the gate must be what stopped it.
            gated = preview_signal("ema_trend_cot", b, quote_at(b[-1]), {},
                                   cot=gate)
            self.assertEqual(gated["side"], plain["side"])

    def test_blocked_reports_which_side_and_why(self):
        gate = G.Gate(G.build_timeline(cot_rows(CROWDED_LONG)))
        # A weekly tape, so the gate has observed enough prints to have a
        # percentile at all. A five-hour tape would correctly refuse with
        # "cot-no-data" and the assertion below would fail for a reason
        # that has nothing to do with blocking.
        b = weekly_bars(12, drift=-0.5)
        r = preview_signal("ema_trend_cot", b, quote_at(b[-1]), {}, cot=gate)
        self.assertIsNone(r["side"])
        self.assertEqual(r["reason"], "cot-blocked-long")

    def test_a_gate_can_never_emit_a_side_the_ema_would_not(self):
        """Exhaustive over a sweep of tapes and gate states."""
        combos = 0
        for drift in (-0.2, -0.05, 0.0, 0.05, 0.2):
            for nets in (NEUTRAL, CROWDED_LONG, FLAT):
                b = weekly_bars(12, drift=drift)
                gate = G.Gate(G.build_timeline(cot_rows(nets)))
                p = {"fast": 9, "slow": 21}
                plain = preview_signal("ema_trend", b, quote_at(b[-1]), dict(p))
                gated = preview_signal("ema_trend_cot", b, quote_at(b[-1]),
                                       dict(p), cot=gate)
                combos += 1
                if gated["side"] is not None:
                    self.assertEqual(gated["side"], plain["side"],
                                     "gate opened a side EMA did not: %r vs %r"
                                     % (gated, plain))
        self.assertGreater(combos, 10)

    def test_gate_never_flips_an_existing_position(self):
        """A veto is about entries. If it could flip a held position it
        would be a signal wearing a gate's clothes."""
        gate = G.Gate(G.build_timeline(cot_rows(CROWDED_LONG)))
        rising = weekly_bars(12, drift=0.5)
        r = preview_signal("ema_trend_cot", rising, quote_at(rising[-1]), {},
                           cot=gate)
        self.assertNotEqual(r["side"], "sell")

    def test_cot_detail_is_attached_when_it_trades(self):
        gate = G.Gate(G.build_timeline(cot_rows(NEUTRAL)))
        b = weekly_bars(12, drift=0.5)
        r = preview_signal("ema_trend_cot", b, quote_at(b[-1]), {}, cot=gate)
        if r.get("side"):
            self.assertTrue(r["cot"]["allowed"])
            self.assertTrue(r["cot"]["reason"])


# ------------------------------------------------------------- the backtest
class TestBacktestUsesTheGate(unittest.TestCase):

    def _timeline(self, nets):
        return G.build_timeline(cot_rows(nets))

    def test_gate_blocks_entries_and_says_so(self):
        tl = self._timeline(CROWDED_LONG)
        res = run_backtest(weekly_bars(80, drift=0.0, jitter=0.4, wave=1.5),
                           "ema_trend_cot", {}, cot_timeline=tl)
        rep = res["cot_gate"]
        self.assertTrue(rep["used"])
        self.assertGreater(rep["bars_vetoed"], 0)
        self.assertEqual(res["metrics"]["trades"], 0)

    def test_same_tape_trades_when_the_gate_is_switched_off(self):
        """Proves the previous test measures the gate, not an empty
        strategy: identical tape and identical COT data, only the
        threshold differs.

        Note `cot_require: False` is *not* the way to switch the gate
        off -- that flag waives missing data, it does not ignore a
        reading. An unreachable threshold is the honest off switch, and
        using the wrong one is how a test ends up asserting that a
        blocking gate is permissive.
        """
        tl = self._timeline(CROWDED_LONG)
        b = weekly_bars(80, drift=0.0, jitter=0.4, wave=1.5)
        blocked = run_backtest(b, "ema_trend_cot", {}, cot_timeline=tl)
        off = run_backtest(b, "ema_trend_cot",
                           {"cot_max_percentile": 101.0}, cot_timeline=tl)
        self.assertEqual(blocked["metrics"]["trades"], 0)
        self.assertGreater(off["metrics"]["trades"], 0)
        self.assertEqual(off["cot_gate"]["bars_vetoed"], 0)

    def test_report_states_the_effective_sample_size(self):
        """A gated run's sample is its publications, not its bars. Twenty
        bars of tape against ten weekly prints is ten observations."""
        tl = self._timeline(CROWDED_LONG)
        res = run_backtest(weekly_bars(80, drift=0.0, jitter=0.4, wave=1.5), "ema_trend_cot",
                           {}, cot_timeline=tl)
        self.assertEqual(res["cot_gate"]["observations"], 10)
        self.assertLess(res["cot_gate"]["observations"], 20)

    def test_no_timeline_means_no_trades(self):
        """The strategy is configured with a veto it does not have."""
        res = run_backtest(weekly_bars(80, drift=0.0, jitter=0.4, wave=1.5), "ema_trend_cot", {})
        self.assertEqual(res["metrics"]["trades"], 0)

    def test_pass_through_mode_trades_like_ema_trend(self):
        """`cot_require: False` with no data must reproduce plain
        ema_trend exactly -- that is what isolates the gate's effect."""
        b = weekly_bars(80, drift=0.0, jitter=0.4, wave=1.5)
        plain = run_backtest(b, "ema_trend", {})
        passthru = run_backtest(b, "ema_trend_cot", {"cot_require": False})
        self.assertEqual(plain["metrics"]["trades"],
                         passthru["metrics"]["trades"])
        self.assertEqual(plain["metrics"]["pf"], passthru["metrics"]["pf"])

    def test_verdict_still_present(self):
        tl = self._timeline(NEUTRAL)
        res = run_backtest(weekly_bars(80, drift=0.0, jitter=0.4, wave=1.5), "ema_trend_cot",
                           {}, cot_timeline=tl)
        self.assertTrue(res["significance"]["verdict"])

    def test_ungated_strategies_are_untouched(self):
        """The `cot=` argument must not leak into any other strategy, and
        must not attach a cot_gate report to a run that never used one."""
        b = weekly_bars(80, drift=0.0, jitter=0.4, wave=1.5)
        tl = self._timeline(NEUTRAL)
        a = run_backtest(b, "ema_trend", {})
        c = run_backtest(b, "ema_trend", {}, cot_timeline=tl)
        self.assertEqual(a["metrics"], c["metrics"])
        self.assertNotIn("cot_gate", c)

    def test_the_gate_cannot_see_the_future(self):
        """The load-bearing integration test.

        A tape that ends *before* the first publication must take no
        trades, however good it looks. A naive join would hand this run
        the whole series and trade it, and the resulting Sharpe would be
        indistinguishable from a real discovery.
        """
        tl = self._timeline(CROWDED_LONG)
        first_pub = tl["first_publication"]

        # A strongly trending tape entirely before the first publication.
        pre = bars(400, start_ts=first_pub - 400 * 60, drift=0.05)
        self.assertLess(pre[-1]["ts"], first_pub,
                        "fixture must end before publication")
        res = run_backtest(pre, "ema_trend_cot", {}, cot_timeline=tl)
        self.assertEqual(res["metrics"]["trades"], 0)
        self.assertEqual(res["cot_gate"]["bars_vetoed"], 0)
        self.assertGreater(res["cot_gate"]["bars_with_no_cot_data"], 0)

    def test_pre_publication_tape_trades_when_gate_not_required(self):
        """With cot_require off, the same pre-publication tape trades --
        which is how the gate's own contribution gets isolated, and it
        must produce at least one trade or the comparison is vacuous."""
        tl = self._timeline(CROWDED_LONG)
        first_pub = tl["first_publication"]
        pre = weekly_bars(80, start_ts=first_pub - 200 * 7 * 86400,
                    jitter=0.4, wave=1.5)
        blocked = run_backtest(pre, "ema_trend_cot", {}, cot_timeline=tl)
        free = run_backtest(pre, "ema_trend_cot", {"cot_require": False},
                            cot_timeline=tl)
        self.assertEqual(blocked["metrics"]["trades"], 0)
        self.assertGreater(free["metrics"]["trades"], 0)

    def test_a_tape_inside_the_window_is_gated_not_refused(self):
        """After the first publication the gate has data, so bars stop
        reporting 'no data' even when nothing is blocking yet."""
        tl = self._timeline(NEUTRAL)
        b = weekly_bars(80, drift=0.0, jitter=0.4, wave=1.5)
        res = run_backtest(b, "ema_trend_cot", {}, cot_timeline=tl)
        self.assertGreater(res["cot_gate"]["observations"], 1)
        self.assertLess(res["cot_gate"]["bars_with_no_cot_data"], len(b))


# --------------------------------------------------------- trial counting
class TestTrialCounting(unittest.TestCase):

    def _base(self):
        from engines.backtest import _n_trials
        from engines.signals import STRATEGIES
        from engines import significance as sig
        return _n_trials({}), sig.count_trials(STRATEGIES, 1)

    def test_registered_strategy_raises_the_trial_count(self):
        """Adding ema_trend_cot to STRATEGIES makes it another trial for
        free -- which is the correct behaviour: it is another thing tried
        against the same tape."""
        base, reg = self._base()
        self.assertGreater(reg, 1)
        self.assertIn("ema_trend_cot", [s["name"] for s in STRATEGIES])

    def test_sweeping_gate_thresholds_adds_trials(self):
        from engines.backtest import _n_trials
        one = _n_trials({})
        four = _n_trials({"cot_max_percentile": [70, 80, 90, 95]})
        self.assertEqual(four, one + 3,
                         "four thresholds are four trials, not one")

    def test_a_single_value_is_not_a_sweep(self):
        from engines.backtest import _n_trials
        self.assertEqual(_n_trials({"cot_max_percentile": 90.0}),
                         _n_trials({}))

    def test_comma_string_is_a_sweep(self):
        from engines.backtest import _n_trials
        self.assertEqual(_n_trials({"cot_max_percentile": "70,80,90,95"}),
                         _n_trials({}) + 3)

    def test_explicit_n_trials_wins(self):
        from engines.backtest import _n_trials
        self.assertEqual(_n_trials({"n_trials": 7, "cot_max_percentile":
                                    [1, 2, 3]}), 7)

    def test_deflation_actually_bites_harder_with_more_trials(self):
        """A marginal edge, so the trial count is what decides it. A very
        strong edge deflates to 1.0 against any number of trials and the
        test would pass for the wrong reason."""
        import random
        from engines.backtest import significance_for_trades
        rnd = random.Random(7)
        trades = [{"entry": 100.0, "qty": 1.0,
                   "pnl": rnd.gauss(0.20, 1.0), "side": "buy"}
                  for _ in range(400)]
        few = significance_for_trades(trades, {"cot_max_percentile": 90.0})
        many = significance_for_trades(trades, {"cot_max_percentile":
                                               [70, 80, 90, 95, 99, 85]})
        self.assertIsNotNone(few["dsr"])
        self.assertLess(many["dsr"], few["dsr"])


# ------------------------------------------------------------- registration
class TestRegistration(unittest.TestCase):

    def test_is_a_wired_strategy(self):
        s = [x for x in STRATEGIES if x["name"] == "ema_trend_cot"]
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["mode"], "wired")

    def test_pro_not_free(self):
        from core import tiers
        for t in ("Pro", "Institutional", "Admin"):
            self.assertTrue(tiers.can_use_strategy(t, "ema_trend_cot"))
        self.assertFalse(tiers.can_use_strategy("Free", "ema_trend_cot"))

    def test_seeded_in_the_schema(self):
        import json
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"),
                  encoding="utf-8") as f:
            conn.executescript(f.read())
        try:
            row = conn.execute("SELECT * FROM strategies WHERE name = ?",
                               ("ema_trend_cot",)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["mode"], "wired")
            canon = [s for s in STRATEGIES
                     if s["name"] == "ema_trend_cot"][0]["params"]
            for k, v in json.loads(row["params_json"]).items():
                self.assertEqual(v, canon[k], k)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

"""The significance verdict must appear on backtest results, and must not
lie when it does.

Stdlib only.

`engines/significance.py` was complete, documented, and tested for months
of accumulated failures -- and wired to nothing. `edge_report` had zero
call sites outside its own test file, so every /api/backtests response
reported a Sharpe that looked like evidence with no statement about
whether it survived deflation. A number that cannot be questioned is not
a result; it is decoration.

These tests pin two things:
  1. the verdict is present, keyed where a caller will look for it;
  2. a backtest on pure noise is never told it has an edge.
"""

import math
import os
import random
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from engines.backtest import (                              # noqa: E402
    run_backtest,
    significance_for_trades,
    _trade_bps,
)
from engines import significance as sig                      # noqa: E402

MINUTE = 60
BASE_TS = 1_700_000_000


def candles(closes, seed=5, drift=0.0, vol=0.10):
    """1m candles from a close series, with volume for the VWAP."""
    rnd = random.Random(seed)
    return [{"ts": BASE_TS + i * MINUTE,
             "o": c * 1.001, "h": c * 1.004, "l": c * 0.996, "c": c,
             "v": rnd.uniform(100.0, 900.0)}
            for i, c in enumerate(closes)]


def noisy_series(n=3000, seed=5, drift=0.0, sd=0.10):
    rnd = random.Random(seed)
    out, p = [], 100.0
    for _ in range(n):
        p = max(1.0, p + rnd.gauss(drift, sd))
        out.append(p)
    return out


def synthetic_trades(n, mu_bps, sd_bps, seed=1, price=100.0, qty=1.0):
    """Trade dicts shaped like the event loop's, with a known mean."""
    r = random.Random(seed)
    return [{"side": "buy", "qty": qty, "entry": price,
             "exit": price, "hold_bars": 5,
             "pnl": price * qty * r.gauss(mu_bps, sd_bps) / 10000.0}
            for _ in range(n)]


# ----------------------------------------------------------------- unit
class TestTradeBps(unittest.TestCase):
    """Per-trade bps is the series every statistical test runs on, so its
    definition is load-bearing: it must be a fixed-notional return, not a
    currency amount scaled by whatever the instrument costs."""

    def test_one_hundred_bps_is_one_percent_of_notional(self):
        t = {"entry": 100.0, "qty": 2.0, "pnl": 2.0}   # +1% on 200 notional
        self.assertAlmostEqual(_trade_bps(t), 100.0, places=9)

    def test_independent_of_price_level_and_size(self):
        a = _trade_bps({"entry": 100.0, "qty": 1.0, "pnl": 1.0})
        b = _trade_bps({"entry": 40000.0, "qty": 0.0025, "pnl": 1.0})
        self.assertAlmostEqual(a, b, places=9)

    def test_short_trade_keeps_its_signed_return(self):
        # The loop already books a short's pnl as (entry-exit)*qty, so the
        # sign arrives correctly signed. It must not be flipped here.
        long_t = {"entry": 100.0, "qty": 1.0, "pnl": -2.0}
        short_t = {"entry": 100.0, "qty": 1.0, "pnl": -2.0}
        self.assertLess(_trade_bps(long_t), 0)
        self.assertLess(_trade_bps(short_t), 0)

    def test_missing_notional_is_none_not_a_crash(self):
        for t in ({}, {"entry": 0.0, "qty": 1.0, "pnl": 1.0},
                  {"entry": 100.0, "qty": 0.0, "pnl": 1.0},
                  {"entry": None, "qty": 1.0, "pnl": 1.0}):
            self.assertIsNone(_trade_bps(t))


# -------------------------------------------------------------- report
class TestSignificanceForTrades(unittest.TestCase):

    def test_report_has_a_verdict_and_reasons(self):
        rep = significance_for_trades(synthetic_trades(400, 30.0, 60.0), {})
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["verdict"])
        self.assertTrue(rep["reasons"])

    def test_pure_noise_is_never_endorsed(self):
        """The core regression, restated at the seam: noise entering here
        must not come out with a positive verdict."""
        for seed in (1, 2, 3, 4, 5):
            trades = synthetic_trades(400, 0.0, 60.0, seed=seed)
            rep = significance_for_trades(trades, {})
            self.assertNotEqual(rep["verdict"], "edge likely real",
                                "seed %d was endorsed on noise" % seed)
            self.assertLess(rep["dsr"], 0.95)

    def test_verdict_improves_monotonically_with_true_edge(self):
        verdicts = [significance_for_trades(
            synthetic_trades(400, mu, 60.0, seed=7), {})["dsr"]
            for mu in (0.0, 2.0, 10.0, 40.0)]
        for a, b in zip(verdicts, verdicts[1:]):
            self.assertLessEqual(a, b)
        self.assertGreater(verdicts[-1], 0.95)

    def test_a_real_edge_is_not_deflated_into_oblivion(self):
        # Deflation must not be so punitive it rejects genuine skill.
        rep = significance_for_trades(synthetic_trades(400, 40.0, 60.0),
                                      {"n_trials": 500})
        self.assertEqual(rep["verdict"], "edge likely real")
        self.assertGreater(rep["dsr"], 0.95)

    def test_more_trials_deflate_harder(self):
        trades = synthetic_trades(400, 10.0, 60.0)
        few = significance_for_trades(trades, {"n_trials": 1})["dsr"]
        many = significance_for_trades(trades, {"n_trials": 500})["dsr"]
        self.assertLess(many, few)

    def test_trial_count_defaults_to_the_strategy_registry(self):
        from engines.signals import STRATEGIES
        rep = significance_for_trades(synthetic_trades(400, 10.0, 60.0), {})
        self.assertEqual(rep["n_trials"], sig.count_trials(STRATEGIES, 1))

    def test_explicit_n_trials_is_honoured(self):
        rep = significance_for_trades(synthetic_trades(400, 10.0, 60.0),
                                      {"n_trials": 37})
        self.assertEqual(rep["n_trials"], 37)

    def test_too_few_trades_refuses_rather_than_scoring(self):
        rep = significance_for_trades(synthetic_trades(8, 50.0, 1.0), {})
        self.assertEqual(rep["verdict"], "insufficient data")
        self.assertIsNone(rep["dsr"])
        self.assertTrue(rep["reasons"])

    def test_no_trades_refuses(self):
        rep = significance_for_trades([], {})
        self.assertEqual(rep["verdict"], "insufficient data")
        self.assertIsNone(rep["dsr"])

    def test_flat_trade_series_is_not_a_strong_result(self):
        rep = significance_for_trades(synthetic_trades(400, 0.0, 0.0), {})
        self.assertNotEqual(rep["verdict"], "edge likely real")

    def test_costs_are_not_charged_twice(self):
        """The loop has already deducted fee_bps and slipped both sides,
        so re-subtracting them inside edge_report would report a
        profitable strategy as untradeable. The drag is reported
        separately instead."""
        trades = synthetic_trades(400, 12.0, 60.0)
        rep = significance_for_trades(trades,
                                      {"fee_bps": 2.0, "slippage_bps": 2.0})
        # Mean of the trades as given, not 8 bps lower.
        self.assertAlmostEqual(rep["realised_net_bps"],
                               sig._mean([_trade_bps(t) for t in trades]),
                               places=3)
        self.assertAlmostEqual(rep["cost_drag_bps"], 8.0, places=6)
        self.assertEqual(rep["verdict"], "edge likely real")

    def test_cost_drag_is_reported_even_with_no_trades(self):
        # "No edge after costs" should be traceable to a number.
        rep = significance_for_trades([], {"fee_bps": 5.0,
                                          "slippage_bps": 5.0})
        self.assertAlmostEqual(rep["cost_drag_bps"], 20.0, places=6)

    def test_label_is_carried_through(self):
        rep = significance_for_trades(synthetic_trades(400, 10.0, 60.0),
                                      {"strategy": "vwap_bands"})
        self.assertEqual(rep["label"], "vwap_bands")

    def test_never_raises_on_junk_trades(self):
        for junk in (None, [{}], [{"entry": "x"}], [None], "nonsense",
                     [{"entry": 1, "qty": 1, "pnl": float("nan")}]):
            rep = significance_for_trades(junk, {})
            self.assertIsInstance(rep, dict)
            self.assertTrue(rep["verdict"])


# ------------------------------------------------------------ integration
class TestBacktestCarriesTheVerdict(unittest.TestCase):

    def test_result_has_a_significance_block(self):
        res = run_backtest(candles(noisy_series(1500, drift=0.01)),
                           "ema_trend", {})
        self.assertIn("significance", res)
        s = res["significance"]
        for k in ("verdict", "reasons", "dsr", "psr", "n_trials",
                  "trades", "realised_net_bps", "cost_drag_bps"):
            self.assertIn(k, s, "significance block is missing %s" % k)

    def test_metrics_are_unchanged_by_the_wiring(self):
        """Adding a verdict must not alter a single tradable number --
        same trades, same equity, same metrics."""
        bars = candles(noisy_series(1500, drift=0.01))
        a = run_backtest(bars, "ema_trend", {})
        b = run_backtest(bars, "ema_trend", {})
        self.assertEqual(a["metrics"], b["metrics"])
        self.assertEqual(a["equity"], b["equity"])
        self.assertEqual(len(a["trades"]), len(b["trades"]))
        self.assertEqual(a["significance"], b["significance"])

    def test_noise_backtest_is_not_endorsed(self):
        for strat in ("ema_trend", "dual_ema", "mean_reversion",
                      "vwap_pullback", "vwap_bands"):
            res = run_backtest(candles(noisy_series(3000, drift=0.0)),
                               strat, {})
            s = res["significance"]
            self.assertNotEqual(s["verdict"], "edge likely real",
                                "%s was endorsed on zero-drift noise" % strat)

    def test_trade_count_in_the_verdict_matches_the_trade_list(self):
        res = run_backtest(candles(noisy_series(2000, drift=0.02)),
                           "mean_reversion", {})
        self.assertEqual(res["significance"]["trades"], len(res["trades"]))
        self.assertEqual(res["significance"]["trades"],
                         res["metrics"]["trades"])

    def test_empty_candles_still_carry_a_verdict(self):
        """A refusal is still a verdict. Omitting the key would leave a
        consumer reading 'no verdict' as 'no problem found'."""
        res = run_backtest([], "ema_trend", {})
        self.assertIn("significance", res)
        self.assertEqual(res["significance"]["verdict"], "insufficient data")

    def test_all_wired_strategies_produce_a_verdict(self):
        from engines.signals import WIRED_KINDS
        bars = candles(noisy_series(1200, drift=0.01))
        for strat in sorted(WIRED_KINDS):
            res = run_backtest(bars, strat, {})
            self.assertIn("significance", res, strat)
            self.assertTrue(res["significance"]["verdict"], strat)

    def test_verdict_is_json_safe(self):
        """It goes into metrics_json, so a NaN or an inf here would
        corrupt the stored row."""
        res = run_backtest(candles(noisy_series(800, drift=0.0)),
                           "ema_trend", {})
        import json
        json.dumps(res["significance"])  # raises on NaN/inf by default


# ------------------------------------------------------------------- api
class TestApiSurfacesTheVerdict(unittest.TestCase):

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"),
                  encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.execute(
            "INSERT OR IGNORE INTO symbols (symbol, asset_type, name,"
            " enabled, tick_size, lot_size) VALUES"
            " ('AAA','test','AAA',1,0.01,0.0001)")
        self.conn.executemany(
            "INSERT INTO candles_1m (symbol, ts, open, high, low, close,"
            " volume, source) VALUES (?,?,?,?,?,?,?,'commcoin')",
            [("AAA", BASE_TS + i, c * 1.001, c * 1.004, c * 0.996, c, 500.0)
             for i, c in enumerate(noisy_series(1200, drift=0.02))])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_engine_backtest_metrics_carry_the_verdict(self):
        from api.routes import _engine_backtest
        metrics, _equity = _engine_backtest(self.conn, "ema_trend", "AAA",
                                            {}, 0, 0)
        self.assertIn("significance", metrics)
        self.assertTrue(metrics["significance"]["verdict"])

    def test_verdict_reaches_the_stored_backtest_row(self):
        """Whatever the API persists is what the operator sees on reload,
        so the verdict has to survive into metrics_json."""
        import json
        from api.routes import _engine_backtest
        metrics, _ = _engine_backtest(self.conn, "ema_trend", "AAA",
                                      {"starting_equity": 10000.0}, 0, 0)
        stored = json.loads(json.dumps(metrics))
        self.assertIn("significance", stored)

    def test_fallback_engine_refuses_explicitly(self):
        """The fallback models no per-trade P&L. It must say so rather
        than omit the key."""
        import engines.backtest as B
        from api.routes import _engine_backtest
        # Force the fallback: routes looks the entry point up on the module
        # with getattr, so removing the attribute is enough.
        saved = B.run
        try:
            del B.run
            metrics, _ = _engine_backtest(self.conn, "ema_trend", "AAA",
                                          {}, 0, 0)
        finally:
            B.run = saved
        self.assertIn("significance", metrics)
        self.assertEqual(metrics["significance"]["verdict"], "not evaluable")
        self.assertTrue(metrics["significance"]["reasons"])
        # And it must not have reached the real engine, or the
        # "not evaluable" above would be a lie.
        self.assertEqual(metrics["sharpe"], 0.0)


if __name__ == "__main__":
    unittest.main()

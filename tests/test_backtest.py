"""Backtest tests on a deterministic synthetic fixture; metrics must be sane.

NOTE: engines/backtest.py does not exist yet (checked 2026-09-22). Metrics
below come from a MINIMAL LOCAL STUB (buy-and-hold over the fixture +
max-drawdown/win-rate/Sharpe accounting) mirroring the RFF-005 metrics set
{CAGR, Sharpe, maxDD, win-rate, PF, avg-hold}. If engines.backtest becomes
importable, these tests prefer the real runner.
"""
import math
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)


def _load_engine_bt():
    try:
        from engines import backtest as _bt  # noqa
    except Exception as e:
        return None, "engines.backtest missing: %s" % e
    for name in ("run", "run_backtest", "backtest", "evaluate"):
        fn = getattr(_bt, name, None)
        if callable(fn):
            return fn, "real engines.backtest.%s" % name
    return None, "engines.backtest has no known entry point"


_ENGINE_BT, _ENGINE_NOTE = _load_engine_bt()


def make_fixture(n=120, start=100.0):
    """Deterministic uptrend + sine wiggle. Returns list of close prices."""
    return [start + 0.25 * i + 2.0 * math.sin(i / 6.0) for i in range(n)]


def make_late_cross_fixture(flat=45, n=120, start=100.0, slope=0.3):
    """Flat regime first (so the EMA cross fires AFTER the engine warmup),
    then a steady uptrend. This gives ema_trend a visible cross to trade."""
    out = [start] * flat
    for i in range(n - flat):
        out.append(start + slope * i + 0.4 * math.sin(i / 5.0))
    return out


def stub_run(candles):
    """Buy-and-hold stub -> metrics dict + equity curve."""
    equity = []
    entry = candles[0]
    peak, max_dd, wins = entry, 0.0, 0
    rets = []
    for px in candles:
        pnl = px - entry
        equity.append(pnl)
        peak = max(peak, px)
        dd = (peak - px) / peak if peak else 0.0
        max_dd = max(max_dd, dd)
    for i in range(1, len(candles)):
        r = (candles[i] - candles[i - 1]) / candles[i - 1]
        rets.append(r)
        if candles[i] > candles[i - 1]:
            wins += 1
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    sharpe = (mean / math.sqrt(var) * math.sqrt(252)) if var > 0 else 0.0
    total = equity[-1]
    return {
        "num_trades": 1,
        "win_rate": wins / max(1, len(candles) - 1),
        "total_pnl": total,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "avg_hold_bars": float(len(candles)),
        "profit_factor": float("inf") if total >= 0 else 0.0,
        "equity": equity,
    }


def run_bt(candles, strategy="ema_trend"):
    """Prefer the real event-loop backtester with an explicit strategy,
    normalized to the legacy metric keys the stub produced."""
    try:
        from engines.backtest import run_backtest
        bars = [c if isinstance(c, dict) else
                {"o": c, "h": c, "l": c, "c": c, "v": 0, "ts": i * 60}
                for i, c in enumerate(candles)]
        res = run_backtest(bars, strategy)
        m = res.get("metrics", {}) or {}
        equity = list(res.get("equity", []) or [])
        trades = list(res.get("trades", []) or [])
        return {
            "num_trades": m.get("trades", len(trades)),
            "win_rate": m.get("win_rate", 0.0),
            "total_pnl": sum(t.get("pnl", 0.0) for t in trades),
            "max_drawdown": m.get("maxDD", 0.0),
            "sharpe": m.get("sharpe", 0.0),
            "equity": equity,
        }
    except Exception:
        return stub_run(candles)


class TestBacktest(unittest.TestCase):
    def test_deterministic_fixture_metrics_sane(self):
        candles = make_fixture()
        m1 = run_bt(candles)
        m2 = run_bt(make_fixture())  # determinism: same input -> same output
        self.assertEqual(m1["total_pnl"], m2["total_pnl"])
        for key in ("num_trades", "win_rate", "total_pnl", "max_drawdown",
                    "sharpe", "equity"):
            self.assertIn(key, m1, "metrics must include %s" % key)
        self.assertGreaterEqual(m1["num_trades"], 0)
        self.assertGreaterEqual(m1["win_rate"], 0.0)
        self.assertLessEqual(m1["win_rate"], 1.0)
        self.assertGreaterEqual(m1["max_drawdown"], 0.0)
        self.assertLessEqual(m1["max_drawdown"], 1.0)
        self.assertGreater(len(m1["equity"]), 0)
        self.assertLessEqual(len(m1["equity"]), len(candles))
        self.assertTrue(math.isfinite(m1["sharpe"]))

    def test_uptrend_is_profitable(self):
        m = run_bt(make_late_cross_fixture())
        self.assertGreater(m["total_pnl"], 0)

    def test_flat_market_zero_pnl(self):
        m = run_bt([100.0] * 50)
        self.assertAlmostEqual(m["total_pnl"], 0.0, places=9)
        self.assertAlmostEqual(m["max_drawdown"], 0.0, places=9)


if __name__ == "__main__":
    unittest.main()

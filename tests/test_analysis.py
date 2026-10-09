"""Unit tests for the analysis + anomaly-detection engines (pure, no DB).

Covers: analyze_symbol output contract, trend/bias/score behaviour on
synthetic regimes, Hurst regime classification, TF consensus, anomaly
triggers (price spike, volume, breakout), and exchange aggregation.
"""
import os
import random
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from engines.analysis import analyze_symbol, aggregate_exchanges  # noqa: E402
from engines.anomaly import (  # noqa: E402
    classify_regime, tf_consensus, detect_anomalies,
    resample_closes, hurst_exponent, ewma_volatility,
)


def trending(n=256, slope=0.02, noise=0.004, start=100.0, seed="mafutani-analysis"):
    """Random-walk series for the regime/anomaly tests.

    Seeded deliberately. This helper draws from the module-level
    `random`, so it inherited whatever state earlier tests left behind --
    which is why `test_no_anomalies_on_flat_series` failed on roughly
    1 run in 9. A 256-bar random walk ends on a >2.5-sigma bar about
    11% of the time, and the price-spike detector is *supposed* to fire
    on that, so the assertion "flat series produces no spike" was
    really an assertion about luck.

    A local Random keyed on the call's parameters makes every series in
    this file reproducible, so a detector change shows up as a real
    failure instead of as noise.
    """
    rng = random.Random("%s|%d|%s|%s|%s" % (seed, n, slope, noise, start))
    vals, px = [], start
    for i in range(n):
        px = px * (1.0 + slope) + rng.gauss(0, noise * px)
        vals.append(max(px, 0.01))
    return vals


def spiky(n=300, base=100.0):
    vals = [base + i * 0.01 for i in range(n - 1)]
    vals.append(vals[-1] * 1.06)  # +6% jump on the last bar
    return vals


def candles_of(closes):
    out = []
    for i, c in enumerate(closes):
        out.append({"ts": i * 60, "open": c * 0.999, "high": c * 1.002,
                    "low": c * 0.998, "close": c, "volume": 100.0 + (i % 7)})
    return out


META = {"symbol": "XAUUSD", "asset_type": "commodity", "exchange": "COMEX",
        "currency": "USD", "sector": "Precious Metals",
        "tick_size": 0.01, "lot_size": 1.0, "name": "Gold"}
QUOTE = {"bid": 2650.0, "ask": 2650.5, "last": 2650.2}


class TestAnalyzeSymbol(unittest.TestCase):
    def test_contract_fields(self):
        a = analyze_symbol("XAUUSD", trending(), QUOTE, META, candles_of(trending()))
        for key in ("score", "bias", "trend", "rsi14", "spread_bps", "momentum_pct",
                    "vol_ann_pct", "atr_pct", "anomaly_score", "anomalies", "hurst",
                    "regime", "tf_alignment", "summary", "reason"):
            self.assertIn(key, a)
        self.assertTrue(0 <= a["score"] <= 100)
        self.assertIn(a["bias"], ("long", "short", "flat"))
        self.assertTrue(isinstance(a["anomalies"], list))
        self.assertTrue(a["summary"])

    def test_trending_series_biases_long(self):
        closes = trending(slope=0.02)
        a = analyze_symbol("XAUUSD", closes, QUOTE, META, candles_of(closes))
        self.assertEqual(a["trend"], "up")
        self.assertEqual(a["bias"], "long")

    def test_declining_series_biases_short(self):
        closes = trending(slope=-0.02)
        a = analyze_symbol("XAUUSD", closes, QUOTE, META, candles_of(closes))
        self.assertEqual(a["trend"], "down")
        self.assertEqual(a["bias"], "short")

    def test_insufficient_data_does_not_crash(self):
        a = analyze_symbol("XAUUSD", [100.0], QUOTE, META, [])
        self.assertTrue(a["ok"])
        self.assertIsNone(a["rsi14"])
        self.assertEqual(a["trend"], "flat")

    def test_commodity_card_attached(self):
        a = analyze_symbol("XAUUSD", trending(), QUOTE, META, candles_of(trending()))
        self.assertEqual(a.get("research_class"), "Precious Metals")
        self.assertTrue(a.get("drivers"))
        self.assertTrue(a.get("leverage"))
        self.assertGreater(a["leverage"]["notional_per_lot"], 0)

    def test_leverage_note_present(self):
        a = analyze_symbol("NGAS", trending(start=2.7), None, {
            "asset_type": "commodity", "tick_size": 0.001, "lot_size": 10000.0}, candles_of(trending(start=2.7)))
        self.assertTrue(a["leverage"]["contract_note"].find("per lot") >= 0)


class TestAnomalyDetection(unittest.TestCase):
    def test_price_spike_detected(self):
        closes = spiky()
        d = detect_anomalies("WTI", closes, candles_of(closes), QUOTE, META)
        kinds = [a["kind"] for a in d["anomalies"]]
        self.assertIn("price_spike", kinds)
        self.assertGreaterEqual(d["anomaly_score"], 1)

    def test_spike_series_scores_higher_than_quiet(self):
        quiet = trending(slope=0.0, noise=0.0005)
        dq = detect_anomalies("XAUUSD", quiet, candles_of(quiet), QUOTE, META)
        ds = detect_anomalies("WTI", spiky(), candles_of(spiky()), QUOTE, META)
        self.assertGreater(ds["anomaly_score"], dq["anomaly_score"])

    def test_severities_are_valid(self):
        d = detect_anomalies("WTI", spiky(), candles_of(spiky()), QUOTE, META)
        for a in d["anomalies"]:
            self.assertIn(a["severity"], ("high", "medium", "low"))
            self.assertTrue(a.get("message"))

    def test_no_anomalies_on_flat_series(self):
        closes = trending(slope=0.0, noise=0.0005)
        d = detect_anomalies("XAUUSD", closes, candles_of(closes), QUOTE, META)
        kinds = [a["kind"] for a in d["anomalies"]]
        self.assertNotIn("price_spike", kinds)

    def test_breakout_flagged(self):
        closes = [100.0 + i for i in range(61)]  # monotonic -> breaks 60-bar high
        d = detect_anomalies("XAUUSD", closes, candles_of(closes), QUOTE, META)
        kinds = [a["kind"] for a in d["anomalies"]]
        self.assertIn("breakout", kinds)


class TestRegime(unittest.TestCase):
    def test_hurst_in_range(self):
        h = hurst_exponent(trending(slope=0.01, noise=0.002, n=512))
        self.assertIsNotNone(h)
        self.assertTrue(0.0 <= h <= 1.0)

    def test_trending_classifies_trending(self):
        r = classify_regime(trending(slope=0.03, noise=0.001, n=512))
        self.assertIn(r["regime"], ("trending", "random"))  # never mean-reverting for a drift series
        self.assertTrue(r["description"])

    def test_crossing_series_classifies_mean_reverting(self):
        # strong alternating pattern
        rng = random.Random(7)
        vals = []
        for i in range(512):
            vals.append(100.0 + (20.0 if i % 2 == 0 else -20.0) + rng.gauss(0, 1.0))
        r = classify_regime(vals)
        self.assertIn(r["regime"], ("mean-reverting", "random"))

    def test_ewma_volatility_positive(self):
        v, var = ewma_volatility([0.001, -0.002, 0.0015])
        self.assertGreater(v, 0.0)
        self.assertGreater(var, 0.0)


class TestTFConsensus(unittest.TestCase):
    def test_aligned_up(self):
        closes = trending(slope=0.02, n=1440)
        c = tf_consensus(closes)
        self.assertEqual(c["alignment"], "aligned-up")
        self.assertEqual(len(c["timeframes"]), 3)

    def test_resample_reduces_length(self):
        closes = trending(n=720)
        out = resample_closes(candles_of(closes), 5)
        self.assertLess(len(out), len(closes))
        self.assertGreater(len(out), 0)


class TestAggregate(unittest.TestCase):
    def test_groups_and_ranks(self):
        rows = [
            {"exchange": "COMEX", "symbol": "XAUUSD", "volume": 100.0, "spread_bps": 5.0, "score": 70.0},
            {"exchange": "COMEX", "symbol": "XAGUSD", "volume": 50.0, "spread_bps": 9.0, "score": 40.0},
            {"exchange": "BINANCE", "symbol": "BTCUSD", "volume": 500.0, "spread_bps": 3.0, "score": 60.0},
        ]
        agg = aggregate_exchanges(rows)
        self.assertEqual(len(agg), 2)
        self.assertEqual(agg[0]["exchange"], "BINANCE")  # highest volume first
        comex = [a for a in agg if a["exchange"] == "COMEX"][0]
        self.assertEqual(comex["symbol_count"], 2)
        self.assertEqual(comex["top_symbol"], "XAUUSD")
        self.assertAlmostEqual(comex["avg_spread_bps"], 7.0)
        self.assertAlmostEqual(comex["avg_score"], 55.0)


if __name__ == "__main__":
    unittest.main()
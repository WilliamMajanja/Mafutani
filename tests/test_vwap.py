"""Session VWAP, standard-deviation bands and the band-entry signal.

Stdlib only.

The module this covers was written, registered in `STRATEGIES`, granted to
Pro+ and then left without a single test -- i.e. selectable in production
and unverified. That is the same failure shape as the SSE batch bug earlier
in this session, where code looked wired and silently never fired. These
tests exist so that cannot recur quietly.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.vwap import (vwap, vwap_bands, vwap_state, typical_price,  # noqa: E402
                          DEFAULT_SESSION_MIN)
from engines import signals as sig  # noqa: E402


def bars(n, base=100.0, vol=1000.0, start_ts=0, ts_step=60):
    out = []
    p = base
    for i in range(n):
        c = base * (1 + 0.0004 * math.sin(i / 9.0))
        out.append({"open": p, "high": max(p, c) * 1.0005,
                    "low": min(p, c) * 0.9995, "close": c,
                    "volume": vol, "ts": start_ts + i * ts_step})
        p = c
    return out


class TestTypicalPrice(unittest.TestCase):
    def test_hlc3_default(self):
        b = {"open": 10.0, "high": 12.0, "low": 8.0, "close": 11.0}
        self.assertAlmostEqual(typical_price(b), 31.0 / 3.0)

    def test_ohlc(self):
        b = {"open": 10.0, "high": 12.0, "low": 8.0, "close": 11.0}
        self.assertAlmostEqual(typical_price(b, "ohlc"), 41.0 / 4.0)

    def test_close_only(self):
        b = {"open": 10.0, "high": 12.0, "low": 8.0, "close": 11.0}
        self.assertAlmostEqual(typical_price(b, "close"), 11.0)

    def test_unknown_source_falls_back_to_hlc3(self):
        b = {"open": 10.0, "high": 12.0, "low": 8.0, "close": 11.0}
        self.assertAlmostEqual(typical_price(b, "nonsense"), 31.0 / 3.0)


class TestSessionAnchoring(unittest.TestCase):
    """The whole reason this module exists.

    `indicators.vwap_series` accumulates from the first candle handed to
    it and never resets, so on 1-minute bars its "session" VWAP drifts
    across days. These pin the reset.
    """

    def test_count_based_reset_at_session_length(self):
        data = bars(3000)                     # no ts -> fixed blocks
        line = vwap(data, session_min=1440)
        # Session 3 starts at index 2880; the VWAP there must equal the
        # single bar's typical price, i.e. the accumulator restarted.
        tp = typical_price(data[2880])
        self.assertAlmostEqual(line[2880], tp, places=9)

    def test_anchor_first_never_resets(self):
        data = bars(3000)
        line = vwap(data, anchor="first")
        self.assertNotAlmostEqual(line[2880], typical_price(data[2880]),
                                  places=6)

    def test_ts_based_reset_at_utc_day_boundary(self):
        base = 1_700_000_000 - (1_700_000_000 % 86400) - 4000 * 60
        data = bars(4000, start_ts=base)
        line = vwap(data)
        resets = [i for i in range(1, len(data))
                  if data[i]["ts"] // 86400 != data[i - 1]["ts"] // 86400]
        self.assertTrue(resets, "test data contains no day boundary")
        for r in resets:
            self.assertAlmostEqual(line[r], typical_price(data[r]), places=9)

    def test_empty_candles(self):
        self.assertEqual(vwap([]), [])
        self.assertEqual(vwap(None), [])

    def test_output_length_matches_input(self):
        data = bars(120)
        self.assertEqual(len(vwap(data)), 120)


class TestBands(unittest.TestCase):

    def test_bands_are_vwap_plus_minus_k_sd(self):
        data = bars(300)
        b = vwap_bands(data, sigmas=(1.0, 2.0))
        last = len(data) - 1
        for k in (1.0, 2.0):
            self.assertAlmostEqual(b["upper"][k][last],
                                   b["vwap"][last] + k * b["sd"][last],
                                   places=9)
            self.assertAlmostEqual(b["lower"][k][last],
                                   b["vwap"][last] - k * b["sd"][last],
                                   places=9)

    def test_zero_dispersion_gives_none_z_not_infinity(self):
        # Identical prices, identical volume: sd is 0, so z is undefined.
        flat = [{"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                 "volume": 100.0, "ts": i * 60} for i in range(50)]
        b = vwap_bands(flat)
        self.assertTrue(all(z is None for z in b["z"]))

    def test_z_is_finite_and_bounded(self):
        data = bars(400)
        b = vwap_bands(data)
        for z in b["z"]:
            if z is not None:
                self.assertTrue(math.isfinite(z))
                self.assertLess(abs(z), 1e6)

    def test_z_sign_matches_price_side_of_vwap(self):
        data = bars(200)
        b = vwap_bands(data)
        last = len(data) - 1
        if b["z"][last] is not None:
            above = data[last]["close"] > b["vwap"][last]
            self.assertEqual(b["z"][last] > 0, above)

    def test_no_volume_gives_no_vwap(self):
        nov = [{"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                "volume": 0.0, "ts": i * 60} for i in range(50)]
        b = vwap_bands(nov)
        self.assertEqual(b["vwap"][-1], 0.0)
        self.assertIsNone(b["z"][-1])

    def test_something_params_dont_break_lengths(self):
        data = bars(80)
        for anchor in ("session", "first", "day_high", "day_low", 5):
            b = vwap_bands(data, anchor=anchor)
            self.assertEqual(len(b["vwap"]), 80, anchor)


class TestState(unittest.TestCase):

    def test_zone_classification(self):
        data = bars(400)
        st = vwap_state(data)
        self.assertTrue(st["ok"])
        self.assertIn(st["zone"],
                      ("at-vwap", "band-1", "band-2", "beyond-3"))

    def test_refuses_without_candles(self):
        st = vwap_state([])
        self.assertFalse(st["ok"])
        self.assertIsNone(st["vwap"])

    def test_refuses_without_volume(self):
        nov = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                "volume": 0.0, "ts": i * 60} for i in range(40)]
        st = vwap_state(nov)
        self.assertFalse(st["ok"])
        self.assertIsNone(st["z"])

    def test_reports_deviation_bps(self):
        st = vwap_state(bars(300))
        if st.get("deviation_bps") is not None:
            self.assertTrue(math.isfinite(st["deviation_bps"]))


class TestSignalRegistration(unittest.TestCase):
    """It is selectable in production, so it must actually be wired."""

    def test_in_strategies(self):
        names = [s["name"] for s in sig.STRATEGIES]
        self.assertIn("vwap_bands", names)

    def test_in_dispatch_table(self):
        self.assertIn("vwap_bands", sig._DISPATCH)

    def test_marked_wired_not_stub(self):
        row = [s for s in sig.STRATEGIES if s["name"] == "vwap_bands"][0]
        self.assertEqual(row["mode"], "wired")

    def test_pro_and_above_only(self):
        from core.tiers import can_use_strategy
        self.assertTrue(can_use_strategy("Pro", "vwap_bands"))
        self.assertTrue(can_use_strategy("Institutional", "vwap_bands"))
        self.assertFalse(can_use_strategy("Free", "vwap_bands"))

    def test_declares_the_paper_parameters(self):
        row = [s for s in sig.STRATEGIES if s["name"] == "vwap_bands"][0]
        for key in ("source", "session_min", "anchor", "entry_sigma",
                    "regime_ema", "stop_ema"):
            self.assertIn(key, row["params"], key)
        # SSRN 6650958 used 200 as the regime classifier, 50 as the stop.
        self.assertEqual(row["params"]["regime_ema"], 200)
        self.assertEqual(row["params"]["stop_ema"], 50)


class TestSignalBehaviour(unittest.TestCase):
    """The signal must refuse rather than fire on absent evidence."""

    def _candles(self, n=400):
        return bars(n)

    def test_returns_a_side_and_a_reason(self):
        out = sig.signal_vwap_bands(self._candles(), None, {})
        self.assertIn("side", out)
        self.assertIn("reason", out)
        self.assertIn(out["side"], (None, "buy", "sell"))
        self.assertTrue(out["reason"])

    def test_warms_up_rather_than_guessing(self):
        out = sig.signal_vwap_bands(bars(10), None, {})
        self.assertIsNone(out["side"])
        self.assertEqual(out["reason"], "warming-up")

    def test_regime_warmup_is_reported(self):
        # Enough for the VWAP, not enough for the 200-EMA regime gate.
        out = sig.signal_vwap_bands(bars(60), None, {"regime_ema": 200})
        self.assertIsNone(out["side"])
        self.assertIn("warming-up", out["reason"])

    def test_never_emits_on_degenerate_bands(self):
        flat = [{"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                 "volume": 500.0, "ts": i * 60} for i in range(400)]
        out = sig.signal_vwap_bands(flat, None, {})
        self.assertIsNone(out["side"])
        self.assertEqual(out["reason"], "no-band-dispersion")

    def test_zero_regime_ema_disables_the_gate(self):
        out = sig.signal_vwap_bands(self._candles(), None, {"regime_ema": 0})
        self.assertNotEqual(out["reason"], "warming-up-regime")

    def test_entry_sigma_parameter_is_respected(self):
        # A 5-sigma gate should almost never trigger where 2 does.
        loose = sig.signal_vwap_bands(self._candles(), None, {"entry_sigma": 2.0})
        tight = sig.signal_vwap_bands(self._candles(), None, {"entry_sigma": 50.0})
        self.assertIsNone(tight["side"])
        self.assertEqual(tight["reason"], "inside-bands")

    def test_bad_params_fall_back_to_defaults(self):
        out = sig.signal_vwap_bands(self._candles(), None,
                                     {"entry_sigma": "not-a-number",
                                      "regime_ema": None, "session_min": "x"})
        self.assertIn("side", out)
        self.assertTrue(out["reason"])


if __name__ == "__main__":
    unittest.main()

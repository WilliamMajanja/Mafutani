"""/api/chart_position — position, unrealised P&L, and round-trip cost.

Three things this must get right, and each one has a quiet failure mode:

**Flat is not a position.** `positions` keeps zero-quantity rows for
symbols that were closed. Reporting one as a position draws an entry line
at `avg_price` 0 and labels it "LONG 0" — a chart claiming an open
position on an empty book. The first implementation did exactly this and
XAUUSD rendered as a position while holding nothing.

**The cost figure is the point.** The panel exists so a signal is read
next to the round trip it has to clear. A dislocation of a few bps against
~70 bps is not a small edge; it is a reason not to trade. So the endpoint
always returns the cost even when flat.

**P&L is arithmetic, not a stored value.** It is computed from the last
price so a stale stored figure cannot disagree with the price drawn on the
chart next to it.
"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.live_server import ensure_live_server

BASE = ensure_live_server()




def get(path, **params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read().decode())


class TestChartPosition(unittest.TestCase):
    def test_known_symbol_answers(self):
        d = get("/api/chart_position", symbol="BTCUSD")
        self.assertEqual(d["symbol"], "BTCUSD")
        self.assertIn("position", d)
        self.assertIn("round_trip_cost_bps", d)

    def test_flat_symbol_reports_null_not_a_zero_position(self):
        """The bug this endpoint was written with, fixed.

        XAUUSD has a row in `positions` with qty 0. Reporting it produced
        {"qty": 0.0, "avg_price": 0.0}, which the chart then drew as an
        entry line at price 0 labelled "LONG 0".
        """
        d = get("/api/chart_position", symbol="XAUUSD")
        if d["position"] is not None:
            self.assertNotEqual(d["position"]["qty"], 0.0,
                                "returned a zero-quantity position")
        self.assertIsNone(d["unrealized_pnl"],
                          "flat position reported unrealised P&L")

    def test_unknown_symbol_is_not_an_error(self):
        d = get("/api/chart_position", symbol="NOSUCHSYMBOL")
        self.assertIsNone(d["position"])
        self.assertIsNone(d["last"])

    def test_missing_symbol_is_an_error(self):
        with self.assertRaises(urllib.error.HTTPError):
            get("/api/chart_position")

    def test_symbol_is_upper_cased(self):
        d = get("/api/chart_position", symbol="btcusd")
        self.assertEqual(d["symbol"], "BTCUSD")

    def test_cost_is_always_present_even_when_flat(self):
        """A flat book still needs the cost, or the panel is useless."""
        d = get("/api/chart_position", symbol="NOSUCHSYMBOL")
        self.assertIsNotNone(d["round_trip_cost_bps"])
        self.assertGreater(d["round_trip_cost_bps"], 0)

    def test_cost_scales_with_tier_and_mode(self):
        """Live trading costs more than paper. If it does not, the number
        on screen is wrong exactly when it matters most."""
        paper = get("/api/chart_position", symbol="BTCUSD", mode="paper")["round_trip_cost_bps"]
        live = get("/api/chart_position", symbol="BTCUSD", mode="live")["round_trip_cost_bps"]
        self.assertIsNotNone(paper)
        self.assertIsNotNone(live)
        self.assertGreaterEqual(live, paper,
                                "live cost is below paper cost")

    def test_unknown_mode_falls_back_rather_than_breaking(self):
        d = get("/api/chart_position", symbol="BTCUSD", mode="nonsense")
        self.assertIsNotNone(d["round_trip_cost_bps"])


class TestUnrealisedArithmetic(unittest.TestCase):
    def test_pnl_matches_the_reported_prices(self):
        """Computed, not stored -- so it must agree with what is returned.

        A stored P&L that disagrees with `last` puts two contradictory
        numbers on the same screen.
        """
        d = get("/api/chart_position", symbol="BTCUSD")
        p, last = d.get("position"), d.get("last")
        if not p or not last or not p.get("avg_price"):
            self.skipTest("BTCUSD is flat on this deployment")
        expected = (last - p["avg_price"]) * p["qty"]
        self.assertAlmostEqual(d["unrealized_pnl"], expected, places=6)

    def test_a_long_down_price_is_a_loss(self):
        """Sign convention, checked rather than assumed."""
        d = get("/api/chart_position", symbol="BTCUSD")
        p, last = d.get("position"), d.get("last")
        if not p or not last:
            self.skipTest("BTCUSD is flat on this deployment")
        if p["qty"] > 0:
            self.assertEqual(d["unrealized_pnl"] > 0, last > p["avg_price"])
        else:
            self.assertEqual(d["unrealized_pnl"] > 0, last < p["avg_price"])


class TestFrontendWiring(unittest.TestCase):
    """The overlay exists only if the page sends and draws it."""

    def _src(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_page_fetches_position_context(self):
        self.assertIn("/api/chart_position", self._src("web/app.js"))

    def test_page_passes_position_to_the_renderer(self):
        self.assertIn("position: posCtx", self._src("web/app.js"))

    def test_page_has_a_host_element(self):
        self.assertIn("id='c-pos'", self._src("web/app.js"))

    def test_chart_renderer_reads_opts_position(self):
        src = self._src("web/charts.js")
        self.assertIn("opts.position", src)
        self.assertIn("LONG", src)
        self.assertIn("SHORT", src)

    def test_overlay_is_optional_in_the_renderer_signature(self):
        """Every other chart page calls drawCandles with 5 args."""
        self.assertIn("function drawCandles(canvas, candles, ema9, ema21, vwap, opts)",
                      self._src("web/charts.js"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

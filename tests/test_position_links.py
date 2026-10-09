"""Position action links and Scalp Live deep links.

Every position row must offer three exits -- chart, buy, sell -- and the
buy/sell links must *arm* the ticket rather than place an order. A link
that fires a trade on navigation is how an operator ends up buying
something they only meant to look at, so that distinction is asserted
directly against the source rather than trusted.
"""
import os
import re
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(WORKDIR, "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(WORKDIR, "web", "style.css"), encoding="utf-8").read()


def block(fn):
    return JS.split("function " + fn, 1)[1].split("\n  function ", 1)[0]


class TestDeepLinkRoute(unittest.TestCase):
    def test_router_handles_scalp_live_path(self):
        self.assertIn('h.indexOf("/scalp-live/") === 0', JS)

    def test_page_accepts_symbol_side_and_qty(self):
        head = block("pageScalpLive")[:400]
        self.assertIn("presetSymbol", head)
        self.assertIn("presetSide", head)
        self.assertIn("presetQty", head)

    def test_qty_parsed_from_query_string(self):
        self.assertIn("qty=([^&]*)", JS)

    def test_symbol_is_decoded(self):
        self.assertIn("decodeURIComponent(parts[0]", JS)


class TestDeepLinkNeverTrades(unittest.TestCase):
    """The load-bearing safety property of these links."""

    def _armed_block(self):
        body = block("pageScalpLive")
        return body.split("if (armedSide)", 1)[1] if "if (armedSide)" in body else ""

    def test_armed_path_does_not_post_an_order(self):
        self.assertNotIn("/api/orders", self._armed_block())

    def test_button_is_highlighted_not_clicked(self):
        armed = self._armed_block()
        self.assertIn('btn.classList.add("armed")', armed)
        self.assertNotIn("btn.click()", armed)

    def test_operator_is_told_nothing_was_sent(self):
        self.assertIn("Nothing is sent until", JS)

    def test_only_buy_or_sell_can_arm(self):
        body = block("pageScalpLive")
        self.assertIn('presetSide === "buy" || presetSide === "sell"', body)


class TestSharedPositionRenderer(unittest.TestCase):
    def test_helpers_exist(self):
        self.assertIn("function positionActions(", JS)
        self.assertIn("function positionRows(", JS)

    def test_both_pages_use_it(self):
        for fn in ("pageDashboard", "pagePortfolio"):
            m = re.search(r"<h3>Positions</h3>(.{0,80})", block(fn), re.S)
            self.assertIsNotNone(m, "%s has no Positions section" % fn)
            self.assertIn("positionRows(", m.group(1),
                          "%s still hand-rolls its position table" % fn)
            self.assertNotIn("<table", m.group(1),
                             "%s still emits a raw table for Positions" % fn)

    def test_both_pages_have_an_empty_state(self):
        for fn in ("pageDashboard", "pagePortfolio"):
            self.assertIn("no open positions", block(fn))

    def test_two_actions_per_row_plus_the_symbol_chart_link(self):
        """The symbol cell is the chart link, so positionActions adds
        only buy and sell. A second identical chart chip on the same row
        is noise."""
        pa = block("positionActions")[:900]
        for want in ("/scalp-live/", "/buy", "/sell"):
            self.assertIn(want, pa)
        self.assertNotIn("#/chart/", pa,
                         "chart is already the symbol link; do not duplicate it")
        # ...and the symbol cell must actually be a chart link
        self.assertIn("href='#/chart/\" + encodeURIComponent(p.symbol)",
                      block("positionRows"))

    def test_qty_is_carried_in_the_link(self):
        pa = block("positionActions")[:900]
        self.assertIn("?qty=", pa)
        self.assertIn("Math.abs(q)", block("positionRows"))

    def test_chart_link_is_encoded(self):
        self.assertIn("encodeURIComponent(symbol)", JS)

    def test_no_duplicate_ids_per_page(self):
        for fn in ("pagePortfolio", "pageDashboard", "pageScalpLive"):
            ids = re.findall(r"id='([a-z0-9\-]+)'", block(fn))
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            self.assertEqual(dupes, [], "%s has duplicate ids: %s" % (fn, dupes))


class TestPositionActionStyling(unittest.TestCase):
    def test_classes_exist(self):
        for cls in (".pos-actions", ".pa", ".pa.buy", ".pa.sell", "button.armed"):
            self.assertIn(cls, CSS)

    def test_armed_pulse_respects_reduced_motion(self):
        self.assertIn("prefers-reduced-motion", CSS)


if __name__ == "__main__":
    unittest.main()

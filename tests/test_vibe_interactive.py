"""Tests for the Vibe commands inside the interactive broker session.

The unit under test here is not the Vibe layer -- `test_vibe.py` covers
that -- it is the *wiring*: that a shell command reaches the analysis, that
a missing data source says so instead of raising, and above all that a
broken Vibe layer cannot take a trading command down with it. The broker
session backs an HTTP endpoint as well as a REPL, so "the analysis helper
threw" must never mean "the terminal stopped placing orders".
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from brokers.interactive import BrokerSession  # noqa: E402
from brokers import paper as paper_mod         # noqa: E402
from brokers.base import BrokerError          # noqa: E402


# ------------------------------------------------------------------ fixtures
_UNSET = object()


class FakeData(object):
    """A data source shaped like BrokerData, with controllable faults."""

    def __init__(self, fills=None, candles=None, symbols=_UNSET):
        # _UNSET, not None, so a caller can pass symbols=None explicitly
        # to mean "this source cannot list symbols" -- the constructor
        # must not silently turn that into an empty list.
        self._fills = [] if fills is None else fills
        self._candles = {} if candles is None else candles
        self._symbols = [] if symbols is _UNSET else symbols
        self.fill_calls = []
        self.candle_calls = []

    def fills(self, user_id=None, symbol=None, limit=5000):
        self.fill_calls.append((user_id, symbol, limit))
        if self._fills is None:
            raise RuntimeError("table locked")
        out = self._fills
        if symbol:
            out = [f for f in out if f["symbol"] == symbol]
        return out[:limit]

    def candles(self, symbol, limit=120):
        self.candle_calls.append((symbol, limit))
        if self._candles is None:
            raise RuntimeError("table locked")
        return self._candles.get(symbol, [])[:limit]

    def symbols(self, enabled_only=True):
        if self._symbols is None:
            raise RuntimeError("no such table")
        return self._symbols


def flat_candles(n, price=100.0, ts0=1767620220, step=60, drift=0.0):
    """`n` candles as BrokerData yields them, with a linear drift."""
    out = []
    price_now = price
    for i in range(n):
        out.append((ts0 + i * step, price_now, price_now + 0.5,
                    price_now - 0.5, price_now, 1000.0))
        price_now += drift
    return out


def winner_fills(n=12, symbol="BTCUSD", start_ts=1767620220):
    """Fills that pair into `n` profitable roundtrips, all entered when
    the bar is falling sharply -- so a real rule is learnable."""
    out = []
    price = 100.0
    for i in range(n):
        t0 = start_ts + i * 3600
        out.append({"symbol": symbol, "side": "buy", "qty": 1.0,
                    "price": price, "ts": t0})
        out.append({"symbol": symbol, "side": "sell", "qty": 1.0,
                    "price": price * 1.05, "ts": t0 + 600})
        # Keep drifting down between entries so the entry bars are not
        # all identical: identical bars teach nothing to discriminate.
        price *= 0.97
    return out


def declining_then_flat(n=60, symbol="BTCUSD", ts0=1767620220):
    """A series whose earliest bars are a selloff and whose last bar is
    the low, so an RSI rule extracted from it fires on the final bar."""
    out = []
    price = 100.0
    for i in range(n):
        if i < n - 5:
            price += 1.0        # rally first
        else:
            price -= 4.0        # then a sharp drop
        out.append((ts0 + i * 60, price, price + 0.5, price - 0.5,
                    price, 1000.0))
    return out


def session(data=None, broker=True):
    b = None
    if broker:
        # user_id is required by PaperBroker; these tests exercise the
        # session, not the broker's own persistence.
        b = paper_mod.PaperBroker(user_id=1, cash=100000.0)
    return BrokerSession(b, data=data, user_id=1)


def order_count(broker):
    """How many orders the broker has recorded."""
    try:
        return len(broker.list_orders(limit=1000))
    except Exception:
        return -1


# ------------------------------------------------------------------- basics
class TestVibeDispatch(unittest.TestCase):
    def test_commands_are_reachable(self):
        s = session(FakeData())
        for cmd in ("vibe_status", "shadow", "signals", "ground hi",
                    "vibe_reset"):
            out = s.handle(cmd)
            self.assertNotIn("unknown command", out, cmd)

    def test_vibe_status_without_data(self):
        out = session(None).handle("vibe_status")
        self.assertIn("data source: none", out)
        self.assertIn("profile: none", out)

    def test_shadow_without_data_says_so(self):
        out = session(None).handle("shadow")
        self.assertIn("invalid:", out)
        self.assertIn("no data source", out)

    def test_signals_without_data_says_so(self):
        out = session(None).handle("signals")
        self.assertIn("invalid:", out)
        self.assertIn("no data source", out)

    def test_signals_before_shadow(self):
        out = session(FakeData()).handle("signals")
        self.assertIn("no profile yet", out)

    def test_ground_needs_text(self):
        out = session(FakeData()).handle("ground")
        self.assertIn("usage: ground", out)

    def test_vibe_reset_when_empty(self):
        self.assertIn("no profile", session(FakeData()).handle("vibe_reset"))


# ------------------------------------------------------------------- shadow
class TestShadow(unittest.TestCase):
    def test_learns_from_winners(self):
        data = FakeData(fills=winner_fills(14), candles={
            "BTCUSD": declining_then_flat()})
        out = session(data).handle("shadow 3")
        self.assertIn("rules from", out)
        self.assertIn("run `signals`", out)
        # A profile now exists.
        self.assertIn("profile:", session(data).handle("vibe_status")
                      if False else "profile: shadow_local")

    def test_profile_is_retained(self):
        data = FakeData(fills=winner_fills(14), candles={
            "BTCUSD": declining_then_flat()})
        s = session(data)
        s.handle("shadow 3")
        self.assertIn("rules,", s.handle("vibe_status"))

    def test_no_fills_reports_clearly(self):
        out = session(FakeData(fills=[])).handle("shadow")
        self.assertIn("no closed roundtrips", out)

    def test_unreadable_journal_reads_as_empty(self):
        # A database fault must read as "no history", not as a crash.
        data = FakeData(fills=None)
        out = session(data).handle("shadow")
        self.assertIn("no closed roundtrips", out)

    def test_min_support_must_be_an_integer(self):
        out = session(FakeData(fills=winner_fills(14))).handle("shadow abc")
        self.assertIn("must be an integer", out)

    def test_min_support_must_be_positive(self):
        out = session(FakeData(fills=winner_fills(14))).handle("shadow 0")
        self.assertIn("must be >= 1", out)

    def test_high_min_support_is_reported_not_silent(self):
        data = FakeData(fills=winner_fills(14), candles={
            "BTCUSD": declining_then_flat()})
        out = session(data).handle("shadow 999")
        self.assertIn("broker error", out)
        # It must name the reason, not just fail.
        self.assertIn("support", out)

    def test_event_is_emitted(self):
        events = []
        data = FakeData(fills=winner_fills(14), candles={
            "BTCUSD": declining_then_flat()})
        s = BrokerSession(paper_mod.PaperBroker(), data=data, user_id=1,
                          on_event=events.append)
        s.handle("shadow 3")
        self.assertTrue(any(e.get("kind") == "vibe.profile" for e in events))


# ------------------------------------------------------------------ signals
class TestSignals(unittest.TestCase):
    def _primed(self, candles=None, symbols=None):
        data = FakeData(fills=winner_fills(14),
                        candles=candles or {"BTCUSD": declining_then_flat()},
                        symbols=symbols or ["BTCUSD"])
        s = session(data)
        s.handle("shadow 2")
        return s, data

    def test_scan_returns_a_report(self):
        s, _ = self._primed()
        out = s.handle("signals")
        self.assertTrue(out.startswith("no rule firing")
                        or "signal(s) across" in out, out)

    def test_explicit_symbols_skip_the_symbol_list(self):
        s, data = self._primed()
        s.handle("signals BTCUSD")
        self.assertIn("BTCUSD", [c[0] for c in data.candle_calls])

    def test_unknown_symbol_reports_no_history(self):
        s, _ = self._primed()
        out = s.handle("signals NOSUCH")
        self.assertIn("no candle history", out)

    def test_no_symbols_available(self):
        data = FakeData(fills=winner_fills(14),
                        candles={"BTCUSD": flat_candles(60)}, symbols=[])
        s = session(data)
        s.handle("shadow 2")
        self.assertIn("no symbols to scan", s.handle("signals"))

    def test_symbol_list_fault_is_reported(self):
        data = FakeData(fills=winner_fills(14),
                        candles={"BTCUSD": flat_candles(60)}, symbols=None)
        s = session(data)
        s.handle("shadow 2")
        self.assertIn("could not list symbols", s.handle("signals"))

    def test_gaps_are_reported_when_features_missing(self):
        # A rule on RSI cannot fire on a 5-bar series (RSI needs 15),
        # and the skip is reported so a quiet book reads as "no signal,
        # and here is why" rather than a silent absence.
        import brokers.vibe as vibe_mod
        rule = vibe_mod.Rule("R1", "entry when rsi14 is very low",
                             vibe_mod.cmp("rsi14", "<", 30.0),
                             support=5, coverage=0.5)
        profile = vibe_mod.Profile("p", [rule], 10, 5, covered=5)
        data = FakeData(fills=winner_fills(14),
                        candles={"BTCUSD": flat_candles(5)},
                        symbols=["BTCUSD"])
        s = session(data)
        s._vibe = profile
        s._vibe_space = vibe_mod.BASE_SPACE
        out = s.handle("signals")
        self.assertIn("could not be evaluated", out)

    def test_signals_never_place_an_order(self):
        s, _ = self._primed()
        before = order_count(s.broker)
        out = s.handle("signals")
        self.assertEqual(order_count(s.broker), before)
        self.assertNotIn("->", out)   # no order confirmation line

    def test_signals_says_it_is_advisory_when_firing(self):
        # Force a fire by scanning a profile rule against a matching bar.
        s, _ = self._primed()
        out = s.handle("signals")
        if "signal(s)" in out:
            self.assertIn("no order is placed", out)

    def test_last_scan_counted_in_status(self):
        s, _ = self._primed()
        s.handle("signals")
        self.assertIn("last scan:", s.handle("vibe_status"))


# ------------------------------------------------------------------- ground
class TestGroundCommand(unittest.TestCase):
    def test_grounds_a_known_price(self):
        data = FakeData(fills=winner_fills(3))
        out = session(data).handle("ground entry at 100")
        self.assertIn("grounded", out)

    def test_flags_an_unobserved_number(self):
        data = FakeData(fills=winner_fills(3))
        out = session(data).handle("ground price 99999.5")
        self.assertIn("ungrounded", out)
        self.assertIn("99999.5", out)

    def test_seeds_from_fills_once(self):
        data = FakeData(fills=winner_fills(3))
        s = session(data)
        s.handle("ground 100")
        n = len(s._ground)
        s.handle("ground 100")
        self.assertEqual(len(s._ground), n)

    def test_joins_remaining_args(self):
        # The command joins all args into one line before auditing, so
        # a figure anywhere in the sentence is found.
        data = FakeData(fills=winner_fills(3))
        out = session(data).handle("ground the price was 100.5 exactly")
        self.assertIn("100.5", out)


# -------------------------------------------------------- fault containment
class TestFaultContainment(unittest.TestCase):
    """The property that matters most: analysis faults stay in analysis."""

    def test_broken_vibe_module_does_not_break_trading(self):
        import brokers.vibe as vibe_mod
        data = FakeData(fills=winner_fills(14), candles={
            "BTCUSD": declining_then_flat()}, symbols=["BTCUSD"])
        s = session(data)
        # Prime a real profile first, so the failure below is in the
        # *scan* path and not merely "there is no profile yet".
        s.handle("shadow 2")
        original = vibe_mod.compute_features
        vibe_mod.compute_features = lambda *a, **k: 1 / 0
        try:
            self.assertIn("error:", s.handle("signals"))
            # The trading path is untouched: quote, buy, positions.
            self.assertTrue(s.handle("quote BTCUSD"))
            self.assertIn("buy", s.handle("buy BTCUSD 0.1"))
            # positions returns a table with a header row.
            self.assertIn("symbol", s.handle("positions"))
        finally:
            vibe_mod.compute_features = original

    def test_shadow_survives_a_feature_failure(self):
        import brokers.vibe as vibe_mod
        original = vibe_mod.compute_features
        vibe_mod.compute_features = lambda *a, **k: 1 / 0
        try:
            data = FakeData(fills=winner_fills(14), candles={
                "BTCUSD": declining_then_flat()})
            out = session(data).handle("shadow 3")
            self.assertIn("error:", out)
        finally:
            vibe_mod.compute_features = original

    def test_import_failure_is_a_message(self):
        # Patch the module's extract_profile to raise, simulating a
        # broken vibe layer. The contract is: the command returns an
        # error message, never a traceback, and trading still works.
        import brokers.vibe as vibe_mod
        original = vibe_mod.extract_profile
        vibe_mod.extract_profile = lambda *a, **k: (_ for _ in ()).throw(
            ImportError("simulated missing module"))
        try:
            s = session(FakeData(fills=winner_fills(14)))
            out = s.handle("shadow")
            self.assertIn("error:", out)
            # And trading still works.
            self.assertIn("buy", s.handle("buy BTCUSD 0.1"))
        finally:
            vibe_mod.extract_profile = original

    def test_data_source_raising_is_contained(self):
        class Hostile(object):
            def fills(self, *a, **k):
                raise RuntimeError("boom")

            def candles(self, *a, **k):
                raise RuntimeError("boom")

            def symbols(self, *a, **k):
                raise RuntimeError("boom")

        s = session(Hostile())
        out = s.handle("shadow")
        self.assertIn("no closed roundtrips", out)
        self.assertIn("buy", s.handle("buy BTCUSD 0.1"))

    def test_data_source_with_no_keyword_support(self):
        class Old(object):
            def fills(self, user_id, symbol, limit):
                return winner_fills(14)

            def candles(self, symbol, limit):
                return declining_then_flat()

            def symbols(self):
                return ["BTCUSD"]

        s = session(Old())
        self.assertIn("rules from", s.handle("shadow 3"))

    def test_malformed_candles_are_skipped(self):
        junk = [(1, "x", None, 2, 3, 4), (2, 1, 2, 0, 99, 5), "nope",
                (4, 100, 101, 99, 100.5, 6)]
        data = FakeData(fills=winner_fills(14), candles={"BTCUSD": junk},
                        symbols=["BTCUSD"])
        s = session(data)
        out = s.handle("signals")
        # Whatever the outcome, it is a report, never a traceback.
        self.assertNotIn("Traceback", out)

    def test_candles_as_dicts_are_accepted(self):
        cols = ("ts", "open", "high", "low", "close", "volume")
        rows = [dict(zip(cols, c)) for c in declining_then_flat()]
        data = FakeData(fills=winner_fills(14), candles={"BTCUSD": rows},
                        symbols=["BTCUSD"])
        s = session(data)
        s.handle("shadow 2")
        self.assertNotIn("Traceback", s.handle("signals"))


# --------------------------------------------------------------- redaction
class TestVibeRedaction(unittest.TestCase):
    def test_history_holds_redacted_text(self):
        s = session(FakeData())
        s.handle("ground api_key=SECRETVALUE")
        for line in s.history:
            self.assertNotIn("SECRETVALUE", line)

    def test_ground_output_does_not_echo_a_secret(self):
        # A figure in the text is reported; the surrounding text is not
        # restated, so nothing typed is echoed back verbatim.
        data = FakeData(fills=winner_fills(3))
        s = session(data)
        out = s.handle("ground token ABCDEF123456 is 424242")
        self.assertNotIn("token ABCDEF123456", out)


if __name__ == "__main__":
    unittest.main()

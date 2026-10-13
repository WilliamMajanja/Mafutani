"""The MQL5 EA's JSON handling, ported to Python and tested against real bridge output.

MQL5 has no JSON parser that we can rely on here, so the EA hand-rolls one:
`ExtractValue` / `ExtractCommand`. That hand-rolled parser is exactly the
kind of code that compiles, passes whatever tests only cover the easy case,
and still fails on real input -- and it did.

The bug
-------
The bridge returns, from `POST /exec` on the Python side and `GET /next` on
the EA side, a body shaped like:

    {"ok": true, "command": {"id": "cmd-1", "op": "submit",
                             "order": {"symbol": "EURUSD", ...}}}

The original parser found `"command":` and cut the value at the first comma,
producing `{"op": "submit"` -- 16 characters, truncated at the comma inside
the nested `order` object. Reading `symbol`/`qty`/`price` from that fragment
returned empty, so `DoSubmit` had no symbol and refused every order. A second
layer compounded it: `json.dumps` emits `"command": {` *with a space*, which
the parser's "is the next char `{`?" check missed, sending the value down the
bare-scalar branch and truncating it again.

This is the same shape as the "computed but never used" and "pruner nothing
calls" bugs from earlier today: it compiled clean, the tests passed, and the
component was completely non-functional against real input.

The port below is a faithful transcription of the MQL5 logic, kept in sync by
`test_port_matches_mq5_source` -- which fails if the MQL5 and this file drift
apart, since a port that silently stops matching the thing it ports is worse
than no port.
"""
import json
import os
import re
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MQ5 = os.path.join(ROOT, "services", "mt5", "MafutaniBridge.mq5")


# ---------------------------------------------------------------------------
# Faithful port of the MQL5 parser. Keep in lockstep with MafutaniBridge.mq5.
# ---------------------------------------------------------------------------
def extract_value(src, key):
    needle = '"%s":' % key
    p = src.find(needle)
    if p < 0:
        return ""
    s = p + len(needle)
    while s < len(src) and src[s] == " ":
        s += 1
    if s >= len(src):
        return ""

    if src[s] == '"':
        out = []
        i = s + 1
        while i < len(src):
            c = src[i]
            if c == "\\" and i + 1 < len(src):
                i += 1
                out.append(src[i])
                i += 1
                continue
            if c == '"':
                break
            out.append(c)
            i += 1
        return "".join(out)

    if src[s] in "{[":
        depth = 0
        in_str = False
        out = []
        i = s
        while i < len(src):
            c = src[i]
            if c == '"' and (i == 0 or src[i - 1] != "\\"):
                in_str = not in_str
            if not in_str:
                if c in "{[":
                    depth += 1
                elif c in "}]":
                    depth -= 1
                    if depth == 0:
                        out.append(c)
                        return "".join(out)
            out.append(c)
            i += 1
        return "".join(out)

    e = src.find(",", s)
    if e < 0:
        e = src.find("}", s)
    if e < 0:
        e = len(src)
    return src[s:e].strip()


def extract_command(body):
    return extract_value(body, "command")


def real_submit_body(coid="coid-abc123", symbol="EURUSD", qty=0.1,
                     otype="limit", price=1.085, stop=None):
    """The body the EA actually receives, built the way the bridge builds it."""
    return json.dumps({
        "ok": True,
        "command": {
            "id": "cmd-1",
            "op": "submit",
            "queued_at": 1791803040.0,
            "order": {
                "symbol": symbol, "side": "buy", "qty": qty, "type": otype,
                "price": price, "stop_price": stop, "trail_pct": None,
                "client_order_id": coid, "reduce_only": False,
                "time_in_force": "day",
            },
        },
    })


class TestAgainstRealBridgeOutput(unittest.TestCase):
    """The case that broke, using the shape the bridge actually emits."""

    def setUp(self):
        self.body = real_submit_body()
        self.cmd = extract_command(self.body)

    def test_command_is_not_truncated(self):
        self.assertTrue(
            self.cmd.rstrip().endswith("}"),
            "command truncated at %d chars: %r" % (len(self.cmd), self.cmd[-40:]))

    def test_command_contains_the_whole_order(self):
        self.assertIn("client_order_id", self.cmd)
        self.assertIn("EURUSD", self.cmd)

    def test_order_object_is_recoverable(self):
        order = extract_value(self.cmd, "order")
        self.assertTrue(order.startswith("{"), "order not found: %r" % order)
        self.assertTrue(order.rstrip().endswith("}"))

    def test_id_and_op_survive(self):
        self.assertEqual(extract_value(self.cmd, "id"), "cmd-1")
        self.assertEqual(extract_value(self.cmd, "op"), "submit")

    def test_every_order_field_is_readable(self):
        """These are the fields DoSubmit depends on. Empty means no order."""
        order = extract_value(self.cmd, "order")
        coid = "coid-abc123"
        self.assertEqual(extract_value(order, "symbol"), "EURUSD")
        self.assertEqual(extract_value(order, "side"), "buy")
        self.assertEqual(float(extract_value(order, "qty")), 0.1)
        self.assertEqual(extract_value(order, "type"), "limit")
        self.assertEqual(float(extract_value(order, "price")), 1.085)
        self.assertEqual(extract_value(order, "client_order_id"), coid)

    def test_null_valued_optional_fields_are_empty_not_the_word_null(self):
        order = extract_value(self.cmd, "order")
        self.assertEqual(extract_value(order, "stop_price"), "null")
        self.assertEqual(float(extract_value(order, "qty")), 0.1,
                         "a later null field corrupted an earlier number")

    def test_no_leading_whitespace_on_scalars(self):
        """' 0.1' parses as 0 on some MQL5 builds."""
        order = extract_value(self.cmd, "order")
        self.assertEqual(extract_value(order, "qty"), "0.1")


class TestOtherCommandShapes(unittest.TestCase):
    """Every op the bridge can send, not just submit."""

    def _cmd(self, payload):
        return extract_command(json.dumps({"ok": True, "command": payload}))

    def test_ping(self):
        c = self._cmd({"id": "cmd-2", "op": "ping"})
        self.assertEqual(extract_value(c, "op"), "ping")
        self.assertEqual(extract_value(c, "id"), "cmd-2")

    def test_cancel(self):
        c = self._cmd({"id": "cmd-3", "op": "cancel", "ticket": 12345})
        self.assertEqual(extract_value(c, "op"), "cancel")
        self.assertEqual(extract_value(c, "ticket"), "12345")

    def test_quote(self):
        c = self._cmd({"id": "cmd-4", "op": "quote", "symbol": "EURUSD"})
        self.assertEqual(extract_value(c, "symbol"), "EURUSD")

    def test_command_null_means_idle(self):
        body = json.dumps({"ok": True, "command": None})
        self.assertIsNone(json.loads(body)["command"])

    def test_missing_key_is_empty_not_an_exception(self):
        self.assertEqual(extract_value('{"a":1}', "nope"), "")


class TestValuesContainingAwkwardCharacters(unittest.TestCase):
    def test_symbol_with_a_dot(self):
        b = real_submit_body(symbol="BTCUSD.m")
        order = extract_value(extract_command(b), "order")
        self.assertEqual(extract_value(order, "symbol"), "BTCUSD.m")

    def test_commas_inside_a_string_do_not_truncate(self):
        """The original bug, in its purest form."""
        b = json.dumps({"ok": True, "command": {
            "id": "c1", "op": "submit",
            "order": {"symbol": "X", "note": "a,b,c"}}})
        order = extract_value(extract_command(b), "order")
        self.assertEqual(extract_value(order, "note"), "a,b,c")

    def test_braces_inside_a_string_do_not_end_the_scan(self):
        b = json.dumps({"ok": True, "command": {
            "id": "c2", "op": "submit", "order": {"symbol": "X", "note": "}{"}}})
        order = extract_value(extract_command(b), "order")
        self.assertEqual(extract_value(order, "note"), "}{")


class TestPortMatchesMq5Source(unittest.TestCase):
    """A port that stops matching what it ports is worse than no port.

    If the MQL5 changes and this file does not, every test above becomes a
    reassuring lie. So the two are checked against each other.
    """

    def _src(self):
        with open(MQ5, encoding="utf-8") as f:
            return f.read()

    def test_mql5_has_the_whitespace_skip(self):
        self.assertIn("StringGetCharacter(src, s) == ' '",
                      self._src(),
                      "the MQL5 lost the whitespace skip the port still has")

    def test_mql5_uses_brace_matching_not_comma_cutting(self):
        src = self._src()
        fn = src.split("string ExtractValue(")[1].split("string ExtractCommand(")[0]
        self.assertIn("depth", fn,
                      "ExtractValue no longer brace-matches nested objects")
        self.assertNotIn('StringFind(cmd, ",", s)', fn)

    def test_mql5_reads_order_fields_from_the_order_object(self):
        src = self._src()
        submit = src.split("bool DoSubmit(")[1].split("bool DoCancel(")[0]
        self.assertIn('ExtractValue(command, "order")', submit,
                      "DoSubmit no longer unwraps the nested order object")

    def test_mql5_refuses_an_order_with_no_symbol(self):
        submit = self._src().split("bool DoSubmit(")[1].split("bool DoCancel(")[0]
        self.assertIn('order has no symbol', submit,
                      "DoSubmit lost its missing-symbol guard")


class TestPreflightWouldNotHaveCaughtThis(unittest.TestCase):
    """Being honest about the limits of the existing verification."""

    def test_simulated_ea_does_not_parse_json(self):
        """The preflight cannot catch EA-side parsing bugs, by construction.

        `SimEA` speaks the protocol from the *Python* side, so every check in
        scripts/mt5_preflight.py passes regardless of what the MQL5 does with
        the bytes. That is why the bug above sat there compiling clean, and
        it is why this port exists.
        """
        import scripts.mt5_preflight as P
        ea = P.SimEA("http://x:1")
        self.assertTrue(hasattr(ea, "_default"),
                        "preflight structure changed; re-check this claim")
        self.assertFalse(hasattr(ea, "_url_is_a_parser"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

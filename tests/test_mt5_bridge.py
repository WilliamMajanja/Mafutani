"""MT5 bridge: adapter, queue, and the EA bridge protocol. No terminal needed.

The EA cannot be run here, so it is replaced by `FakeEA` -- a thread that
does exactly what the real one does: poll `/next`, compute an answer, post
it to `/result`. Everything either side of that (queueing, timeout,
retcode mapping, the live gate) is real code and is what these tests cover.

The tests that matter most are the ones about what happens when the EA does
*not* answer. A bridge that loses a result is the dangerous case, because
the order may well have executed at the terminal while the app is left
guessing. A timeout that reports "unknown" is correct; a timeout that
returns a fill, or that silently retries, is how an account ends up
double-exposed.
"""
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from brokers import mt5 as M
from brokers.base import (BrokerAuthError, BrokerError, BrokerRejectedError,
                          BrokerUnavailableError, BrokerValidationError,
                          OrderRequest)
from services.mt5.bridge_server import BridgeState, serve


# --------------------------------------------------------------- retcodes
class TestRetcodeMapping(unittest.TestCase):
    """`retcode` is MT5's own vocabulary, not HTTP, and it must not be
    flattened into pass/fail -- 10009 means accepted, not filled."""

    def test_done_is_accepted_not_filled(self):
        status, why = M.map_retcode(10009)
        self.assertEqual(status, "accepted")
        self.assertIn("NOT filled", why)

    def test_money_and_price_failures_are_rejections(self):
        for code in (10013, 10014, 10015, 10016, 10017, 10018, 10021):
            self.assertEqual(M.map_retcode(code)[0], "rejected",
                             "retcode {} should be a rejection".format(code))

    def test_unknown_code_fails_closed(self):
        """A new MT5 code must not default to success."""
        status, why = M.map_retcode(99999)
        self.assertEqual(status, "rejected")
        self.assertIn("99999", why)

    def test_missing_retcode_is_not_success(self):
        self.assertEqual(M.map_retcode(None)[0], "rejected")
        self.assertFalse(M.is_accepted(None))

    def test_only_accepted_counts_as_accepted(self):
        accepted = [c for c in M._RETCODES if M.is_accepted(c)]
        self.assertEqual(accepted, [10009],
                         "more than one retcode claims acceptance")


# ------------------------------------------------------------- live gate
class TestLiveGateBlocksBeforeAnythingIsSent(unittest.TestCase):
    """A blocked order must never reach the terminal at all."""

    def _broker(self, **kw):
        kw.setdefault("live", False)
        return M.MT5Broker(**kw)

    def test_disabled_env_blocks(self):
        os.environ.pop("LIVE_TRADING_ENABLED", None)
        b = self._broker()
        with self.assertRaises(BrokerError) as cm:
            b.submit_order(OrderRequest(symbol="EURUSD", qty=0.1).validate())
        self.assertIn("LIVE_TRADING_ENABLED", str(cm.exception))

    def test_missing_confirmation_blocks_even_when_live(self):
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        try:
            b = self._broker(live=True, confirm=False)
            with self.assertRaises(BrokerError) as cm:
                b.submit_order(OrderRequest(symbol="EURUSD", qty=0.1).validate())
            self.assertIn("confirmation", str(cm.exception))
        finally:
            os.environ.pop("LIVE_TRADING_ENABLED", None)

    def test_tier_not_permitted_blocks(self):
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        try:
            b = self._broker(live=True, confirm=True, tier_live_enabled=False)
            with self.assertRaises(BrokerError):
                b.submit_order(OrderRequest(symbol="EURUSD", qty=0.1).validate())
        finally:
            os.environ.pop("LIVE_TRADING_ENABLED", None)


class TestBridgeBindingGuard(unittest.TestCase):
    def test_non_loopback_host_is_refused(self):
        with self.assertRaises(BrokerValidationError) as cm:
            M.build(host="192.168.1.50")
        self.assertIn("plaintext", str(cm.exception))

    def test_loopback_is_allowed(self):
        self.assertIsInstance(M.build(host="127.0.0.1"), M.MT5Broker)


# ----------------------------------------------------------------- queue
class TestBridgeState(unittest.TestCase):
    def setUp(self):
        self.s = BridgeState()

    def test_submit_then_take_is_fifo(self):
        a = self.s.submit({"op": "ping"})
        b = self.s.submit({"op": "account"})
        self.assertEqual(self.s.take()["id"], a)
        self.assertEqual(self.s.take()["id"], b)

    def test_take_on_empty_is_none(self):
        self.assertIsNone(self.s.take())

    def test_ids_are_unique(self):
        ids = {self.s.submit({"op": "ping"}) for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_result_round_trips(self):
        cid = self.s.submit({"op": "ping"})
        self.s.put_result({"id": cid, "ok": True, "data": {"v": 1}})
        r = self.s.take_result(cid)
        self.assertEqual(r["data"], {"v": 1})

    def test_result_is_consumed_once(self):
        cid = self.s.submit({"op": "ping"})
        self.s.put_result({"id": cid, "ok": True})
        self.assertIsNotNone(self.s.take_result(cid))
        self.assertIsNone(self.s.take_result(cid))

    def test_stale_command_is_dropped_not_executed(self):
        """A terminal closed for lunch must not place a forgotten order."""
        cid = self.s.submit({"op": "submit", "order": {"symbol": "X"}})
        self.s._pending[0]["queued_at"] = time.time() - 9999
        self.assertIsNone(self.s.take(),
                          "executed a command older than the age limit")
        self.assertIsNone(self.s.take_result(cid))

    def test_fresh_command_survives_alongside_a_stale_one(self):
        self.s.submit({"op": "old"})
        self.s._pending[0]["queued_at"] = time.time() - 9999
        fresh = self.s.submit({"op": "new"})
        self.assertEqual(self.s.take()["id"], fresh)

    def test_result_store_is_bounded(self):
        for _ in range(600):
            cid = self.s.submit({"op": "ping"})
            self.s.put_result({"id": cid, "ok": True})
        self.assertLessEqual(len(self.s._results), 500)

    def test_wait_for_times_out_as_none(self):
        self.assertIsNone(self.s.wait_for("nope", 0.05))


# ------------------------------------------------- end to end, with a fake EA
class FakeEA(threading.Thread):
    """Stands in for the MQL5 EA: poll /next, answer, post to /result."""

    daemon = True

    def __init__(self, base, token="", answer=None, delay=0.0, silent=False):
        super().__init__()
        self.base = base
        self.token = token
        self.answer = answer or (lambda cmd: {"ok": True, "data": {}})
        self.delay = delay
        self.silent = silent          # simulate a dead terminal
        self.seen = []
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _headers(self):
        h = {"X-Mafutani-Token": self.token} if self.token else {}
        return h

    def run(self):
        import json as _json
        import urllib.request as _r
        while not self._stop.is_set():
            try:
                req = _r.Request(self.base + "/next", headers=self._headers())
                with _r.urlopen(req, timeout=30) as r:
                    msg = _json.loads(r.read().decode())
            except Exception:
                time.sleep(0.05)
                continue
            cmd = msg.get("command")
            if not cmd:
                continue
            self.seen.append(cmd)
            if self.silent:
                continue
            if self.delay:
                time.sleep(self.delay)
            try:
                res = self.answer(cmd)
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            res["id"] = cmd["id"]
            try:
                body = _json.dumps(res).encode()
                rq = _r.Request(self.base + "/result", data=body, method="POST",
                                headers=dict(self._headers(),
                                             **{"Content-Type": "application/json"}))
                _r.urlopen(rq, timeout=10).read()
            except Exception:
                pass


class _LiveBridge:
    """Runs the real server on an ephemeral port."""

    def __init__(self, token=""):
        self.token = token
        self.thread = None

    def __enter__(self):
        import socketserver
        from http.server import ThreadingHTTPServer
        from services.mt5.bridge_server import BridgeState, _Handler
        self.state = BridgeState()
        handler = type("H", (_Handler,),
                       {"state": self.state, "token": self.token})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.port)
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestEAQueryStringAuth(unittest.TestCase):
    """The carrier the MQL5 EA is forced to use.

    MQL5's WebRequest has no overload that sets headers *and* a body, so
    the EA cannot send `X-Mafutani-Token` on its POST /result and puts the
    token in the URL instead. That was found by compiling against a real
    MetaEditor, not by reading documentation, so it is pinned here: if the
    bridge stops accepting the query form the EA silently stops being able
    to report any result at all, and every order looks like a timeout.
    """

    def test_token_in_the_query_string_is_accepted(self):
        with _LiveBridge(token="s3cret") as b:
            import json as _json
            import urllib.request as _r
            # Exactly what the EA does: token in the URL, no header.
            # /next is a long-poll that holds ~25s when the queue is empty,
            # so a command is queued first to make it return at once --
            # otherwise this test needs a 25s timeout just to assert nothing.
            b.state.submit({"op": "ping"})
            with _r.urlopen(b.base + "/next?token=s3cret", timeout=10) as r:
                self.assertEqual(_json.loads(r.read().decode())["ok"], True)

            body = _json.dumps({"id": "x", "ok": True}).encode()
            rq = _r.Request(b.base + "/result?token=s3cret", data=body,
                            method="POST",
                            headers={"Content-Type": "application/json"})
            _r.urlopen(rq, timeout=10).read()
            self.assertEqual(b.state.take_result("x")["ok"], True)

    def test_query_token_is_still_rejected_when_wrong(self):
        import urllib.error
        import urllib.request as _r
        with _LiveBridge(token="s3cret") as b:
            # /health answers immediately, so the 401 lands before any
            # long-poll timeout could mask it.
            with self.assertRaises(urllib.error.HTTPError) as cm:
                _r.urlopen(b.base + "/health?token=nope", timeout=10)
            self.assertEqual(cm.exception.code, 401)

    def test_header_token_still_works(self):
        """The Python side keeps using the header; both carriers stay live."""
        import json as _json
        import urllib.request as _r
        with _LiveBridge(token="s3cret") as b:
            req = _r.Request(b.base + "/health",
                             headers={"X-Mafutani-Token": "s3cret"})
            with _r.urlopen(req, timeout=10) as r:
                self.assertEqual(_json.loads(r.read().decode())["ok"], True)

    def test_routing_ignores_the_query_string(self):
        """/health?token=.. must route as /health, not 404."""
        import json as _json
        import urllib.request as _r
        with _LiveBridge(token="s3cret") as b:
            with _r.urlopen(b.base + "/health?token=s3cret", timeout=10) as r:
                self.assertEqual(_json.loads(r.read().decode())["ok"], True)


class TestEndToEndWithFakeEA(unittest.TestCase):
    def setUp(self):
        os.environ.pop("LIVE_TRADING_ENABLED", None)

    def _broker(self, bridge, **kw):
        kw.setdefault("timeout", 15.0)
        kw.setdefault("host", "127.0.0.1")
        kw.setdefault("port", bridge.port)
        return M.MT5Broker(**kw)

    def test_ping_round_trip(self):
        with _LiveBridge() as b:
            ea = FakeEA(b.base,
                        answer=lambda c: {"ok": True,
                                          "data": {"build": 4500, "account": "1"}})
            ea.start()
            try:
                broker = self._broker(b).connect()
                self.assertTrue(broker.connected)
                self.assertEqual(broker.terminal_info()["build"], 4500)
            finally:
                ea.stop()

    def test_submit_reports_open_never_filled(self):
        """The invariant. 10009 accepted must not become status 'filled'."""
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        try:
            with _LiveBridge() as b:
                ea = FakeEA(b.base, answer=lambda c: {
                    "ok": True,
                    "data": {"ticket": 555, "retcode": 10009, "price": 1.085}})
                ea.start()
                try:
                    br = self._broker(b, live=True, confirm=True)
                    order = br.submit_order(
                        OrderRequest(symbol="EURUSD", side="buy",
                                     qty=0.1).validate())
                    self.assertEqual(order.status, "open")
                    self.assertNotEqual(order.status, "filled")
                    self.assertEqual(order.filled_qty, 0.0)
                    self.assertFalse(order.raw["filled"])
                    self.assertIn("NOT filled", order.raw["retcode_meaning"])
                    self.assertEqual(order.order_id, "555")
                finally:
                    ea.stop()
        finally:
            os.environ.pop("LIVE_TRADING_ENABLED", None)

    def test_client_order_id_survives_to_the_ea(self):
        """The only thing tying our order to the terminal's ticket."""
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        try:
            with _LiveBridge() as b:
                ea = FakeEA(b.base, answer=lambda c: {
                    "ok": True, "data": {"ticket": 7, "retcode": 10009}})
                ea.start()
                try:
                    req = OrderRequest(symbol="EURUSD", qty=0.1).validate()
                    self._broker(b, live=True, confirm=True).submit_order(req)
                    self.assertEqual(ea.seen[0]["order"]["client_order_id"],
                                     req.client_order_id)
                finally:
                    ea.stop()
        finally:
            os.environ.pop("LIVE_TRADING_ENABLED", None)

    def test_rejection_raises_rather_than_returning_an_order(self):
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        try:
            with _LiveBridge() as b:
                ea = FakeEA(b.base, answer=lambda c: {
                    "ok": True, "data": {"retcode": 10013}})
                ea.start()
                try:
                    br = self._broker(b, live=True, confirm=True)
                    with self.assertRaises(BrokerRejectedError) as cm:
                        br.submit_order(OrderRequest(symbol="EURUSD",
                                                      qty=0.1).validate())
                    self.assertIn("no money", str(cm.exception))
                finally:
                    ea.stop()
        finally:
            os.environ.pop("LIVE_TRADING_ENABLED", None)

    def test_silent_terminal_times_out_and_says_so(self):
        """The dangerous case: the result is lost, not the order.

        A timeout must NOT look like success and must NOT retry on its own.
        """
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        try:
            with _LiveBridge() as b:
                ea = FakeEA(b.base, silent=True)     # never answers
                ea.start()
                try:
                    br = self._broker(b, live=True, confirm=True, timeout=1.5)
                    with self.assertRaises(BrokerUnavailableError) as cm:
                        br.submit_order(OrderRequest(symbol="EURUSD",
                                                      qty=0.1).validate())
                    msg = str(cm.exception)
                    self.assertIn("reconcile", msg)
                    self.assertIn("may or may not have", msg)
                finally:
                    ea.stop()
        finally:
            os.environ.pop("LIVE_TRADING_ENABLED", None)

    def test_wrong_token_is_refused(self):
        """The *broker's* token is wrong, so the bridge refuses at the door.

        Worth separating from the "EA is silent" case: that one produces a
        504 after a wait, this one is an immediate 401. They need different
        operator responses -- a misconfigured token is not a dead terminal.
        """
        with _LiveBridge(token="s3cret") as b:
            ea = FakeEA(b.base, token="s3cret",
                        answer=lambda c: {"ok": True, "data": {}})
            ea.start()
            try:
                br = M.MT5Broker(host="127.0.0.1", port=b.port,
                                 token="wrong-one", timeout=5.0)
                with self.assertRaises(BrokerAuthError) as cm:
                    br.connect()
                self.assertIn("MAFUTANI_MT5_TOKEN", str(cm.exception))
            finally:
                ea.stop()

    def test_ea_with_the_wrong_token_simply_never_polls(self):
        """The mirror image: a misconfigured EA looks exactly like a dead
        terminal, and reports as a timeout rather than an auth failure."""
        with _LiveBridge(token="s3cret") as b:
            ea = FakeEA(b.base, token="wrong-one", silent=True)
            ea.start()
            try:
                br = M.MT5Broker(host="127.0.0.1", port=b.port,
                                 token="s3cret", timeout=1.0)
                with self.assertRaises(BrokerUnavailableError):
                    br._call({"op": "ping", "_timeout": 1.0})
                self.assertEqual(ea.seen, [], "EA polled despite a bad token")
            finally:
                ea.stop()

    def test_correct_token_is_accepted(self):
        with _LiveBridge(token="s3cret") as b:
            ea = FakeEA(b.base, token="s3cret",
                        answer=lambda c: {"ok": True, "data": {"build": 1}})
            ea.start()
            try:
                br = M.MT5Broker(host="127.0.0.1", port=b.port,
                                 token="s3cret", timeout=10.0)
                self.assertTrue(br.connect().connected)
            finally:
                ea.stop()

    def test_no_ea_at_all_is_unavailable_not_a_silent_success(self):
        with _LiveBridge() as b:
            br = self._broker(b, timeout=1.5)
            with self.assertRaises(BrokerUnavailableError):
                br.submit_order.__self__  # noqa: B018 - attribute sanity
                br._call({"op": "ping"}, timeout=1.5)


class TestReadOperations(unittest.TestCase):
    def setUp(self):
        os.environ.pop("LIVE_TRADING_ENABLED", None)

    def test_quote_normalises_to_our_type(self):
        with _LiveBridge() as b:
            ea = FakeEA(b.base, answer=lambda c: {
                "ok": True, "data": {"bid": 1.0840, "ask": 1.0844,
                                     "bid_size": 3.0, "ask_size": 2.0,
                                     "last": 1.0842, "ts": 1700000000}})
            ea.start()
            try:
                q = M.MT5Broker(host="127.0.0.1", port=b.port,
                                timeout=10.0).quote("eurusd")
                self.assertEqual(q.symbol, "EURUSD")
                self.assertAlmostEqual(q.spread, 0.0004, places=6)
            finally:
                ea.stop()

    def test_positions_parse(self):
        with _LiveBridge() as b:
            ea = FakeEA(b.base, answer=lambda c: {"ok": True, "data": {
                "positions": [{"symbol": "EURUSD", "qty": 0.2,
                               "avg_entry": 1.08, "realized_pnl": 3.5,
                               "ts": 1}]}})
            ea.start()
            try:
                ps = M.MT5Broker(host="127.0.0.1", port=b.port,
                                 timeout=10.0).positions()
                self.assertEqual(len(ps), 1)
                self.assertEqual(ps[0].symbol, "EURUSD")
                self.assertAlmostEqual(ps[0].realized_pnl, 3.5)
            finally:
                ea.stop()

    def test_ea_error_becomes_rejection_not_silence(self):
        with _LiveBridge() as b:
            ea = FakeEA(b.base,
                        answer=lambda c: {"ok": False, "error": "not logged in"})
            ea.start()
            try:
                br = M.MT5Broker(host="127.0.0.1", port=b.port, timeout=10.0)
                # Through _read, which is the path every read op uses.
                with self.assertRaises(BrokerRejectedError) as cm:
                    br._read({"op": "account"})
                self.assertIn("not logged in", str(cm.exception))
            finally:
                ea.stop()


class TestServerStartupGuards(unittest.TestCase):
    """Startup refusals, checked without binding a socket."""

    def test_no_token_and_not_insecure_refuses(self):
        with self.assertRaises(SystemExit) as cm:
            serve(host="127.0.0.1", port=0, token="", insecure_no_token=False)
        self.assertIn("token", str(cm.exception).lower())

    def test_non_loopback_bind_refuses_without_override(self):
        with self.assertRaises(SystemExit) as cm:
            serve(host="0.0.0.0", port=0, token="t", insecure_no_token=False)
        self.assertIn("place orders", str(cm.exception))

    def test_insecure_override_is_honoured(self):
        """The escape hatch works, so the refusal is a real choice."""
        import socketserver
        from http.server import ThreadingHTTPServer
        state = BridgeState()
        handler = type("H", (__import__(
            "services.mt5.bridge_server", fromlist=["_Handler"])._Handler,),
            {"state": state, "token": ""})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.server_close()   # bound fine; we only proved the guard passes


if __name__ == "__main__":
    unittest.main(verbosity=2)

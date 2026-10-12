"""Drive the real MT5 bridge server end to end. Stdlib only, no terminal.

    python3 scripts/mt5_preflight.py

What this is for
----------------
`tests/test_mt5_bridge.py` exercises the bridge with a server constructed
inside the test process. That covers the logic but not the shipped binary:
it never runs `bridge_server.py` as an actual subprocess, never opens a real
socket to it, and never goes through `argparse`. Those are exactly the
seams where a working module fails to work as a program.

So this starts the real server as a subprocess on a free port, waits for it
to accept connections, and drives the whole documented sequence through the
real `MT5Broker` adapter: ping, account, quote, submit, cancel, positions.

An EA cannot be simulated perfectly, but `SimEA` polls `/next` and posts to
`/result` with the same protocol the MQL5 EA uses -- including sending its
token as a query parameter, because MQL5 cannot set headers on a POST. The
one thing this cannot verify is the MQL5 itself.

Exit code is the useful part: 0 means the bridge and adapter are wired
correctly, so a later failure is on the EA side and the operator knows where
to look. It does not mean MT5 is working.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BRIDGE = os.path.join(ROOT, "services", "mt5", "bridge_server.py")

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class _SilentDidNotTimeOut(Exception):
    """Raised when submit_order returns against a bridge with no EA.

    A distinct type, not a bare AssertionError, because the surrounding
    `except BrokerError` would otherwise be reached by an AssertionError
    propagating from inside -- and the check would report success for a
    failure.
    """


def _c(code, s):
    return "%s%s%s" % (code, s, OFF) if sys.stdout.isatty() else s


class SimEA(threading.Thread):
    """Stands in for MafutaniBridge.mq5 -- same protocol, no terminal.

    Deliberately reproduces the EA's constraint: the token goes in the URL
    and no `X-Mafutani-Token` header is sent, because MQL5's WebRequest
    cannot set headers on a request with a body. If the bridge ever stops
    accepting that form, the real EA stops working, and this is where that
    shows up.
    """

    daemon = True

    def __init__(self, base, token="", handler=None, mute=False):
        super().__init__()
        self.base = base
        self.token = token
        self.handler = handler or self._default
        # `mute` receives commands and never answers. That models a terminal
        # that vanished mid-order far better than "stop the thread" does: a
        # stopped thread is still parked inside a long-poll and will happily
        # answer a command submitted afterwards, which is exactly how the
        # first version of this check passed for the wrong reason.
        self.mute = mute
        self.handled = 0
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _url(self, path):
        return "%s%s?token=%s" % (self.base, path, self.token)

    def _default(self, cmd):
        op = cmd.get("op")
        if op == "ping":
            return {"ok": True, "data": {"build": 4750,
                                          "terminal": "MetaTrader 5",
                                          "company": "simulated",
                                          "account": "0",
                                          "trade_allowed": True,
                                          "trade_context_busy": False}}
        if op == "account":
            return {"ok": True, "data": {"cash": 10000.0, "equity": 10000.0,
                                          "buying_power": 5000.0,
                                          "currency": "USD", "account_id": "0"}}
        if op == "quote":
            return {"ok": True, "data": {"bid": 1.08400, "ask": 1.08440,
                                          "last": 1.08420, "bid_size": 3.0,
                                          "ask_size": 2.0, "ts": 1700000000}}
        if op == "positions":
            return {"ok": True, "data": {"positions": []}}
        if op == "submit":
            return {"ok": True, "data": {"retcode": 10009, "ticket": 42,
                                          "price": 1.0842}}
        if op == "cancel":
            return {"ok": True, "data": {"retcode": 10009, "ticket": 42}}
        return {"ok": False, "error": "unknown op %s" % op}

    def run(self):
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self._url("/next"), timeout=30) as r:
                    msg = json.loads(r.read().decode())
            except Exception:
                time.sleep(0.1)
                continue
            cmd = msg.get("command")
            if not cmd:
                continue
            if self.mute:
                # Take the command and drop it. This is the dangerous case:
                # the bridge believes an EA is alive, and the app is waiting
                # on a result that will never come.
                self.handled += 1
                continue
            try:
                res = self.handler(cmd)
            except Exception as e:
                res = {"ok": False, "error": "sim raised %s" % e}
            res["id"] = cmd.get("id")
            try:
                body = json.dumps(res).encode()
                req = urllib.request.Request(
                    self._url("/result"), data=body, method="POST",
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
                self.handled += 1
            except Exception:
                pass


def wait_for_port(host, port, timeout=20.0):
    """Block until the bridge accepts a connection, or give up."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    token = os.environ.get("MAFUTANI_MT5_TOKEN", "preflight-token")
    port = free_port()
    host = "127.0.0.1"

    print(_c(DIM, "MT5 bridge preflight — real server subprocess, simulated EA"))
    print(_c(DIM, "This does NOT verify MQL5 or your broker account.\n"))

    proc = subprocess.Popen(
        [sys.executable, BRIDGE, "--host", host, "--port", str(port),
         "--token", token],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ea = None
    failures = []

    try:
        if not wait_for_port(host, port):
            print(_c(RED, "  FAIL  bridge did not start listening"))
            proc.terminate()
            print((proc.stdout.read() or "")[:2000])
            return 1
        print(_c(GREEN, "  ok    bridge_server.py started and is listening"))

        from brokers.base import (BrokerAuthError, BrokerError,
                                  BrokerRejectedError, OrderRequest)
        from brokers.mt5 import MT5Broker

        def broker(**kw):
            kw.setdefault("host", host)
            kw.setdefault("port", port)
            kw.setdefault("token", token)
            kw.setdefault("timeout", 15.0)
            return MT5Broker(**kw)

        def check(label, fn):
            try:
                value = fn()
                print(_c(GREEN, "  ok    %-22s %s" % (label, value if value else "")))
                return value
            except Exception as e:
                print(_c(RED, "  FAIL  %-22s %s: %s"
                         % (label, type(e).__name__, str(e)[:90])))
                failures.append(label)
                return None

        # -- wrong token must be refused, before anything else ------------
        def wrong_token():
            bad = MT5Broker(host=host, port=port, token="wrong", timeout=8.0)
            bad.connect()
            raise AssertionError("a wrong token was accepted")
        try:
            wrong_token()
            failures.append("wrong-token-refused")
            print(_c(RED, "  FAIL  %-22s a wrong token was accepted"
                     % "wrong-token-refused"))
        except BrokerAuthError:
            print(_c(GREEN, "  ok    %-22s" % "wrong-token-refused"))
        except Exception as e:
            print(_c(RED, "  FAIL  %-22s %s"
                     % ("wrong-token-refused", type(e).__name__)))
            failures.append("wrong-token-refused")

        # -- no EA yet: must fail, not hang or pretend ------------------
        def no_ea():
            b = broker(timeout=2.0)
            b._call({"op": "ping", "_timeout": 1.0})
            raise AssertionError("returned without an EA")
        try:
            no_ea()
            failures.append("no-ea-is-unavailable")
            print(_c(RED, "  FAIL  %-22s returned without an EA" % "no-ea-is-unavailable"))
        except BrokerError as e:
            if "reconcile" in str(e):
                print(_c(GREEN, "  ok    %-22s"
                         % "no-ea-is-unavailable"))
            else:
                print(_c(YELLOW, "  warn  %-22s %s"
                         % "no-ea-is-unavailable", str(e)[:60]))

        # -- now start the simulated EA ---------------------------------
        ea = SimEA("http://%s:%d" % (host, port), token=token)
        ea.start()
        time.sleep(0.3)

        info = check("connect()", lambda: "build %s" % broker().connect().terminal_info().get("build"))
        check("account()", lambda: "cash %.2f" % broker().account()["cash"])
        q = check("quote()", lambda: broker().quote("EURUSD"))
        if q:
            print(_c(DIM, "        spread %.5f" % q.spread))
        check("positions()", lambda: "[]" if not broker().positions() else "n")

        # -- the live gate must refuse before anything is queued --------
        def gated():
            os.environ.pop("LIVE_TRADING_ENABLED", None)
            b = broker(live=True, confirm=True)
            b.submit_order(OrderRequest(symbol="EURUSD", qty=0.1).validate())
            raise AssertionError("submitted with live trading disabled")
        try:
            gated()
            failures.append("live-gate-blocks")
            print(_c(RED, "  FAIL  %-22s submitted while live is disabled"
                     % "live-gate-blocks"))
        except BrokerError as e:
            if "LIVE_TRADING_ENABLED" in str(e):
                print(_c(GREEN, "  ok    %-22s" % "live-gate-blocks"))
            else:
                print(_c(YELLOW, "  warn  %-22s %s"
                         % ("live-gate-blocks", str(e)[:60])))

        # -- now the real submit, with the gate satisfied ----------------
        def submit():
            os.environ["LIVE_TRADING_ENABLED"] = "1"
            try:
                b = broker(live=True, confirm=True)
                req = OrderRequest(symbol="EURUSD", side="buy",
                                   qty=0.1).validate()
                o = b.submit_order(req)
                if o.status == "filled":
                    raise AssertionError(
                        "retcode 10009 was reported as filled; that is the "
                        "invariant this adapter exists to hold")
                return "status=%s retcode=%s" % (o.status,
                                                 o.raw.get("retcode"))
            finally:
                os.environ.pop("LIVE_TRADING_ENABLED", None)
        check("submit_order()", submit)
        check("cancel_order()", lambda: broker(live=False).cancel_order(42).get("status"))

        # -- a muted EA must produce the reconcile warning ---------------
        # Swap in a *muting* EA rather than stopping the thread. A stopped
        # thread is still parked inside a long-poll and answers anyway, which
        # made the first version of this check report a timeout that never
        # actually happened.
        ea.stop()
        mute_ea = SimEA("http://%s:%d" % (host, port), token=token, mute=True)
        mute_ea.start()
        ea = mute_ea
        time.sleep(0.5)

        def silent():
            b = broker(live=True, confirm=True)
            os.environ["LIVE_TRADING_ENABLED"] = "1"
            try:
                # Short bridge wait so this check does not sit for the
                # default 20 seconds.
                b._call({"op": "ping", "_timeout": 3.0})
            finally:
                os.environ.pop("LIVE_TRADING_ENABLED", None)
            raise _SilentDidNotTimeOut()
        try:
            silent()
            failures.append("muted-ea-warns")
            print(_c(RED, "  FAIL  %-22s did not time out" % "muted-ea-warns"))
        except _SilentDidNotTimeOut:
            # submit_order returned against a bridge whose EA is swallowing
            # commands. A distinct exception type, because an earlier
            # version raised AssertionError here and the surrounding
            # `except BrokerError` was reached by an unrelated failure and
            # reported success.
            failures.append("muted-ea-warns")
            print(_c(RED, "  FAIL  %-22s did not time out" % "muted-ea-warns"))
        except BrokerError as e:
            if "reconcile" in str(e):
                print(_c(GREEN, "  ok    %-22s (%d command(s) swallowed)"
                         % ("muted-ea-warns", mute_ea.handled)))
            else:
                failures.append("muted-ea-warns")
                print(_c(RED, "  FAIL  %-22s wrong error: %s"
                         % ("muted-ea-warns", str(e)[:60])))

    finally:
        if ea is not None:
            ea.stop()
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    print()
    if failures:
        print(_c(RED, "  %d check(s) failed: %s"
                 % (len(failures), ", ".join(failures))))
        return 1
    print(_c(GREEN, "  bridge + adapter are wired correctly."))
    print(_c(DIM, "\n  Still unverified: the MQL5 EA inside a running terminal,"))
    print(_c(DIM, "  the WebRequest whitelist, and OrderSend on a real account.\n"))
    print("  Next: open MT5, attach MafutaniBridge with AllowTrading=false,")
    print("  whitelist http://%s:%d, and check the Experts log." % (host, port))
    return 0


if __name__ == "__main__":
    sys.exit(main())

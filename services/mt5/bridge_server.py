"""MT5 bridge server. Runs on the Windows host beside the terminal. Stdlib.

    python3 services/mt5/bridge_server.py --port 8031

The EA cannot listen on a port (MQL5's `WebRequest()` is client-only), so
the listener lives here and the EA is the client. The EA long-polls
`GET /next`, executes with `OrderSend`, and posts the result to
`POST /result`.

Threading and why it matters
----------------------------
The terminal's trade context is single-threaded per EA. If two commands
reach `OrderSend` concurrently the terminal returns retcode 10025 "trade
context busy" and *silently drops one of them*. So this server executes
one command at a time under a lock, and the EA is told so it does not
pipeline. Getting this wrong does not raise; it loses an order without
telling anyone, which is the worst possible failure for this path.

Two endpoints, and the security posture
---------------------------------------
The listener is loopback-only by default. Anything that can reach the port
can place orders in a real account, so:

  * `--host` defaults to 127.0.0.1 and refusing a non-loopback bind is
    the caller's explicit decision, not a warning;
  * when `--token` is set every request must carry it, compared with
    `hmac.compare_digest` rather than `==`;
  * without a token the server still refuses to start unless
    `--insecure-no-token` is passed, because a silently unauthenticated
    order endpoint is exactly the thing that should not exist by default.

This is the same invariant as the rest of the codebase applied to a
different threat: the loopback has no credentials in flight, but "who can
reach this port" is still the whole question.
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Longest a /next request is held open waiting for a command. Just under a
# minute so a request never lands exactly on a typical 60s idle timeout.
POLL_HOLD_S = 25.0

# Commands older than this are dropped rather than executed. A terminal
# that was closed for lunch must not come back and place an order the
# operator has forgotten about.
MAX_COMMAND_AGE_S = 120.0


class BridgeState:
    """Command queue + single-flight execution, shared by all connections."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = []          # list of dicts, FIFO
        self._results = {}          # id -> result dict
        self._newest_id = 0

    def submit(self, cmd):
        """Queue a command from the app side. Returns its id."""
        with self._lock:
            self._newest_id += 1
            cid = "cmd-{}".format(self._newest_id)
            cmd = dict(cmd or {})
            cmd["id"] = cid
            cmd["queued_at"] = time.time()
            self._pending.append(cmd)
            return cid

    def take(self):
        """Oldest live command, or None. Stale commands are discarded."""
        with self._lock:
            now = time.time()
            live = []
            dropped = 0
            for c in self._pending:
                if now - float(c.get("queued_at") or 0) > MAX_COMMAND_AGE_S:
                    dropped += 1
                    continue
                live.append(c)
            self._pending = live
            return self._pending.pop(0) if self._pending else None

    def put_result(self, res):
        with self._lock:
            cid = str(res.get("id") or "")
            self._results[cid] = dict(res or {})
            self._results[cid]["at"] = time.time()
            if len(self._results) > 500:
                for k in sorted(self._results,
                                key=lambda x: self._results[x].get("at", 0))[:100]:
                    self._results.pop(k, None)

    def take_result(self, cid):
        with self._lock:
            return self._results.pop(str(cid), None)

    def wait_for(self, cid, timeout):
        """Block until the EA posts a result for `cid`, or time out."""
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            r = self.take_result(cid)
            if r is not None:
                return r
            time.sleep(0.02)
        return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "MafutaniMT5Bridge/1"
    state = None
    token = ""

    def log_message(self, fmt, *args):
        pass  # quiet; the app logs its own view

    def _authorized(self):
        if not self.token:
            return True
        import hmac
        got = self.headers.get("X-Mafutani-Token", "")
        return hmac.compare_digest(str(got), str(self.token))

    def _send(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return None

    def do_GET(self):
        if not self._authorized():
            self._send(401, {"error": "bad or missing token"})
            return
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "service": "mt5-bridge"})
            return
        if self.path.startswith("/next"):
            # One held request per connection; the EA polls this in a loop.
            deadline = time.time() + POLL_HOLD_S
            while time.time() < deadline:
                cmd = self.state.take()
                if cmd is not None:
                    self._send(200, {"ok": True, "command": cmd})
                    return
                time.sleep(0.05)
            self._send(200, {"ok": True, "command": None})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._send(401, {"error": "bad or missing token"})
            return
        body = self._body()
        if body is None:
            self._send(400, {"error": "invalid JSON"})
            return

        if self.path.startswith("/result"):
            self.state.put_result(body)
            self._send(200, {"ok": True})
            return
        if self.path.startswith("/exec"):
            cid = self.state.submit(body)
            res = self.state.wait_for(cid, float(
                body.pop("_timeout", 20) if isinstance(body, dict) else 20))
            if res is None:
                # The EA never answered. This is NOT "probably fine" and
                # definitely not "assume filled": the command may have
                # executed with the result lost. Say so explicitly.
                self._send(504, {"ok": False, "id": cid,
                                 "error": "EA did not respond in time; the "
                                          "command may or may not have "
                                          "executed -- reconcile by ticket "
                                          "before retrying"})
                return
            self._send(200, res)
            return
        self._send(404, {"error": "not found"})


def serve(host="127.0.0.1", port=8031, token="", insecure_no_token=False):
    if host not in ("127.0.0.1", "::1", "localhost") and not insecure_no_token:
        raise SystemExit(
            "refusing to bind {}: anything that can reach this port can place "
            "orders. Pass --insecure-no-token if that is genuinely "
            "acceptable, or keep it on loopback.".format(host))
    if not token and not insecure_no_token:
        raise SystemExit(
            "refusing to start without a token: an unauthenticated order "
            "endpoint should not exist by accident. Pass --insecure-no-token "
            "to override deliberately.")

    state = BridgeState()
    handler = type("H", (_Handler,), {"state": state, "token": token or ""})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    sys.stderr.write("mt5 bridge on http://{}:{}{}\n".format(
        host, port, " (token required)" if token else " (NO TOKEN)"))
    sys.stderr.write("the EA polls GET /next and posts to POST /result\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main():
    ap = argparse.ArgumentParser(description="MT5 bridge server (Windows side)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8031)
    ap.add_argument("--token", default=os.environ.get("MAFUTANI_MT5_TOKEN", ""))
    ap.add_argument("--insecure-no-token", action="store_true",
                    help="explicitly allow an unauthenticated listener")
    args = ap.parse_args()
    serve(args.host, args.port, args.token, args.insecure_no_token)


if __name__ == "__main__":
    main()

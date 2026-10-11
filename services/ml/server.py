"""Torch model service. Runs OUTSIDE Mafutani, in .venv-torch.

    .venv-torch/bin/python services/ml/server.py --port 8021

Deliberately separate from the app. Mafutani is stdlib-only; this process is
the only thing in the repository that may import torch, and the app reaches
it over plain HTTP via `engines.mlclient`. If this process is not running,
the app carries on unchanged -- the client raises, callers degrade, and
nothing in the trading path fails because a model server is absent.

Endpoints
---------
GET  /health   -> {"ok", "torch", "version", "device"}
POST /fit      -> fit an impact model from this deployment's own transitions
POST /predict  -> apply a fitted model to one observation

Every response is JSON and every failure is a 4xx/5xx with a reason string.
The contract is deliberately dull, because the interesting logic lives in
model.py and duplicating error handling in two places is how they drift.
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch  # noqa: E402  (only importable in this venv, by design)

import model as _model  # noqa: E402

CONTRACT = "mafutani.ml/1"
DEFAULT_PORT = 8021
MAX_BODY = 16 * 1024 * 1024
_STARTED = time.time()


def _warmup():
    """Pay torch's lazy initialisation at boot, not on the first request.

    The first optimiser call in a fresh process spins up torch's thread pool
    and took 23 seconds on this machine; every call after it took 0.03s. That
    23s landed on a real /fit, where the client's 10s timeout expired and the
    caller saw a transport error indistinguishable from the service being
    down. Warming here means the first genuine request costs milliseconds.

    Uses a synthetic fit with *more than* MIN_OBS rows, so it actually
    reaches the optimiser. An earlier version passed four rows, which is
    below MIN_OBS, so fit() returned early without ever constructing an LBFGS
    -- and the warmup warmed up nothing. The first real /fit then paid the
    full 22s and blew the client's timeout. Initialisation only happens on
    the path that runs, so the warmup has to run that path.

    The result is discarded; nothing here can become a usable model because
    this payload is synthetic and never leaves the process.
    """
    try:
        n = _model.MIN_OBS + 10
        xs = [float(i) for i in range(n)]
        _model.fit(xs, [10.0 + (i % 3) for i in range(n)],
                   [0.01 * i for i in range(n)], steps=5)
        torch.zeros(2).sum()
        return True
    except Exception as e:
        sys.stderr.write("[ml] warmup failed: {}\n".format(e))
        return False


def _device():
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


HEALTH = {"contract": CONTRACT, "ok": True,
          "torch": torch.__version__,
          "cuda": bool(torch.cuda.is_available()),
          "device": _device(),
          "min_obs": _model.MIN_OBS}


def _finite(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _clean_triples(payload):
    """(ofi, depth, dmid) triples, rejecting anything non-finite.

    A NaN reaching a fit does not raise -- it quietly produces NaN
    coefficients, and NaN coefficients fail open at exactly the moment
    someone trusts them. So they are dropped here, before the optimiser.
    """
    ofi, depth, dm = [], [], []
    for row in (payload.get("samples") or []):
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        a, b, c = _finite(row[0]), _finite(row[1]), _finite(row[2])
        if a is None or b is None or c is None or b <= 0:
            continue
        ofi.append(a)
        depth.append(b)
        dm.append(c)
    return ofi, depth, dm


class Handler(BaseHTTPRequestHandler):
    server_version = "mafutani-ml/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[ml] %s\n" % (fmt % args))

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/"):
            h = dict(HEALTH)
            h["uptime_s"] = round(time.time() - _STARTED, 1)
            return self._send(200, h)
        return self._send(404, {"contract": CONTRACT,
                                "error": "no such endpoint: {}".format(self.path)})

    def do_POST(self):
        route = self.path.split("?")[0]
        if route not in ("/fit", "/predict"):
            return self._send(404, {"contract": CONTRACT,
                                    "error": "no such endpoint: {}".format(self.path)})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._send(400, {"contract": CONTRACT, "error": "empty body"})
        if length > MAX_BODY:
            return self._send(413, {"contract": CONTRACT,
                                    "error": "body too large ({} bytes)".format(length)})
        try:
            req = json.loads(self.rfile.read(length).decode())
        except Exception as e:
            return self._send(400, {"contract": CONTRACT,
                                    "error": "malformed JSON: {}".format(e)})
        if not isinstance(req, dict):
            return self._send(400, {"contract": CONTRACT,
                                    "error": "body must be a JSON object"})
        ver = req.get("contract")
        if ver and ver != CONTRACT:
            return self._send(409, {
                "contract": CONTRACT,
                "error": "contract mismatch: service speaks {}, caller sent {!r}"
                         .format(CONTRACT, ver)})

        if route == "/fit":
            return self._fit(req)
        return self._predict(req)

    def _fit(self, req):
        ofi, depth, dm = _clean_triples(req)
        if not ofi:
            return self._send(400, {"contract": CONTRACT,
                                    "error": "no usable samples: need "
                                             "(ofi, depth>0, mid_change) triples"})
        lam = _finite(req.get("ridge_lambda"))
        if lam is None or lam <= 0:
            lam = _model.MIN_OBS * 0 + 1e-4
        try:
            res = _model.fit(ofi, depth, dm, ridge_lambda=lam)
        except Exception as e:
            return self._send(500, {"contract": CONTRACT,
                                    "error": "fit failed: {}".format(e)})
        se = _model.standard_errors(ofi, depth, dm, ridge_lambda=lam)
        res["standard_error_beta"] = se.get("se_beta")
        res["t_beta"] = se.get("t_beta")
        res["contract"] = CONTRACT
        res["device"] = _device()
        return self._send(200, res)

    def _predict(self, req):
        m = req.get("model") or {}
        norm = _finite(req.get("ofi_norm"))
        depth = _finite(req.get("depth"))
        if norm is None or depth is None:
            return self._send(400, {"contract": CONTRACT,
                                    "error": "ofi_norm and depth required, "
                                             "both finite"})
        if depth <= 0:
            return self._send(400, {"contract": CONTRACT,
                                    "error": "depth must be > 0"})
        val = _model.predict_one(m, norm, depth)
        if val is None:
            return self._send(422, {
                "contract": CONTRACT,
                "error": "model is not fitted (ok={})".format(m.get("ok"))})
        return self._send(200, {"contract": CONTRACT, "predicted_dmid": val,
                                "model_n": m.get("n"),
                                "model_r2": m.get("r2")})


def main():
    ap = argparse.ArgumentParser(description="Mafutani torch model service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "::1", "localhost") and not os.environ.get(
            "MAFUTANI_ML_ALLOW_PUBLIC"):
        sys.stderr.write("refusing to bind {} without MAFUTANI_ML_ALLOW_PUBLIC=1\n"
                         .format(args.host))
        return 2

    banner = (
        "\n"
        "  ============================================================\n"
        "   Mafutani model service   contract {}   torch {}\n"
        "   device: {}\n"
        "   The app stays stdlib-only; it reaches this over HTTP.\n"
        "   http://{}:{}/health\n"
        "  ============================================================\n"
    ).format(CONTRACT, torch.__version__, _device(), args.host, args.port)
    sys.stderr.write(banner)

    warm = _warmup()
    sys.stderr.write("[ml] warmup {}\n".format("ok" if warm else "FAILED"))

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    def _shutdown(_s, _f):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""Reference forecast service -- a random walk, speaking mfutani.forecast/1.

Read the banner this prints before anything else:

    THIS IS NOT KRONOS. It is a random walk with drift.

That sentence is the whole point of this file. It is not scaffolding to be
deleted once the real model lands -- it is the benchmark the real model has
to beat, and it is the reason the contract is worth having.

Why a random walk is the correct benchmark
------------------------------------------
Under the efficient-market hypothesis the best available forecast is that the
next bar equals the last one. Anything that reliably beats that is an edge,
and anything that claims to is either carrying information the market has not
priced, or carrying an error. You cannot tell which from looking at a pretty
fan chart, so you need the dumb baseline on screen next to it. A foundation
model that cannot beat this is adding nothing but latency and a large
dependency.

So the reference service is useful in its own right, and swapping in Kronos
is a matter of running a different process against the same contract. Nothing
in this repository changes.

The statistics, stated plainly
------------------------------
Prices are modelled as a geometric random walk in log space:

    log(S_t) = log(S_0) + mu*t + sigma*W_t

``mu`` (drift) is estimated from the sample mean of log returns, but the raw
sample mean is a terrible estimator -- its standard error is sigma/sqrt(n),
so on 400 bars a spurious 0.1% drift produces a "significant" trend out of
pure noise. So the drift is **shrunk** toward zero by its own signal-to-noise
ratio, ``mu * t2/(1+t2)``, which is a unit-information prior. On data with no
edge this collapses mu to approximately zero, which is the correct answer.

``sigma`` is the sample standard deviation of log returns, used both for the
predictive band (which widens as sqrt(horizon)) and to put the quantiles on
an unbiased-for-level footing: since log returns are roughly normal, the
median is ``S_0*exp(mu*h)`` and the mean carries the usual +0.5*sigma^2*h
correction that a lognormal median otherwise understates.

The confidence reported per horizon is a one-sided Gaussian probability that
the forecast band contains the eventual close, computed by integrating the
drift uncertainty into the band rather than pretending the drift is known.

Stdlib only, matching the rest of the repository.
"""

import argparse
import json
import math
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTRACT = "mafutani.forecast/1"
MODEL_NAME = "rw-null"
FAMILY = "baseline"
IS_REAL_MODEL = False

# 1.2816 = 80th percentile of the standard normal. p10/p90 bracket the
# middle 80%, which is the band worth showing on a chart: wide enough to
# be honest about uncertainty, narrow enough to be readable.
Z80 = 1.2815515655446004

MIN_BARS = 2
MAX_BARS = 5000
MAX_BODY = 8 * 1024 * 1024

DEFAULT_HORIZONS = (1, 4, 8)


def _mean(xs):
    return sum(xs) / len(xs)


def _stdev(xs, ddof=1):
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(max(var, 0.0))


def rw_forecast(bars, horizons=DEFAULT_HORIZONS):
    """Random-walk-with-drift forecast. `bars` is [[ts, close], ...] oldest
    first. Returns the contract's response payload."""
    t0 = time.time()
    closes = [float(b[1]) for b in bars]
    if len(closes) < MIN_BARS:
        raise ValueError("need at least {} bars, got {}".format(MIN_BARS, len(closes)))
    if any(c <= 0 for c in closes):
        raise ValueError("closes must be positive (log-space model)")

    logrets = []
    for a, b in zip(closes, closes[1:]):
        logrets.append(math.log(b / a))

    n = len(logrets)
    last = closes[-1]
    sigma = _stdev(logrets)

    # Drift, shrunk by its own signal-to-noise. A unit-information prior:
    # with no evidence the posterior mean of drift is zero, which is the
    # honest answer for a liquid instrument.
    #
    # The sigma floor is load-bearing, not defensive padding. A perfectly
    # smooth series measures sigma ~1e-16, which makes the standard error
    # vanish and the t-statistic infinite -- so the shrinkage factor goes to
    # 1 and the raw drift is extrapolated at full strength. That is exactly
    # backwards: with no dispersion there is no evidence of anything, and a
    # synthetic or stale series would otherwise produce the most confident
    # drift estimate the service can emit. Below the floor, drift is 0.
    mu_raw = _mean(logrets)
    if sigma < 1e-12:
        se, t2, mu = 0.0, 0.0, 0.0
    else:
        se = sigma / math.sqrt(n) if n > 0 else 0.0
        t2 = (mu_raw / se) ** 2 if se > 0 else 0.0
        mu = mu_raw * (t2 / (1.0 + t2)) if t2 > 0 else 0.0

    out = []
    for h in horizons:
        h = int(h)
        if h < 1:
            continue
        p50 = last * math.exp(mu * h)
        mean = p50 * math.exp(0.5 * sigma * sigma * h)
        band = last * sigma * math.sqrt(h)
        p10 = p50 * math.exp(-Z80 * sigma * math.sqrt(h))
        p90 = p50 * math.exp(+Z80 * sigma * math.sqrt(h))

        # Confidence that the eventual close lands inside [p10, p90],
        # once drift uncertainty is integrated in. Treating a point
        # estimate of drift as exact would report a confident number for a
        # band the model knows is wide.
        extra = abs(mu_raw) * math.sqrt(h) if se > 0 else 0.0
        total = math.sqrt((sigma * math.sqrt(h)) ** 2 + extra ** 2)
        conf = 2.0 * 0.5 * (1.0 + math.erf(Z80 * sigma * math.sqrt(h) /
                                          (total if total > 0 else 1.0))) - 1.0
        out.append({
            "horizon": h,
            "mean": mean,
            "median": p50,
            "sigma": band,
            "p10": p10,
            "p50": p50,
            "p90": p90,
            "confidence": max(0.0, min(1.0, conf)),
        })

    return {
        "contract": CONTRACT,
        "model": MODEL_NAME,
        "family": FAMILY,
        "is_real_model": IS_REAL_MODEL,
        "symbol": "",
        "freq": "",
        "asof": int(bars[-1][0]),
        "n_bars": n + 1,
        "last_close": last,
        "predicted": out,
        "diagnostics": {
            "sigma_per_bar": sigma,
            "drift_raw_per_bar": mu_raw,
            "drift_used_per_bar": mu,
            "drift_t2": t2,
            "shrinkage": (t2 / (1.0 + t2)) if t2 > 0 else 0.0,
            "note": "random walk with shrunk drift -- not a trained model",
        },
        "elapsed_ms": round((time.time() - t0) * 1000.0, 3),
    }


HEALTH = {
    "contract": CONTRACT,
    "ok": True,
    "model": MODEL_NAME,
    "family": FAMILY,
    "is_real_model": IS_REAL_MODEL,
    "lookback": MAX_BARS,
    "horizons": list(DEFAULT_HORIZONS),
    "warning": ("This service is a random walk, NOT Kronos. It exists as the "
                "benchmark a real model must beat. Do not read it as a "
                "prediction of anything."),
}
_STARTED = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "mafutani-forecast-ref/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[forecast-ref] %s\n" % (fmt % args))

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
            payload = dict(HEALTH)
            payload["uptime_s"] = round(time.time() - _STARTED, 1)
            return self._send(200, payload)
        return self._send(404, {"contract": CONTRACT,
                                "error": "no such endpoint: {}".format(self.path)})

    def do_POST(self):
        if self.path.split("?")[0] != "/predict":
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
                "error": "contract mismatch: this service speaks {}, "
                         "caller sent {!r}".format(CONTRACT, ver)})

        bars = req.get("bars")
        if not isinstance(bars, list):
            return self._send(400, {"contract": CONTRACT,
                                    "error": "'bars' must be a list of [ts, close]"})
        if len(bars) > MAX_BARS:
            return self._send(400, {"contract": CONTRACT,
                                    "error": "too many bars ({} > {}); "
                                             "truncate client-side".format(
                                                 len(bars), MAX_BARS)})
        clean = []
        for b in bars:
            if not isinstance(b, (list, tuple)) or len(b) < 2:
                return self._send(400, {"contract": CONTRACT,
                                        "error": "each bar must be [ts, close]"})
            try:
                clean.append([int(b[0]), float(b[1])])
            except (TypeError, ValueError):
                return self._send(400, {"contract": CONTRACT,
                                        "error": "non-numeric bar: {!r}".format(b)})

        try:
            payload = rw_forecast(clean, req.get("horizons") or DEFAULT_HORIZONS)
        except ValueError as e:
            return self._send(400, {"contract": CONTRACT, "error": str(e)})
        except Exception as e:
            return self._send(500, {"contract": CONTRACT,
                                    "error": "model failure: {}".format(e)})
        payload["symbol"] = str(req.get("symbol") or "")
        payload["freq"] = str(req.get("freq") or "")
        return self._send(200, payload)


def main():
    ap = argparse.ArgumentParser(description="Reference forecast service (random walk).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8011)
    args = ap.parse_args()

    # Bound to loopback unless told otherwise: this speaks plain HTTP with
    # no auth, and it is not something to expose off-box by accident.
    if args.host not in ("127.0.0.1", "::1", "localhost") and not os.environ.get(
            "MAFUTANI_FORECAST_ALLOW_PUBLIC"):
        sys.stderr.write("refusing to bind {} without MAFUTANI_FORECAST_ALLOW_PUBLIC=1\n"
                         .format(args.host))
        return 2

    banner = (
        "\n"
        "  ============================================================\n"
        "   THIS IS NOT KRONOS.  It is a random walk with drift.\n"
        "   Contract: {}   model: {}   is_real_model: {}\n"
        "   It exists as the benchmark a real model must beat.\n"
        "   http://{}:{}/health\n"
        "  ============================================================\n"
    ).format(CONTRACT, MODEL_NAME, IS_REAL_MODEL, args.host, args.port)
    sys.stderr.write(banner)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    stop = threading.Event()

    def _shutdown(_sig, _frm):
        stop.set()
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
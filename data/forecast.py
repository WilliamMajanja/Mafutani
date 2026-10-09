"""Forecast service client. Stdlib only.

Talks to a forecast model running as a separate process over the
``mafutani.forecast/1`` contract (see services/forecast/CONTRACT.md). The
model itself -- Kronos, TimesFM -- is PyTorch and does not belong in a
stdlib-only repository, so it lives outside and only its wire format is
shared.

The guarantee this module makes
-------------------------------
**Every failure raises. None of them return a number.**

A terminal that renders a plausible forecast when the model failed is worse
than one that renders nothing, because at a glance the fallback is
indistinguishable from a real one, and the person reading it does not know
which they are looking at. So:

* service down            -> ForecastUnavailable
* service slow            -> ForecastUnavailable (timeout is a failure, not a
                             wait-forever)
* malformed JSON          -> ForecastUnavailable
* wrong contract revision -> ForecastUnavailable
* missing ``is_real_model``-> ForecastUnavailable
* quantiles with no sigma -> ForecastUnavailable

That last pair is the one that matters. A reference implementation, a stub,
or a misconfigured endpoint that quietly returns a number without declaring
what it is would otherwise pass straight through and be drawn on a chart
labelled like a real forecast. Requiring the service to *state* whether it is
a trained model means "I am a random walk" has to be said out loud before a
number is drawn.

Configuration
-------------
``MAFUTANI_FORECAST_URL``        service root (default http://127.0.0.1:8011)
``MAFUTANI_FORECAST_TIMEOUT_S``  per-request timeout (default 20)
``MAFUTANI_FORECAST_TTL_S``      result cache TTL (default 60)

Forecasts are **display only**. Nothing in this module places or arms an
order, and ``forecast`` is absent from every tier's ``methods_allowed`` so no
signal or backtest path can reach it. See engines/forecast.py.
"""

import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request

CONTRACT = "mafutani.forecast/1"

DEFAULT_URL = "http://127.0.0.1:8011"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_TTL_S = 60.0

# The service is told not to accept more than this. Sending more is a
# client bug (or an unbounded query) and the service will reject it; better
# to fail here with a clear message than to burn the payload.
MAX_BARS = 5000
MIN_BARS = 2

_cache = {}
_cache_lock = threading.Lock()


class ForecastUnavailable(Exception):
    """No forecast could be obtained.

    Carries ``reason`` (a machine-readable short string) and ``detail``
    (human text). Never carries a partial forecast: a caller that catches
    this has no number to accidentally render.
    """

    def __init__(self, reason, detail):
        super().__init__("{}: {}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# -- configuration ------------------------------------------------------

def service_url():
    return (os.environ.get("MAFUTANI_FORECAST_URL") or DEFAULT_URL).rstrip("/")


def timeout_s():
    raw = os.environ.get("MAFUTANI_FORECAST_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return v if v > 0 else DEFAULT_TIMEOUT_S


def ttl_s():
    raw = os.environ.get("MAFUTANI_FORECAST_TTL_S")
    if not raw:
        return DEFAULT_TTL_S
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_TTL_S
    return max(0.0, v)


def clear_cache():
    """Drop every cached health check and forecast. Used by tests and by
    the settings UI when the operator repoints the service."""
    with _cache_lock:
        _cache.clear()


# -- validation ---------------------------------------------------------

def _num(v):
    """Coerce to a finite float, or None. Rejects bool, NaN and inf.

    ``json.loads`` accepts ``NaN`` and ``Infinity`` as bare literals, so a
    malformed producer can put them in the payload and they will survive
    into the UI as a number that renders as garbage. Checked here.
    """
    if isinstance(v, bool):
        return None
    if not isinstance(v, (int, float)):
        return None
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def validate(payload):
    """Check a service response against the contract. Return a normalised
    dict, or raise ForecastUnavailable. Never returns on a partial accept."""
    if not isinstance(payload, dict):
        raise ForecastUnavailable("malformed", "response was not a JSON object")

    ver = payload.get("contract")
    if ver != CONTRACT:
        raise ForecastUnavailable(
            "contract_mismatch",
            "service speaks {!r}, this client requires {!r}".format(ver, CONTRACT))

    # Must be *present*. Absent means the service cannot tell us what it is,
    # which is precisely the case that must not be drawn on a chart.
    if "is_real_model" not in payload:
        raise ForecastUnavailable(
            "unidentified_model",
            "service did not state is_real_model; refusing to display an "
            "unidentified model")
    real = payload["is_real_model"]
    if not isinstance(real, bool):
        raise ForecastUnavailable(
            "unidentified_model",
            "is_real_model must be a boolean, got {!r}".format(real))

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ForecastUnavailable("malformed", "missing model name")

    preds = payload.get("predicted")
    if not isinstance(preds, list) or not preds:
        raise ForecastUnavailable("malformed", "no predictions returned")

    last_close = _num(payload.get("last_close"))
    if last_close is None:
        raise ForecastUnavailable("malformed", "missing or non-finite last_close")

    clean = []
    for p in preds:
        if not isinstance(p, dict):
            raise ForecastUnavailable("malformed", "prediction entry is not an object")
        h = p.get("horizon")
        if isinstance(h, bool) or not isinstance(h, int) or h < 1:
            raise ForecastUnavailable("malformed",
                                      "horizon must be a positive int, got {!r}".format(h))
        row = {"horizon": h}
        for k in ("mean", "median", "sigma", "p10", "p50", "p90", "confidence"):
            if k in p:
                n = _num(p[k])
                if n is None:
                    raise ForecastUnavailable(
                        "malformed",
                        "field {!r} on horizon {} is not finite ({!r})".format(k, h, p[k]))
                row[k] = n
        if "mean" not in row:
            if "p50" not in row:
                raise ForecastUnavailable("malformed",
                                          "horizon {} has neither mean nor p50".format(h))
            row["mean"] = row["p50"]
        # Quantiles without a dispersion to check them against are
        # decoration: a band with no sigma cannot be verified as calibrated.
        if any(q in row for q in ("p10", "p90")) and "sigma" not in row:
            raise ForecastUnavailable(
                "uncalibrated",
                "horizon {} gives quantiles but no sigma".format(h))
        for lo, hi in (("p10", "p90"), ("p10", "p50"), ("p50", "p90")):
            if lo in row and hi in row and row[lo] > row[hi]:
                raise ForecastUnavailable(
                    "malformed",
                    "horizon {} has {} > {}".format(h, lo, hi))
        if "sigma" in row and row["sigma"] < 0:
            raise ForecastUnavailable("malformed",
                                      "horizon {} has negative sigma".format(h))
        clean.append(row)
    clean.sort(key=lambda r: r["horizon"])

    out = {
        "contract": CONTRACT,
        "model": model,
        "family": payload.get("family") or "unknown",
        "is_real_model": real,
        "symbol": payload.get("symbol") or "",
        "freq": payload.get("freq") or "",
        "asof": int(_num(payload.get("asof")) or 0),
        "n_bars": int(_num(payload.get("n_bars")) or 0),
        "last_close": last_close,
        "predicted": clean,
        "diagnostics": payload.get("diagnostics") or {},
        "elapsed_ms": _num(payload.get("elapsed_ms")) or 0.0,
    }
    if out["is_real_model"] is False:
        out["warning"] = (
            "'{}' is not a trained model (family={}). It is the baseline a "
            "real model must beat, not a prediction.".format(model, out["family"]))
    return out


# -- transport ----------------------------------------------------------

def _request(method, path, body=None, timeout=None):
    url = service_url() + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "mafutani-tcc/1.1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    tmo = timeout if timeout is not None else timeout_s()
    try:
        with urllib.request.urlopen(req, timeout=tmo) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # The service said no in words. Pass those words on rather than
        # replacing them with a generic message: "need at least 2 bars" is
        # actionable, "forecast failed" is not.
        try:
            err = json.loads(e.read().decode() or "{}")
            detail = err.get("error") or "HTTP {} from service".format(e.code)
        except Exception:
            detail = "HTTP {} from service".format(e.code)
        raise ForecastUnavailable("service_rejected", detail)
    except urllib.error.URLError as e:
        raise ForecastUnavailable(
            "unreachable",
            "{} (is the forecast service running? set MAFUTANI_FORECAST_URL)".format(e.reason))
    except (socket.timeout, TimeoutError):
        raise ForecastUnavailable(
            "timeout", "no response within {}s".format(tmo))
    except Exception as e:
        raise ForecastUnavailable("transport", "{}: {}".format(type(e).__name__, e))

    try:
        return json.loads(raw.decode())
    except Exception as e:
        raise ForecastUnavailable("malformed", "response was not JSON: {}".format(e))


# -- public API ---------------------------------------------------------

def health(force=False, timeout=None):
    """GET /health. Returns the service's self-description.

    This is unauthenticated on purpose: it carries no user data and no
    prices, and the UI needs it to tell the operator which model they are
    actually looking at before any forecast is drawn.

    Raises ForecastUnavailable when the service is not reachable.
    """
    key = ("health", service_url(), bool(force))
    ttl = ttl_s()
    if not force and ttl > 0:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]

    payload = _request("GET", "/health", timeout=timeout or min(4.0, timeout_s()))
    if not isinstance(payload, dict):
        raise ForecastUnavailable("malformed", "health was not a JSON object")
    if payload.get("contract") != CONTRACT:
        raise ForecastUnavailable(
            "contract_mismatch",
            "service speaks {!r}, this client requires {!r}".format(
                payload.get("contract"), CONTRACT))
    h = {
        "contract": CONTRACT,
        "ok": bool(payload.get("ok")),
        "model": payload.get("model") or "unknown",
        "family": payload.get("family") or "unknown",
        "is_real_model": payload.get("is_real_model"),
        "horizons": payload.get("horizons") or [],
        "warning": payload.get("warning") or "",
        "url": service_url(),
    }
    with _cache_lock:
        _cache[key] = (time.time(), h)
    return h


def is_available(timeout=None):
    """Boolean health probe. Never raises -- the one place that swallows
    failure, for callers that only want to know whether to offer a button."""
    try:
        return bool(health(force=True, timeout=timeout).get("ok"))
    except Exception:
        return False


def predict(bars, symbol="", horizons=None, timeout=None):
    """POST /predict. `bars` is [(ts, close), ...] oldest first.

    Returns a validated forecast dict. Raises ForecastUnavailable on every
    failure path; see the module docstring for why that is the contract.
    """
    if not isinstance(bars, (list, tuple)):
        raise ForecastUnavailable("bad_input", "bars must be a sequence")
    if len(bars) < MIN_BARS:
        raise ForecastUnavailable(
            "insufficient_history",
            "need at least {} bars, got {}".format(MIN_BARS, len(bars)))
    if len(bars) > MAX_BARS:
        raise ForecastUnavailable(
            "too_much_history",
            "{} bars exceeds the {} the service accepts; truncate client-side".format(
                len(bars), MAX_BARS))

    wire = []
    for b in bars:
        try:
            ts, cl = int(b[0]), float(b[1])
        except (TypeError, ValueError, IndexError):
            raise ForecastUnavailable("bad_input", "bar must be (ts, close): {!r}".format(b))
        if not math.isfinite(cl) or cl <= 0:
            raise ForecastUnavailable(
                "bad_input", "close must be positive and finite: {!r}".format(cl))
        wire.append([ts, cl])

    hs = [int(h) for h in (horizons or (1, 4, 8))]
    if not hs:
        raise ForecastUnavailable("bad_input", "no horizons requested")

    key = ("predict", service_url(), symbol, wire[-1][0], len(wire), tuple(sorted(hs)))
    ttl = ttl_s()
    if ttl > 0:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]

    body = {"contract": CONTRACT, "symbol": symbol or "", "bars": wire, "horizons": hs}
    payload = _request("POST", "/predict", body=body, timeout=timeout)
    out = validate(payload)
    out["symbol"] = symbol or out.get("symbol") or ""
    if not out.get("freq"):
        out["freq"] = _infer_freq(wire)
    with _cache_lock:
        _cache[key] = (time.time(), out)
    return out


def _infer_freq(wire):
    """Best-effort bar spacing label, for display only."""
    if len(wire) < 2:
        return ""
    gap = wire[-1][0] - wire[-2][0]
    for label, secs in (("1m", 60), ("5m", 300), ("15m", 900),
                        ("1h", 3600), ("4h", 14400), ("1d", 86400)):
        if abs(gap - secs) <= secs * 0.1:
            return label
    return "{}s".format(gap)
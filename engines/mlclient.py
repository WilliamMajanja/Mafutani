"""Stdlib client for the torch model service. No third-party imports.

The guarantee this module makes
-------------------------------
**Every failure raises. None of them return a number.**

Identical in shape to the contract the forecast service had, and for the
same reason. A terminal that silently substitutes a default coefficient when
the model server is down is worse than one that reports nothing, because the
fallback and the real fit are indistinguishable at the point of use -- and a
trader who cannot tell them apart will eventually act on the fallback.

So: service down, timeout, wrong contract, unfitted model, malformed JSON --
all raise `MLUnavailable`. Callers degrade by catching it, not by receiving a
plausible number.

Configuration
-------------
``MAFUTANI_ML_URL``        service root (default http://127.0.0.1:8021)
``MAFUTANI_ML_TIMEOUT_S``  per-request timeout (default 30)
``MAFUTANI_ML_TTL_S``      fit-result cache TTL (default 300)
"""
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request

CONTRACT = "mafutani.ml/1"
DEFAULT_URL = "http://127.0.0.1:8021"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_TTL_S = 300.0

_cache = {}
_lock = threading.Lock()


class MLUnavailable(Exception):
    """No model could be obtained. Carries `reason` and `detail`.

    Never carries a partial model: a caller that catches this has no
    coefficient to accidentally trade on.
    """

    def __init__(self, reason, detail):
        super().__init__("{}: {}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def service_url():
    return (os.environ.get("MAFUTANI_ML_URL") or DEFAULT_URL).rstrip("/")


def timeout_s():
    raw = os.environ.get("MAFUTANI_ML_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return v if v > 0 else DEFAULT_TIMEOUT_S


def ttl_s():
    raw = os.environ.get("MAFUTANI_ML_TTL_S")
    if not raw:
        return DEFAULT_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TTL_S


def clear_cache():
    with _lock:
        _cache.clear()


def _num(v):
    """Coerce to a finite float or None. Rejects bool, NaN, inf.

    json.loads accepts NaN and Infinity as bare literals, so a malformed
    producer can smuggle them into a coefficient and they will render as
    garbage rather than raise.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def validate_model(payload):
    """Check a /fit response. Returns a normalised dict or raises.

    The two rejections that matter: a model that did not fit (`ok` false) is
    refused, and a model whose coefficient is missing or non-finite is
    refused. Neither is a warning -- both are ways a number that looks like a
    calibration reaches something that acts on it.
    """
    if not isinstance(payload, dict):
        raise MLUnavailable("malformed", "response was not a JSON object")
    if payload.get("contract") != CONTRACT:
        raise MLUnavailable("contract_mismatch",
                            "service speaks {!r}, client requires {!r}"
                            .format(payload.get("contract"), CONTRACT))
    if not payload.get("ok"):
        raise MLUnavailable("not_fitted",
                            "model did not fit: {}".format(
                                payload.get("reason") or "no reason given"))
    out = {"contract": CONTRACT, "ok": True, "n": payload.get("n"),
           "alpha": None, "beta": None, "gamma": None, "r2": None,
           "ridge_lambda": payload.get("ridge_lambda"),
           "se_beta": _num(payload.get("standard_error_beta")),
           "t_beta": _num(payload.get("t_beta")),
           "device": payload.get("device")}
    for k in ("alpha", "beta", "gamma", "r2"):
        v = _num(payload.get(k))
        if v is None:
            raise MLUnavailable("malformed",
                                "field {!r} missing or non-finite: {!r}"
                                .format(k, payload.get(k)))
        out[k] = v
    n = payload.get("n")
    if not isinstance(n, int) or n < 1:
        raise MLUnavailable("malformed", "n must be a positive int, got {!r}"
                            .format(n,))
    # A coefficient with no dispersion to compare it against cannot be judged
    # worth acting on. Requiring an SE keeps "trust it" a decision rather
    # than a default.
    if out["se_beta"] is None:
        out["has_se"] = False
    else:
        out["has_se"] = True
        if out["t_beta"] is not None and abs(out["t_beta"]) < 2.0:
            out["verdict"] = "not-distinguishable-from-noise"
        else:
            out["verdict"] = "fitted"
    return out


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
        try:
            err = json.loads(e.read().decode() or "{}")
            detail = err.get("error") or "HTTP {} from service".format(e.code)
        except Exception:
            detail = "HTTP {} from service".format(e.code)
        raise MLUnavailable("service_rejected", detail)
    except urllib.error.URLError as e:
        raise MLUnavailable(
            "unreachable",
            "{} (is the model service running? MAFUTANI_ML_URL)".format(e.reason))
    except (TimeoutError, OSError) as e:
        raise MLUnavailable("transport", "{}: {}".format(type(e).__name__, e))
    try:
        return json.loads(raw.decode())
    except Exception as e:
        raise MLUnavailable("malformed", "response was not JSON: {}".format(e))


def health(force=False, timeout=None):
    """GET /health. Raises if the service is not up."""
    payload = _request("GET", "/health", timeout=timeout or min(3.0, timeout_s()))
    if not isinstance(payload, dict):
        raise MLUnavailable("malformed", "health was not a JSON object")
    return {"ok": bool(payload.get("ok")), "contract": CONTRACT,
            "torch": payload.get("torch"), "cuda": bool(payload.get("cuda")),
            "device": payload.get("device"), "url": service_url()}


def is_available(timeout=None):
    """Boolean probe. The one place that swallows failure."""
    try:
        return bool(health(timeout=timeout).get("ok"))
    except Exception:
        return False


def fit(samples, ridge_lambda=None, timeout=None):
    """POST /fit. `samples` is [(ofi, depth, dmid), ...].

    Returns a validated model dict, or raises. Cached on the sample shape so
    a repeated scan does not refit the same data every few seconds.
    """
    rows = []
    for s in (samples or []):
        try:
            a, b, c = float(s[0]), float(s[1]), float(s[2])
        except (TypeError, ValueError, IndexError):
            raise MLUnavailable("bad_input", "sample must be (ofi, depth, dmid): "
                                             "{!r}".format(s))
        if b <= 0:
            continue
        if any(math.isnan(x) or math.isinf(x) for x in (a, b, c)):
            continue
        rows.append((a, b, c))
    if not rows:
        raise MLUnavailable("no_samples",
                            "nothing to fit: {} samples supplied, none usable"
                            .format(len(samples or [])))

    key = ("fit", service_url(), len(rows), ridge_lambda, rows[0], rows[-1])
    ttl = ttl_s()
    if ttl > 0:
        with _lock:
            hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]

    body = {"contract": CONTRACT, "samples": [list(r) for r in rows]}
    if ridge_lambda:
        body["ridge_lambda"] = float(ridge_lambda)
    out = validate_model(_request("POST", "/fit", body=body, timeout=timeout))
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def predict(model, ofi_norm, depth, timeout=None):
    """POST /predict. Returns the predicted mid change, or raises."""
    d = _num(depth)
    o = _num(ofi_norm)
    if d is None or d <= 0:
        raise MLUnavailable("bad_input", "depth must be positive and finite")
    if o is None:
        raise MLUnavailable("bad_input", "ofi_norm must be finite")
    body = {"contract": CONTRACT, "model": model, "ofi_norm": o, "depth": d}
    payload = _request("POST", "/predict", body=body, timeout=timeout)
    val = _num((payload or {}).get("predicted_dmid"))
    if val is None:
        raise MLUnavailable("malformed",
                            "no finite predicted_dmid in response: {!r}"
                            .format((payload or {}).get("predicted_dmid")))
    return val
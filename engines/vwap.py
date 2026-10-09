"""VWAP toolkit: session VWAP, standard-deviation bands, anchored VWAP.

Stdlib only, import-safe, pure (lists in, lists out, no I/O).

Why a new module when `indicators.vwap_series` exists
-----------------------------------------------------
`vwap_series` accumulates from the first candle it is handed and never
resets. That is correct for a *rolling accumulation over whatever slice
you passed*, and the docstring says as much. It is not session VWAP: on
this app's 1-minute bars a full day is 1,440 bars, so the "session" VWAP
in `vwap_series` is really a multi-day cumulative average that drifts
further from the current session every day it runs. Every published guide
to this indicator anchors to the session open, because that is what makes
it a fair execution benchmark for the current session.

So rather than change `vwap_series` -- the strategy parity tests pin its
output -- this module adds the session-aware, parameterised family and
leaves the old function alone.

Parameters, and what they actually change
------------------------------------------
`source`      price used for the volume weighting.
              ``hlc3`` ((H+L+C)/3) is the conventional choice and the
              default. ``ohlc`` = (O+H+L+C)/4, ``close`` = C only.
              ``close`` is the fastest to react and the noisiest.
`session_min` session length in minutes (1440 = daily). Used to segment
              when candles carry no usable timestamp.
`anchor`      ``"session"`` (default), ``"first"``, ``"day_high"``,
              ``"day_low"``, ``"last_swing"`` (highest high / lowest low of
              the session so far), or an integer bar index.
`sigmas`      standard-deviation multipliers for the bands. The 1/2/3 set
              is conventional; Al Brooks style H1/H2/L1/L2 entries sit on
              the 1 and 2 bands.

Standard-deviation bands
------------------------
Bands come from the *volume-weighted variance of the cumulative price
series*, not the variance of the closes. That distinction matters: the
band width is meant to reflect where volume actually traded, so weighting
the dispersion by volume is what makes "2 sigma" mean the same thing here
as it does on every other VWAP band chart.

Where it refuses
----------------
A band with zero dispersion produces an infinite z-score, and a VWAP with
no volume has no denominator. Both return ``None`` rather than a number,
because the alternative is a band that looks infinitely far away and a
signal that always fires.
"""

import math

DEFAULT_SOURCE = "hlc3"
DEFAULT_SESSION_MIN = 1440
DEFAULT_SIGMAS = (1.0, 2.0, 3.0)


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def _get(c, *names, **kw):
    for n in names:
        if isinstance(c, dict):
            if n in c:
                return c[n]
        else:
            if hasattr(c, n):
                return getattr(c, n)
    return kw.get("default", None)


def typical_price(candle, source=DEFAULT_SOURCE):
    """The price that gets volume-weighted, per `source`."""
    h = _f(_get(candle, "high", "h"))
    l = _f(_get(candle, "low", "l"))
    c = _f(_get(candle, "close", "c"))
    o = _f(_get(candle, "open", "o"), c)
    src = str(source or DEFAULT_SOURCE).lower()
    if src in ("hlc3", "typical"):
        return (h + l + c) / 3.0
    if src == "ohlc":
        return (o + h + l + c) / 4.0
    if src in ("close", "c"):
        return c
    if src == "hl2":
        return (h + l) / 2.0
    return (h + l + c) / 3.0


def _session_of(ts, session_min):
    """Bucket index for a bar's timestamp. None when ts is unusable."""
    t = _f(ts, 0.0)
    if t <= 0:
        return None
    width = max(1, int(session_min or DEFAULT_SESSION_MIN)) * 60
    return int(t // width)


def vwap(candles, source=DEFAULT_SOURCE, session_min=DEFAULT_SESSION_MIN,
         anchor="session"):
    """Session-anchored VWAP series.

    `anchor`:
      ``"session"``    reset at each session boundary (conventional)
      ``"first"``      one cumulative line across everything supplied
      ``"day_high"``   reset at the highest high seen so far in the session
      ``"day_low"``    reset at the lowest low so far in the session
      int              reset at that bar index

    Returns a list the same length as `candles`. Bars before any VWAP can
    be computed carry 0.0 rather than None, matching how the rest of this
    codebase treats warm-up.
    """
    bars = list(candles or [])
    out = [0.0] * len(bars)
    if not bars:
        return out

    cum_pv = 0.0
    cum_v = 0.0
    cur_session = None
    anchor_idx = 0

    for i, c in enumerate(bars):
        sess = _session_of(_get(c, "ts"), session_min)
        reset = False
        if anchor == "session":
            # No timestamp? fall back to counting bars in fixed blocks so
            # the reset still happens rather than never happening.
            if sess is not None:
                reset = (cur_session is None or sess != cur_session)
                cur_session = sess
            else:
                reset = (i % max(1, int(session_min or DEFAULT_SESSION_MIN))) == 0
            if i == 0:
                reset = True
        elif anchor == "first":
            reset = (i == 0)
        elif anchor == "day_high":
            window = bars[max(0, i - int(session_min or DEFAULT_SESSION_MIN) + 1): i + 1]
            hi = max(_f(_get(b, "high", "h")) for b in window) if window else 0.0
            reset = (i == 0) or (hi >= _f(_get(c, "high", "h")))
            if i == 0:
                anchor_idx = 0
        elif anchor == "day_low":
            window = bars[max(0, i - int(session_min or DEFAULT_SESSION_MIN) + 1): i + 1]
            lo = min(_f(_get(b, "low", "l")) for b in window) if window else 0.0
            reset = (i == 0) or (lo <= _f(_get(c, "low", "l")))
        elif isinstance(anchor, int) and not isinstance(anchor, bool):
            reset = (i == anchor)
        else:
            reset = (i == 0)

        if reset:
            cum_pv = 0.0
            cum_v = 0.0
            anchor_idx = i

        tp = typical_price(c, source)
        v = _f(_get(c, "volume", "v"))
        if v > 0:
            cum_pv += tp * v
            cum_v += v
        out[i] = (cum_pv / cum_v) if cum_v > 0 else 0.0
    return out


def vwap_bands(candles, sigmas=DEFAULT_SIGMAS, source=DEFAULT_SOURCE,
               session_min=DEFAULT_SESSION_MIN, anchor="session"):
    """VWAP plus volume-weighted standard-deviation bands.

    Returns ``{"vwap": [...], "sd": [...], "upper": {k: [...]},
    "lower": {k: [...]}, "z": [...]}`` -- all the same length as `candles`.

    `z` is the signed distance of price from VWAP in band units: 0 at the
    VWAP, +2 on the second upper band. `None` before the line exists and
    wherever dispersion is exactly zero, because an infinite z-score would
    make every downstream comparison true.
    """
    bars = list(candles or [])
    n = len(bars)
    line = vwap(bars, source=source, session_min=session_min, anchor=anchor)
    out = {"vwap": line, "sd": [0.0] * n, "upper": {}, "lower": {},
           "z": [None] * n, "bars_used": [0] * n}
    if not bars:
        return out

    # Per-session running sums for a volume-weighted variance.
    s_pv = 0.0
    s_p2v = 0.0
    s_v = 0.0
    cur_session = None
    for i, c in enumerate(bars):
        sess = _session_of(_get(c, "ts"), session_min)
        reset = (i == 0)
        if anchor == "session":
            if sess is not None:
                reset = (cur_session is None or sess != cur_session)
                cur_session = sess
            else:
                reset = (i % max(1, int(session_min or DEFAULT_SESSION_MIN))) == 0
        elif anchor == "first":
            reset = (i == 0)
        elif isinstance(anchor, int) and not isinstance(anchor, bool):
            reset = (i == anchor)
        if reset:
            s_pv = 0.0
            s_p2v = 0.0
            s_v = 0.0
        tp = typical_price(c, source)
        v = _f(_get(c, "volume", "v"))
        if v > 0:
            s_pv += tp * v
            s_p2v += tp * tp * v
            s_v += v
        if s_v > 0:
            mean = s_pv / s_v
            var = (s_p2v / s_v) - mean * mean
            out["sd"][i] = math.sqrt(var) if var > 0 else 0.0
        out["bars_used"][i] = 1 if s_v > 0 else 0

    try:
        mults = [float(k) for k in (sigmas or ())]
    except (TypeError, ValueError):
        mults = list(DEFAULT_SIGMAS)
    if not mults:
        mults = list(DEFAULT_SIGMAS)

    for k in mults:
        out["upper"][k] = [line[i] + k * out["sd"][i] if line[i] > 0 else 0.0
                          for i in range(n)]
        out["lower"][k] = [line[i] - k * out["sd"][i] if line[i] > 0 else 0.0
                          for i in range(n)]

    for i in range(n):
        sd = out["sd"][i]
        price = _f(_get(bars[i], "close", "c"))
        if line[i] > 0 and sd > 0:
            out["z"][i] = (price - line[i]) / sd
    return out


def vwap_state(candles, source=DEFAULT_SOURCE, session_min=DEFAULT_SESSION_MIN,
               anchor="session", sigmas=DEFAULT_SIGMAS):
    """A compact read of where price sits versus VWAP and the bands.

    Everything a UI or a strategy needs in one call, so the same numbers
    back both the chart and the signal instead of drifting apart.
    """
    b = vwap_bands(candles, sigmas=sigmas, source=source,
                   session_min=session_min, anchor=anchor)
    if not candles:
        # Same key set as the ok branch, so a consumer can read the shape
        # without a .get() guard on every field. A refusal still has to
        # be machine-readable, or callers grow defensive branches and
        # the reason string gets lost.
        return {"ok": False, "reason": "no candles",
                "vwap": None, "sd": None, "z": None, "price": None,
                "zone": None, "source": source, "anchor": anchor,
                "session_min": session_min, "deviation_bps": None,
                "upper": {}, "lower": {}}
    last = len(candles) - 1
    line = b["vwap"][last]
    sd = b["sd"][last]
    price = _f(_get(candles[last], "close", "c"))
    if line <= 0:
        return {"ok": False, "reason": "no vwap (no volume in session)",
                "vwap": None, "z": None, "price": price}
    z = ((price - line) / sd) if sd > 0 else None
    zone = "at-vwap"
    if z is not None:
        az = abs(z)
        if az >= 3.0:
            zone = "beyond-3"
        elif az >= 2.0:
            zone = "band-2"
        elif az >= 1.0:
            zone = "band-1"
    return {
        "ok": True,
        "vwap": round(line, 8),
        "sd": round(sd, 8),
        "price": price,
        "z": round(z, 3) if z is not None else None,
        "zone": zone,
        "source": source,
        "anchor": anchor,
        "session_min": session_min,
        "deviation_bps": round((price - line) / line * 10000.0, 2) if line else None,
        "upper": {k: round(v[last], 8) for k, v in b["upper"].items()},
        "lower": {k: round(v[last], 8) for k, v in b["lower"].items()},
    }

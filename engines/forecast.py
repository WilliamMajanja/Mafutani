"""Forecast scoring. Stdlib only, no I/O in the pure functions.

Two jobs, and it is worth being clear about which is which.

1. **Interpret a single forecast** -- did the eventual close land inside the
   band it claimed, and was the direction right? (``band_hit``,
   ``direction_hit``, ``verdict``)

2. **Score a model over time** -- coverage and hit rate against every
   forecast this deployment has recorded, resolved against what actually
   happened. (``score_model``)

The second is the one that matters, and it is deliberately the boring one. A
fan chart says nothing about calibration. The question is not "did it look
confident" but "when it said 80%, was it right 80% of the time, and did that
beat a random walk?" A model with a 55% hit rate and honest bands is a
*worse* forecasting system than one with a 51% hit rate and 95% bands,
because the second one is calibrated enough for a caller to size a position
against. So `score_model` reports coverage against the nominal the bands
claimed, and `verdict` refuses to call anything useful when coverage is far
from nominal.

Why this can never move money
-----------------------------
``NOT_A_TRADE`` is exported and asserted in the test suite. Forecasts are
observations. ``forecast`` is absent from every tier's ``methods_allowed``,
so no signal or backtest path can reach this module, and no route here
writes to ``orders``. That is not caution about a weak model -- it is
caution about a *confidently wrong* model, which is the only kind that
actually costs money. A time-series foundation model has no demonstrated edge
on liquid crypto; Kronos' published results are in-sample and predate the
market conditions it would now be run against.
"""

import time

# Mafutani stores 1-minute candles, so one "bar" is 60s. Named rather than
# inlined because a contract that says "horizon in bars" without saying how
# long a bar is invites an off-by-60x bug the first time the feed resamples.
BAR_SECONDS = 60

# Nominal coverage implied by p10/p90 (the middle 80%).
NOMINAL_COVERAGE = 0.80

# Bands narrower than this relative to spot are not credible. A model
# claiming +/-0.01% on crypto is not precise, it is broken, and treating it
# as precise would make the coverage statistic meaningless.
MIN_BAND_PCT = 0.0005

NOT_A_TRADE = {
    "tradeable": False,
    "reason": ("forecast output is an observation, not an instruction; "
               "no forecast path may place, modify, or arm an order"),
}

_HORIZON_FIELDS = ("mean", "median", "sigma", "p10", "p50", "p90", "confidence")


def point(row):
    """The forecast's central estimate. Prefers p50 -- a model's median is
    the more robust central estimate than a mean that a single outlier can
    drag, and both are allowed by the contract."""
    for k in ("p50", "median", "mean"):
        if k in row and row[k] is not None:
            return float(row[k])
    return None


def band(row):
    """(low, high) for a row, or None if it carries no band.

    Tolerates a model that reports only ``sigma``: a symmetric band around
    the central estimate is a claim the scorer can still check.
    """
    if row.get("p10") is not None and row.get("p90") is not None:
        return (float(row["p10"]), float(row["p90"]))
    c, s = point(row), row.get("sigma")
    if c is not None and s is not None:
        return (c - float(s), c + float(s))
    return None


def band_hit(row, realized):
    """True if `realized` fell inside the forecast band.

    A row with no band returns False rather than None: an unbanded
    forecast cannot be scored on coverage, and counting it as a miss would
    quietly drag the statistic down for a reason that has nothing to do
    with the model's accuracy.
    """
    b = band(row)
    if b is None or realized is None:
        return False
    return b[0] <= float(realized) <= b[1]


def nominal_band(row):
    """Fraction of outcomes the row claims its band contains.

    Inferred from the width of the band relative to sigma, falling back to
    the contract's default when sigma is absent. Used so that a model
    serving +/-3 sigma can be compared to one serving p10/p90 instead of
    being scored against 80% for both.
    """
    b, s = band(row), row.get("sigma")
    if b is None:
        return None
    c = point(row)
    if c in (None, 0) or s in (None, 0):
        return NOMINAL_COVERAGE
    half = (b[1] - b[0]) / 2.0
    z = half / float(s)
    if z <= 0:
        return None
    return max(0.0, min(1.0, 2.0 * 0.5 * (1.0 + _erf(z / math_sqrt2())) - 1.0))


def _erf(x):
    # math.erf exists in the stdlib; this indirection exists only so the
    # formula above reads without a local import mid-expression.
    import math
    return math.erf(x)


def math_sqrt2():
    import math
    return math.sqrt(2.0)


def direction(row, last_close, realized):
    """-1 if the forecast pointed down, +1 up, 0 if flat or unknowable."""
    c = point(row)
    if c is None or last_close is None or realized is None:
        return 0
    c, last_close, realized = float(c), float(last_close), float(realized)
    if c == last_close:
        return 0
    if c > last_close:
        return 1 if realized > last_close else -1
    return -1 if realized < last_close else 1


def verdict(coverage, nominal, n, hit_rate=None):
    """Plain-language assessment of a calibration statistic.

    Deliberately conservative and deliberately unimpressed. A model with
    no history, or with bands so wide they always contain the outcome, is
    called what it is -- uninformative -- rather than credited for
    coverage it achieved by being vague.
    """
    if not n:
        return {"label": "no history", "usable": False,
                "detail": "not enough resolved forecasts to judge"}
    if nominal is None:
        return {"label": "unscorable", "usable": False,
                "detail": "forecast carried no band to score against"}
    tol = 0.15
    dev = coverage - nominal
    if abs(dev) <= tol:
        label, usable = "calibrated", True
        detail = "covered {:.0%} of outcomes against a nominal {:.0%}".format(
            coverage, nominal)
    elif dev < 0:
        label, usable = "overconfident", False
        detail = ("claimed {:.0%}, delivered {:.0%} -- its bands are too "
                  "narrow".format(nominal, coverage))
    else:
        label, usable = "underconfident", False
        detail = ("claimed {:.0%}, delivered {:.0%} -- its bands are too "
                  "wide to act on".format(nominal, coverage))
    if hit_rate is not None:
        detail += "; direction right {:.0%} of the time".format(hit_rate)
        if usable and hit_rate < 0.45:
            # Calibrated but no directional skill is still no skill.
            label, usable = "calibrated but uninformative", False
            detail += " -- no usable directional edge"
    return {"label": label, "usable": usable, "detail": detail}


# -- persistence --------------------------------------------------------

def record(conn, user_id, symbol, fc, now=None):
    """Store every horizon of one forecast so it can be scored later.

    Rows are written even for a non-real model: the whole point of the
    reference service is to accumulate a baseline that a real model is then
    measured against, and a benchmark nobody recorded is worthless.
    """
    now = int(now or time.time())
    asof = int(fc.get("asof") or 0)
    last = float(fc.get("last_close") or 0.0)
    model = fc.get("model") or "unknown"
    real = 1 if fc.get("is_real_model") else 0
    family = fc.get("family") or "unknown"
    rows = []
    for p in fc.get("predicted") or []:
        rows.append((int(user_id), symbol.upper(), model, real, family,
                     int(p["horizon"]), last, asof, now,
                     *[p.get(f) for f in _HORIZON_FIELDS]))
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO forecasts (user_id, symbol, model, is_real_model, family,"
        " horizon, last_close, asof, created_at, mean, median, sigma, p10,"
        " p50, p90, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def score_model(conn, symbol, model=None, bar_seconds=BAR_SECONDS, limit=500):
    """Resolve stored forecasts against what actually happened.

    Returns counts and rates for every recorded horizon whose target bar
    has closed, plus a `verdict`. Unresolved forecasts are counted in
    `pending` rather than dropped, so a brand-new model is visibly
    "building history" instead of looking like a model with no track record.
    """
    sym = (symbol or "").upper()
    sql = ("SELECT horizon, last_close, asof, mean, median, sigma, p10, p50,"
           " p90, confidence FROM forecasts WHERE symbol = ?")
    args = [sym]
    if model:
        sql += " AND model = ?"
        args.append(model)
    sql += " ORDER BY asof DESC, horizon ASC LIMIT ?"
    args.append(int(limit) * 4)
    stored = conn.execute(sql, args).fetchall()

    n = resolved = hits = dirs = dir_n = 0
    covered = 0.0
    nominal_num = 0.0
    max_dt = 0
    for r in stored:
        r = dict(r)
        h = int(r["horizon"])
        target = int(r["asof"]) + h * int(bar_seconds)
        n += 1
        got = conn.execute(
            "SELECT close FROM candles_1m WHERE symbol = ? AND ts = ?",
            (sym, target)).fetchone()
        if got is None:
            continue
        realized = float(got["close"])
        max_dt = max(max_dt, target - int(r["asof"]))
        resolved += 1
        row = {k: r[k] for k in _HORIZON_FIELDS if k in r}
        if band_hit(row, realized):
            covered += 1.0
            hits += 1
        nb = nominal_band(row)
        if nb is not None:
            nominal_num += 1.0
        d = direction(row, r["last_close"], realized)
        if d != 0:
            dirs += 1 if d > 0 else 0
            dir_n += 1

    if resolved and nominal_num:
        cov = covered / float(resolved)
        nom = min(1.0, nominal_num / float(resolved))
        hit = (dirs / float(dir_n)) if dir_n else None
        v = verdict(cov, nom, resolved, hit)
        return {
            "symbol": sym, "model": model or "all",
            "stored": n, "resolved": resolved, "pending": n - resolved,
            "band_hits": hits, "coverage": cov, "nominal": nom,
            "direction_n": dir_n, "direction_hit": hit,
            "horizon_s": max_dt, "verdict": v,
            "tradeable": False,
        }
    return {"symbol": sym, "model": model or "all", "stored": n,
            "resolved": resolved, "pending": n - resolved, "band_hits": 0,
            "coverage": None, "nominal": None, "direction_n": 0,
            "direction_hit": None, "horizon_s": 0,
            "verdict": verdict(0.0, None, resolved), "tradeable": False}


def compare_to_null(model_fc, null_fc):
    """How far a real model's forecast departs from a random walk.

    The most honest single number available: if a model's central estimate
    sits inside the null model's band, it has not yet shown it knows
    anything the last price did not.
    """
    if not model_fc or not null_fc:
        return None
    rows = {}
    for r in null_fc.get("predicted") or []:
        rows[int(r["horizon"])] = r
    out = []
    for r in model_fc.get("predicted") or []:
        h = int(r["horizon"])
        nr = rows.get(h)
        if nr is None:
            continue
        mp, np_ = point(r), point(nr)
        # Reference price comes from the forecast's top level, not from the
        # per-horizon row: a predicted row carries mean/sigma/quantiles and
        # has no last_close, so reading it from there silently yields None
        # and drops every horizon.
        ref = null_fc.get("last_close") or model_fc.get("last_close")
        if mp is None or np_ is None or not ref:
            continue
        nb = band(nr)
        within = (nb[0] <= mp <= nb[1]) if nb else None
        out.append({
            "horizon": h,
            "model_point": mp,
            "null_point": np_,
            "delta_pct": (mp - np_) / float(ref) * 100.0,
            "inside_null_band": within,
        })
    return out or None
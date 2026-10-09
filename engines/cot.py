"""COT analytics: net position, Williams index, percentile, z-score.

Stdlib only, pure (lists in, dict out, no I/O, no DB).

What a COT number is worth
--------------------------
It is a weekly count of contracts held, classified by the trader's
self-reported business purpose, published three days after the fact. It
says who is positioned. It does not say why, it does not say what they
will do next, and it is a *slow* variable -- 52 observations a year, each
one describing a market as it stood nine days ago when you get to read it.

So every function here is about *normalising* that variable rather than
about predicting from it. A raw net position of +180,000 contracts means
nothing on its own: 180k is trivial in crude and enormous in lean hogs,
and it is unremarkable for gold in 2021 and extreme in 1985. The value is
in where the number sits against its own history for that same contract,
which is what an index or a percentile gives and what a raw count cannot.

The four measures, and why more than one
----------------------------------------
``net``         the arithmetic. Signed long minus short.
``cot_index``   Williams' COT Index: where the current net sits between
                the minimum and maximum net of the trailing window, as
                0-100. Bounded, so it is comparable across contracts.
``percentile``  the fraction of observations at or below the current net.
                Unlike the index it does not assume the extremes are the
                meaningful bounds, and it degrades more gracefully on a
                short window.
``zscore``      how many standard deviations from the trailing mean.
                Sensitive to the tail, which is where COT lives -- a
                print at 2.5 sigma is a different event from 2.5 sigma of
                a bell curve.

All four say the same thing at different sensitivities, which is the point
of returning all four: a signal that fires on one and not the others is
information about the signal.

Where it refuses
----------------
* A single observation has no percentile, no z-score and no index. All
  return ``None``, never 0.0, because 0.0 reads as "the bottom of the
  range" and would be a confident statement about a market where exactly
  one thing is known.
* A flat window has zero dispersion. The z-score divides by it, so the
  z-score is ``None`` and the index is 50 (dead centre), which is what a
  flat series genuinely is.
* The window is reported alongside every result. A percentile of 12 from
  two years of data and one from five are different claims, and the
  caller has to be able to tell them apart.
"""

import math

# Williams' index is conventionally three years. Trailing 52-week windows
# are also common in published work; both are offered because the choice
# changes the answer materially at turning points, which is exactly where
# a positioning read gets acted on.
DEFAULT_WEEKS = 156          # three years
WINDOWS = {"3y": 156, "5y": 260, "2y": 104, "1y": 52}


def _f(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def net_position(long_pos, short_pos):
    """Signed net. None when either side is unknown."""
    lng, sht = _f(long_pos), _f(short_pos)
    if lng is None or sht is None:
        return None
    return lng - sht


def net_series(rows, category, key="category"):
    """Chronological [(date, net)] for one category.

    `rows` are dicts with report_date / long_pos / short_pos. Dates are
    sorted ascending, because a percentile over an unsorted window is a
    percentile of the wrong sample.
    """
    picked = [r for r in (rows or []) if r.get(key) == category]
    out = []
    for r in picked:
        n = net_position(r.get("long_pos"), r.get("short_pos"))
        if n is None:
            continue
        d = str(r.get("report_date") or "")
        if len(d) != 10:
            continue
        out.append((d, n))
    out.sort(key=lambda t: t[0])
    return out


def window(series, weeks=DEFAULT_WEEKS):
    """Trailing window including the latest observation."""
    if weeks is None or weeks <= 0:
        return list(series)
    return list(series[-int(weeks):])


def cot_index(series, weeks=DEFAULT_WEEKS):
    """Williams COT Index over the trailing window, 0-100.

    None with fewer than two observations: the index needs a minimum and a
    maximum, and with one observation those are the same number.
    """
    w = window(series, weeks)
    if len(w) < 2:
        return None
    vals = [n for _d, n in w]
    cur = vals[-1]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        # A contract that has held one net position for the whole window is
        # genuinely at the centre of its range; 50 says that, None would
        # hide it.
        return 50.0
    return round((cur - lo) / float(hi - lo) * 100.0, 2)


def percentile(series, weeks=DEFAULT_WEEKS):
    """Fraction of the window at or below the latest net, as 0-100.

    None with fewer than two observations. A single print has no rank.
    """
    w = window(series, weeks)
    if len(w) < 2:
        return None
    vals = [n for _d, n in w]
    cur = vals[-1]
    below = sum(1 for v in vals if v <= cur)
    return round(below / float(len(vals)) * 100.0, 2)


def zscore(series, weeks=DEFAULT_WEEKS):
    """Standard deviations from the window mean. None on zero dispersion."""
    w = window(series, weeks)
    if len(w) < 2:
        return None
    vals = [n for _d, n in w]
    n = len(vals)
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    if var <= 0:
        return None
    sd = math.sqrt(var)
    return round((vals[-1] - mean) / sd, 3)


def weekly_change(series):
    """Change in net since the previous report. None with no prior week."""
    if len(series) < 2:
        return None
    return series[-1][1] - series[-2][1]


def pct_of_open_interest(long_pos, short_pos, open_interest):
    """Net as a share of open interest.

    The size-independent version of `net`. Open interest is the honest
    denominator: a net that grows because the market grew is not a more
    crowded position, and this says so.
    """
    n = net_position(long_pos, short_pos)
    oi = _f(open_interest)
    if n is None or oi is None or oi <= 0:
        return None
    return round(n / oi * 100.0, 3)


def cot_ratio(long_pos, short_pos):
    """Classic commercial/non-commercial long-short ratio.

    From the legacy family. Ratio near 1.0 means the two sides are
    balanced; the extremes are what the report is read for. None when
    either side is zero -- a ratio against zero is an infinity, and an
    infinite ratio is not a number anyone should trade on.
    """
    lng, sht = _f(long_pos), _f(short_pos)
    if lng is None or sht is None:
        return None
    if lng <= 0 or sht <= 0:
        return None
    return round(lng / sht, 4)


def crowding(long_pos, short_pos, open_interest, series=None,
             weeks=DEFAULT_WEEKS):
    """A one-word read on how extended this category's positioning is.

    Deliberately blunt and deliberately only four bands. The point is to
    say "this is stretched" or "this is not" as a flag a strategy can
    branch on, not to produce a number that invites false precision. It
    returns None unless a percentile can actually be computed.
    """
    p = percentile(series or [], weeks) if series else None
    if p is None:
        return None
    if p >= 90:
        return "crowded-long"
    if p <= 10:
        return "crowded-short"
    if p >= 75:
        return "extended-long"
    if p <= 25:
        return "extended-short"
    return "neutral"


def analyse(rows, category, weeks=DEFAULT_WEEKS):
    """The full read for one category of one contract."""
    series = net_series(rows, category)
    latest = series[-1] if series else None
    return {
        "category": category,
        "observations": len(series),
        "window_weeks": int(weeks),
        "window_years": round(weeks / 52.0, 2),
        "sufficient": len(series) >= 2,
        "report_date": latest[0] if latest else None,
        "net": latest[1] if latest else None,
        "cot_index": cot_index(series, weeks),
        "percentile": percentile(series, weeks),
        "zscore": zscore(series, weeks),
        "weekly_change": weekly_change(series),
        "net_min": min([n for _d, n in window(series, weeks)]) if series else None,
        "net_max": max([n for _d, n in window(series, weeks)]) if series else None,
    }


def divergence(rows, category, closes, weeks=DEFAULT_WEEKS):
    """Positioning and price disagreeing.

    Takes the COT series for one category and a price series on the same
    contract, and reports whether net positioning and price moved in the
    same direction over the window.

    The interesting quadrant is disagreement: specs building shorts into a
    rally, or longs into a decline, which is where a positioning report
    has historically been most informative as a *contrarian* read. It
    reports the quadrant and both deltas; it does not assert a trade.

    `closes` is a list of prices ascending in time, and its length is
    matched to the COT series by taking the last N. Returns None rather
    than guessing when the two series cannot be aligned.
    """
    series = net_series(rows, category)
    if len(series) < 2 or not closes:
        return None
    px = [_f(c) for c in closes]
    px = [c for c in px if c is not None]
    n = min(len(series), len(px))
    if n < 2:
        return None
    s = series[-n:]
    p = px[-n:]
    d_net = s[-1][1] - s[0][1]
    d_px = p[-1] - p[0]
    if d_net == 0 or d_px == 0:
        out = {"net_change": d_net, "price_change": round(d_px, 8),
               "quadrant": "flat", "observations": n}
        return out
    same = (d_net > 0) == (d_px > 0)
    if same:
        quadrant = "confirming-long" if d_px > 0 else "confirming-short"
    else:
        quadrant = "shorts-building-into-rally" if d_px > 0 \
            else "longs-building-into-decline"
    return {
        "net_change": d_net,
        "price_change": round(d_px, 8),
        "same_direction": same,
        "quadrant": quadrant,
        "observations": n,
    }


def consensus(analyses):
    """Roll several category analyses into one read.

    `analyses` is a list of dicts from `analyse`. The per-category
    measures are averaged rather than combined into an index, because the
    categories are different populations with different typical nets and
    averaging their percentiles is the only combination that does not
    pretend they share a scale.

    Returns None when no category has enough history to say anything --
    an average of nothing is not a neutral reading, it is an absence of
    one.
    """
    usable = [a for a in (analyses or [])
              if a and a.get("percentile") is not None]
    if not usable:
        return None
    def avg(k):
        vals = [a[k] for a in usable if a.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    p = avg("percentile")
    band = "neutral"
    if p is not None:
        if p >= 90:
            band = "crowded-long"
        elif p <= 10:
            band = "crowded-short"
        elif p >= 75:
            band = "extended-long"
        elif p <= 25:
            band = "extended-short"
    return {
        "categories": [a["category"] for a in usable],
        "percentile": p,
        "cot_index": avg("cot_index"),
        "zscore": avg("zscore"),
        "band": band,
        "observations": max(a.get("observations") or 0 for a in usable),
    }


def staleness(report_date, now_ts=None):
    """How old a COT report is, in days, and what that means.

    A report dated Tuesday T is published Friday T+3. Read on the Friday
    it lands it is already three days stale, and by the following Friday
    it is ten. Positioning is a slow variable, so this is not a warning
    that the number is wrong -- it is the reason this data must never be
    treated as a price feed.
    """
    import time as _t
    d = str(report_date or "")
    if len(d) != 10:
        return None
    try:
        y, mo, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
        import calendar
        ts = calendar.timegm((y, mo, dd, 0, 0, 0, 0, 0, 0))
    except (TypeError, ValueError):
        return None
    now = int(now_ts if now_ts is not None else _t.time())
    age_days = int((now - ts) / 86400)
    return {
        "report_date": d,
        "age_days": age_days,
        "publication_lag_days": 3,
        "note": ("positions as of {}; the CFTC publishes on the Friday "
                 "three days later".format(d)),
    }

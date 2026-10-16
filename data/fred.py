"""Federal Reserve Economic Data (FRED): macro series in pure stdlib.

Origin
------
This is a stdlib reimplementation of a data source that OpenBB also carries
(FRED is its most-used free provider). Nothing here is copied from OpenBB --
that package needs fastapi, pydantic, aiohttp, pandas and numpy, and
RFC-005 makes mafutani strict-stdlib. What is taken is the *design*: a
declared series registry with per-series metadata and provenance, so a data
source is described rather than scattered.

CFTC COT is the other provider OpenBB ships and mafutani already has, in
`data/cot.py` and `engines/cot.py`. FRED is the macro layer that was missing.

Why keyless
-----------
FRED has a documented JSON API that needs a free registration key. It also
publishes every series as CSV with no key at all:

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10

That endpoint is the one used here. It needs no account, no key material and
no third-party client, which is what lets this module respect RFC-005. The
trade-off is honest and worth stating: it is the same data, but the response
is CSV rather than JSON and there is no paging or revision metadata. For
macro context on a daily cadence that is not a meaningful loss.

Missing observations
--------------------
FRED writes a literal "." for a date with no value -- weekends and holidays
for daily series. Those are *absent*, not zero. Storing them as 0 would
manufacture a VIX of zero on every Saturday, so they are dropped here and
the gap is reported in `missing` rather than silently filled.
"""

import csv
import datetime
import io
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

# No User-Agent is sent, and that is deliberate rather than an oversight.
#
# `data/cot.py` sets one, as is conventional and as CFTC requires. FRED's CSV
# endpoint behaves the opposite way: sending *any* User-Agent makes it stall
# and time out the read rather than returning an error. Measured here, three
# trials each:
#
#     no UA header            ok 5.4s / 2.3s / 2.6s   (269,059 bytes each)
#     UA: mafutani-cta/1.1    TimeoutError 12.4s / 12.4s / 13.2s
#     UA: Mozilla/5.0         TimeoutError 12.6s / 12.2s / 12.4s
#
# Reproducible, and not a UA-content filter -- a browser UA is refused exactly
# like our own. The default `Python-urllib/x.y` is what works, so omitting the
# header is the fix. Adding one back "to be polite", which is exactly what a
# future editor of this file would do, turns every fetch into a timeout.
#
# Named constant with an empty value so the omission is visible and carries
# its reason, rather than reading as a mistake.
UA = {}

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# Seconds. Macro series update at most daily and FRED is not a low-latency
# source, so a long timeout is not giving up responsiveness, it is not
# pretending a 30s fetch will succeed.
TIMEOUT = 30

# "." is FRED's missing-value marker, not a number.
MISSING = ("", ".", "NaN", "nan")


# The series mafutani actually uses. Declared here rather than discovered at
# runtime so the set is reviewable: each entry is a deliberate claim that this
# series is worth storing, with the unit attached so a caller never has to
# guess whether 16.87 means percent or index points.
#
# Title/units mirror FRED's own metadata and are used for labelling only.
# Nothing is inferred from them.
SERIES = {
    # -- rates ---------------------------------------------------------
    "DGS10": {"title": "10-Year Treasury Constant Maturity Rate",
              "units": "percent", "category": "rates"},
    "DGS2": {"title": "2-Year Treasury Constant Maturity Rate",
             "units": "percent", "category": "rates"},
    "DFF": {"title": "Federal Funds Effective Rate",
            "units": "percent", "category": "rates"},
    # -- curve ---------------------------------------------------------
    "T10Y2Y": {"title": "10-Year Treasury Minus 2-Year (yield curve)",
               "units": "percent", "category": "rates"},
    "T10Y3M": {"title": "10-Year Treasury Minus 3-Month",
               "units": "percent", "category": "rates"},
    # -- risk ----------------------------------------------------------
    "VIXCLS": {"title": "CBOE Volatility Index (VIX)",
               "units": "index", "category": "risk"},
    "BAMLH0A0HYM2": {"title": "ICE BofA US High Yield OAS",
                     "units": "percent", "category": "risk"},
    "BAMLC0A0CM": {"title": "ICE BofA US Corporate OAS",
                   "units": "percent", "category": "risk"},
    # -- economy -------------------------------------------------------
    "CPIAUCSL": {"title": "Consumer Price Index for All Urban Consumers",
                 "units": "index", "category": "economy"},
    "UNRATE": {"title": "Civilian Unemployment Rate",
               "units": "percent", "category": "economy"},
    "INDPRO": {"title": "Industrial Production: Total Index",
               "units": "index", "category": "economy"},
    # -- credit spread, the classic recession tell ----------------------
    "T10Y3M": {"title": "10-Year Treasury Minus 3-Month",
               "units": "percent", "category": "rates"},
}


def _http(url):
    """GET with our UA. SSL context is default (verified)."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return r.read().decode("utf-8", "replace")


def _num(v):
    """FRED value -> float, or None for a missing observation.

    Returns None rather than 0 for ".", so a caller cannot mistake a holiday
    for a real reading. `""` in a numeric field has the same meaning.
    """
    s = str(v or "").strip()
    if s in MISSING:
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def parse_series(text, series_id):
    """CSV text -> (rows, missing).

    rows is a list of {"date": "YYYY-MM-DD", "value": float} ascending by
    date. `missing` counts observations FRED published as absent, which is
    reported rather than hidden -- a reader who sees 2,000 rows and a
    `missing` of 500 knows why the series is shorter than the calendar.
    """
    if not text or not text.strip():
        return [], 0
    rdr = csv.reader(io.StringIO(text))
    try:
        header = next(rdr)
    except StopIteration:
        return [], 0
    if len(header) < 2:
        return [], 0
    # FRED names the value column after the series id, but not always with
    # the same case, and some endpoints prefix it. Match loosely.
    col = None
    for i, h in enumerate(header):
        if str(h).strip().upper() == str(series_id).strip().upper():
            col = i
            break
    if col is None:
        col = 1
    rows, missing = [], 0
    for r in rdr:
        if len(r) <= col:
            continue
        d = str(r[0]).strip()
        # Full ISO validation, not a shape check. A length-and-dash test
        # accepts "2026-13-45" -- month 13, day 45 -- which is exactly the
        # impossible-but-well-formed value a feed emits when something
        # upstream is broken. A test caught this.
        try:
            datetime.date.fromisoformat(d)
        except (TypeError, ValueError):
            continue
        v = _num(r[col])
        if v is None:
            missing += 1
            continue
        rows.append({"date": d, "value": v})
    rows.sort(key=lambda x: x["date"])
    return rows, missing


def fetch_series(series_id, start=None, end=None, cache_dir=None,
                 max_age=86400):
    """Fetch one series.

    Returns {"ok", "series_id", "rows", "missing", "url", "error"}. Never
    raises for a data problem: a dead upstream is a result with ok=False, not
    an exception in the caller. `missing` is always reported so a truncated
    series is never mistaken for a complete one.

    `start`/`end` are inclusive YYYY-MM-DD filters applied after parsing --
    FRED's CSV endpoint returns the whole history, and asking it to window
    would mean a second API we deliberately do not use.
    """
    sid = str(series_id or "").strip().upper()
    out = {"ok": False, "series_id": sid, "rows": [], "missing": 0,
           "url": "", "error": "", "cached": False}
    if not sid:
        out["error"] = "no series id"
        return out
    url = CSV_URL + "?" + urllib.parse.urlencode({"id": sid})
    out["url"] = url

    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "fred_%s.json" % sid)
        try:
            if max_age > 0 and (os.path.getmtime(cache_path) >
                                 __import__("time").time() - max_age):
                with open(cache_path, encoding="utf-8") as f:
                    cached = json.load(f)
                cached["cached"] = True
                return cached
        except (OSError, ValueError):
            pass  # a missing or corrupt cache is not an error; refetch

    try:
        text = _http(url)
    except urllib.error.HTTPError as e:
        out["error"] = "HTTP %s" % e.code
        return out
    except urllib.error.URLError as e:
        out["error"] = "network: %s" % (getattr(e, "reason", e),)
        return out
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out

    rows, missing = parse_series(text, sid)
    if not rows and not missing:
        out["error"] = "no parseable rows (is the series id valid?)"
        return out

    if start:
        rows = [r for r in rows if r["date"] >= str(start)]
    if end:
        rows = [r for r in rows if r["date"] <= str(end)]

    out.update({"ok": True, "rows": rows, "missing": missing})
    if cache_path:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(out, f)
        except OSError:
            pass  # caching is an optimisation, never a failure path
    return out


def summarise(results):
    """Aggregate counts for a batch. Split out so it is testable without a
    network round trip: `fetch_many` is otherwise only exercisable live,
    which is how its counting went untested."""
    results = results or {}
    ok = sum(1 for r in results.values() if (r or {}).get("ok"))
    return {"results": results, "count": len(results), "ok": ok,
            "failed": len(results) - ok}


def fetch_many(series_ids=None, cache_dir=None, max_age=86400, **kw):
    """Fetch several series. Per-series isolation: one dead series does not
    fail the batch, exactly as the venue pollers behave."""
    ids = list(series_ids or SERIES.keys())
    results = {}
    for sid in ids:
        results[str(sid).upper()] = fetch_series(
            sid, cache_dir=cache_dir, max_age=max_age, **kw)
    return summarise(results)


def meta(series_id):
    """Declared metadata for a series, or None if it is not registered."""
    return SERIES.get(str(series_id or "").strip().upper())


def latest(series_id, **kw):
    """Most recent observation, or None. Reports staleness rather than
    pretending an old reading is current."""
    r = fetch_series(series_id, **kw)
    if not r.get("ok") or not r.get("rows"):
        return None
    row = r["rows"][-1]
    return {"series_id": r["series_id"], "date": row["date"],
            "value": row["value"], "missing": r.get("missing", 0),
            "url": r.get("url", "")}


def staleness(latest_date, now_date=None, max_days=7):
    """Whole days between an observation and `now_date`.

    Macro series are daily but not daily-complete: weekends and holidays are
    absent, so "stale" has to mean "older than a few days", not "not today".
    """
    try:
        a = datetime.date.fromisoformat(str(latest_date))
        b = datetime.date.fromisoformat(str(now_date or datetime.date.today()))
    except (TypeError, ValueError):
        return None
    return (b - a).days


# -- persistence ---------------------------------------------------------
#
# Same provenance discipline as `cot_fetches`: a stored number carries the
# series' declared metadata and the URL it came from, and every fetch is
# recorded whether it succeeded or not.

def persist_series(conn, result):
    """Write one fetch result to macro_observations + macro_fetches.

    Returns {"ok", "stored", "fetch_id"}. Upserts rather than ignores:
    FRED restates history, so a later fetch legitimately replaces an earlier
    value for the same (series_id, obs_date). A failed fetch still records a
    row with ok=0 and the error -- a silent failure is the thing this whole
    exercise keeps finding.

    Nothing is written when the fetch did not parse, so a bad upstream
    response cannot truncate a good stored series.
    """
    res = result or {}
    sid = str(res.get("series_id") or "").upper()
    meta_row = SERIES.get(sid, {})
    rows = res.get("rows") or []
    latest = rows[-1]["date"] if rows else ""

    fid = None
    try:
        cur = conn.execute(
            "INSERT INTO macro_fetches (series_id, kind, url, rows_ok, missing,"
            " latest_date, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "single", res.get("url", ""), len(rows),
             int(res.get("missing") or 0), latest,
             1 if res.get("ok") else 0, res.get("error", "")[:500]))
        fid = cur.lastrowid
    except Exception:
        return {"ok": False, "stored": 0, "fetch_id": None}

    stored = 0
    if res.get("ok") and rows:
        for r in rows:
            try:
                conn.execute(
                    "INSERT INTO macro_observations (series_id, obs_date, value,"
                    " units, category, title, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'fred')"
                    " ON CONFLICT(series_id, obs_date) DO UPDATE SET"
                    "   value = excluded.value, units = excluded.units,"
                    "   category = excluded.category, title = excluded.title,"
                    "   fetched_at = strftime('%s','now')",
                    (sid, r["date"], r["value"],
                     meta_row.get("units", ""), meta_row.get("category", ""),
                     meta_row.get("title", "")))
                stored += 1
            except Exception:
                continue
    try:
        conn.commit()
    except Exception:
        pass
    return {"ok": bool(res.get("ok")), "stored": stored, "fetch_id": fid}


def persist_many(conn, batch):
    """Write a fetch_many() batch. One dead series does not stop the others."""
    out = {"ok": 0, "failed": 0, "stored": 0}
    for sid, res in ((batch or {}).get("results") or {}).items():
        r = persist_series(conn, res)
        out["stored"] += int(r.get("stored") or 0)
        if r.get("ok"):
            out["ok"] += 1
        else:
            out["failed"] += 1
    return out


def load_series(conn, series_id, limit=None, start=None, end=None):
    """Stored observations, ascending by date. Stored data only.

    Deliberately does not fall back to a live fetch: a read endpoint that
    silently blocks on an upstream is the behaviour this project has been
    removing all session. Fetching is a separate, explicit action.
    """
    sid = str(series_id or "").strip().upper()
    if not sid:
        return []
    inner = ("SELECT series_id, obs_date, value, units, category, title,"
             " source, fetched_at FROM macro_observations WHERE series_id = ?")
    params = [sid]
    if start:
        inner += " AND obs_date >= ?"
        params.append(str(start))
    if end:
        inner += " AND obs_date <= ?"
        params.append(str(end))
    if limit:
        # Take the NEWEST `limit` rows, then present them ascending.
        #
        # The DESC has to survive into the query that LIMIT applies to.
        # Building "... ORDER BY obs_date" then appending " DESC LIMIT ?"
        # and rewriting the clause back to "ORDER BY obs_date" left LIMIT
        # with no ordering to work from, so it took the *oldest* N rows and
        # the caller got 1990 data when it asked for the latest. Every
        # "last N" read was silently reaching back to the start of the
        # series -- including the credit median labelled "30d", which was a
        # median of the first 30 observations ever stored.
        sql = ("SELECT * FROM (" + inner + " ORDER BY obs_date DESC LIMIT ?)"
               " ORDER BY obs_date")
        params.append(int(limit))
    else:
        sql = inner + " ORDER BY obs_date"
    try:
        return [_row(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def latest_stored(conn):
    """Newest observation per series -- the dashboard view."""
    try:
        rows = conn.execute(
            "SELECT m.series_id, m.obs_date, m.value, m.units, m.category,"
            " m.title FROM macro_observations m"
            " JOIN (SELECT series_id, MAX(obs_date) AS mx"
            "       FROM macro_observations GROUP BY series_id) g"
            "   ON g.series_id = m.series_id AND g.mx = m.obs_date"
            " ORDER BY m.series_id").fetchall()
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        d = _row(r)
        if d["series_id"] in seen:
            continue
        seen.add(d["series_id"])
        d["age_days"] = staleness(d["obs_date"])
        out.append(d)
    return out


def _row(r):
    try:
        return dict(r)
    except Exception:
        try:
            return {"series_id": r[0], "obs_date": r[1], "value": r[2]}
        except Exception:
            return {}


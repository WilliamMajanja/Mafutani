"""Fetch real intraday OHLCV from Yahoo's chart API. Stdlib only, no key.

    python3 scripts/fetch_yahoo_intraday.py --list
    python3 scripts/fetch_yahoo_intraday.py --dry-run
    python3 scripts/fetch_yahoo_intraday.py --interval 1m
    python3 scripts/fetch_yahoo_intraday.py --days 29 --interval 5m

Why this exists
---------------
40 of this deployment's symbols were carrying 43,200 synthetic 1-minute bars
each, written by `scripts/seed.py` from a seeded RNG. The backtester read
only `candles_1m`, so a backtest over AAPL measured a random walk and
reported its Sharpe as though it described a strategy. Yahoo's chart API
does serve real 1-minute bars for equities, FX and futures -- verified live
against AAPL, EURUSD=X and BZ=F before this was written.

It writes to `candles_1m` with `source='yahoo-chart'`, which is what makes
`engines.dataprov` treat the bars as citable. Nothing here infers, resamples
or interpolates: a gap in the venue's data stays a gap, because a filled gap
is indistinguishable from a real one after the fact.

Yahoo's intraday limits, all enforced rather than discovered
------------------------------------------------------------
The API caps how far back it will serve each interval, and it returns a
*short* response rather than an error when asked for more. Requesting 6mo of
1m data returns 30 days and says nothing, so a caller that trusted the row
count would believe it had six months. `MAX_HISTORY` below is the documented
ceiling and the fetch is refused rather than truncated when asked for more:

    1m  30 days        5m  60 days       15m  60 days
    30m 60 days        1h  730 days      1d  no limit

Market hours, not 24/7
----------------------
Yahoo returns bars only when the instrument traded. AAPL at `range=7d`
yields ~2,500 bars, not 10,080, because equities do not trade overnight or at
the weekend. So gaps are expected and correct here, and the coverage report
says how much of the requested window was actually observed -- via
`core.asof.coverage_gap`, which reports an unobserved window as unobserved
rather than as an empty one.

Undocumented endpoint
---------------------
Unauthenticated, free, and liable to change or rate-limit without notice.
Delays between requests, per-symbol failure isolation, and an explicit
summary of what did not resolve -- "ingested 38 symbols" quietly reading as
"all" is the failure mode this avoids.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

# Yahoo's own tickers. The three LME base metals (NICKEL, TIN, LEAD) have no
# equivalent and are reported unresolved rather than mapped to something
# approximate -- see fetch_yahoo.py, which handles them for daily bars.
YAHOO_MAP = {
    "AAPL": "AAPL", "MSFT": "MSFT", "NVDA": "NVDA",
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "USDJPY": "JPY=X", "EURJPY": "EURJPY=X", "GBPJPY": "GBPJPY=X",
    "AUDJPY": "AUDJPY=X",
    "XAUUSD": "GC=F", "XAGUSD": "SI=F", "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    "WTI": "CL=F", "BRENT": "BZ=F", "NGAS": "NG=F", "HEATOIL": "HO=F",
    "GASOLINE": "RB=F",
    "CORN": "ZC=F", "WHEAT": "ZW=F", "SOYBEANS": "ZS=F", "SOYMEAL": "ZM=F",
    "SOYOIL": "ZL=F", "OATS": "ZO=F", "RRICE": "RY=F",
    "COFFEE": "KC=F", "COCOA": "CC=F", "COTTON": "CT=F", "SUGAR": "SB=F",
    "ORANGE": "OJ=F", "LHOGS": "HE=F", "LCATTLE": "LE=F", "FCATTLE": "GF=F",
    "COPPER": "HG=F", "ALUM": "ALI=F",
}

# The three with no Yahoo ticker at all. Named so they are reported rather
# than silently absent.
UNRESOLVED = {
    "NICKEL": "no Yahoo ticker; LME nickel has no listed continuous contract",
    "TIN": "no Yahoo ticker; LME tin has no listed continuous contract",
    "LEAD": "no Yahoo ticker; LME lead has no listed continuous contract",
}

# Ceiling on how far back Yahoo serves each interval. Requesting more returns
# a short response with no error, so this is enforced by refusal instead.
MAX_HISTORY = {
    "1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60,
    "60m": 730, "1h": 730, "1d": 0,
}

INTERVALS = ("1m", "2m", "5m", "15m", "30m", "60m")

# Seconds between requests. Yahoo returns 429 readily under load, and a
# backfill that dies at symbol 30 of 40 is worse than a slow one.
DELAY_S = 1.5

SOURCE = "yahoo-chart"

# A 429 is the one failure worth waiting out rather than retrying promptly.
# An earlier version retried three times with a 1.5s backoff and lost all 39
# symbols: a rate-limited window was still limited at +4.5s, so every symbol
# burned all three attempts inside the same window and the run reported
# "ingested 0" having never actually tried a slow request. The backoff below
# crosses the window instead of hammering it.
RATE_LIMIT_BACKOFF_S = (30, 60, 120, 240, 420)


def fetch_one(ticker, interval="5m", days=60, timeout=20, retries=3,
              verbose=False):
    """One symbol's bars. Returns (rows, error_message).

    rows: [{"ts", "open", "high", "low", "close", "volume"}], ascending.
    """
    rng = "%dd" % max(1, min(int(days), 730))
    url = "%s%s?interval=%s&range=%s" % (
        CHART_URL, urllib.parse.quote(ticker), interval, rng)
    headers = {
        # Verified against the live endpoint: a browser-shaped UA is served,
        # and `Accept: application/json` is required (`text/html` gets 406).
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    last = None
    rate_attempt = 0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers),
                    timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            return _extract(payload)
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code == 429 and rate_attempt < len(RATE_LIMIT_BACKOFF_S):
                wait = RATE_LIMIT_BACKOFF_S[rate_attempt]
                rate_attempt += 1
                if verbose:
                    print("        rate limited; waiting %ds" % wait,
                          file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 429:
                # Backoff schedule exhausted. Say so plainly rather than
                # reporting a bare code that reads like a permanent block.
                return [], ("rate limited: still throttled after %s -- "
                            "wait a few minutes and re-run with "
                            "--skip-real" % ", ".join(
                                "%ds" % s for s in RATE_LIMIT_BACKOFF_S[:rate_attempt]))
            return [], last
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            time.sleep(2.0 * (attempt + 1))
    return [], "gave up after %d tries: %s" % (retries, last)

def _extract(payload):
    """Chart JSON -> ascending rows, or ([], reason)."""
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        return [], "yahoo error: %s" % (chart["error"],)
    results = chart.get("result") or []
    if not results:
        return [], "empty result"
    res = results[0]

    ts = res.get("timestamp") or []
    if not ts:
        return [], "no timestamps"
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c, v = (quote.get(k) or [] for k in
                     ("open", "high", "low", "close", "volume"))
    # A partial bar is still forming; including it means the newest row
    # changes on every read and any indicator over it is unrepeatable.
    meta = res.get("meta") or {}
    regular = meta.get("regularMarketTime")
    cutoff = int(regular) if regular else None

    rows = []
    for i, t in enumerate(ts):
        vals = (o[i] if i < len(o) else None, h[i] if i < len(h) else None,
                l[i] if i < len(l) else None, c[i] if i < len(c) else None)
        if any(x is None for x in vals):
            continue
        try:
            op, hi, lo, cl = (float(x) for x in vals)
        except (TypeError, ValueError):
            continue
        if not (hi >= lo > 0 and op > 0 and cl > 0):
            continue
        ts_i = int(t)
        if cutoff is not None and ts_i >= cutoff:
            continue
        vol = float(v[i]) if i < len(v) and v[i] is not None else 0.0
        rows.append({"ts": ts_i, "open": op, "high": hi, "low": lo,
                     "close": cl, "volume": vol})
    rows.sort(key=lambda r: r["ts"])
    return rows, ""


def ingest(conn, symbol, rows, source=SOURCE):
    """Upsert bars with their provenance. Returns (written, rejected)."""
    batch = [(symbol, r["ts"], r["open"], r["high"], r["low"], r["close"],
              r["volume"], source) for r in rows]
    if not batch:
        return 0, 0
    last = None
    for attempt in range(20):
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO candles_1m"
                " (symbol, ts, open, high, low, close, volume, source)"
                " VALUES (?,?,?,?,?,?,?,?)", batch)
            conn.commit()
            return len(batch), 0
        except sqlite3.OperationalError as e:
            last = e
            time.sleep(1.0 + attempt * 0.5)
    raise last


def coverage_of(rows, days):
    """How much of the requested window was actually observed.

    Uses `core.asof.coverage_gap`: an instrument that traded for 6 hours a
    day has not "missed" the rest, but a response that stops two weeks short
    of what was asked for has, and the two must not read the same.
    """
    try:
        from core import asof
    except Exception:
        return {"observed_bars": len(rows or []), "gap": "coverage unavailable"}
    if not rows:
        return {"observed_bars": 0, "gap": "no observations returned"}
    # The coverage question here is narrow, and an earlier version of this
    # function got it wrong in a way worth recording: it passed the
    # requested window as both the thing asked for *and* the feed's declared
    # horizon, so `coverage_gap` compared 55 days against 55 days and
    # reported a one-day shortfall as a gap on all 39 symbols.
    #
    # Two lessons. A check that fires on every input trains you to ignore it.
    # And `range=55d` already tells us how far the endpoint looked -- asking
    # again is circular.
    #
    # So no `tail_lookback` is declared, which asserts nothing about what the
    # feed could reach, and the verdict rests on the response: what arrived,
    # and whether it is plausibly a full window of trading rather than a stub.
    first, last = rows[0]["ts"], rows[-1]["ts"]
    observed_span_days = max(1, (last - first) // asof.DAY)
    gap = None
    if not rows:
        gap = "no observations returned"
    elif len(rows) < 20:
        gap = ("only %d bars -- too few to distinguish a thin instrument "
               "from a truncated response" % len(rows))
    elif observed_span_days < max(1, int(days) * 0.5):
        gap = ("response spans %d days of the %d requested, so at least half "
               "the window was not served; treat the earlier period as "
               "unobserved rather than quiet" % (observed_span_days,
                                                 int(days)))
    return {"observed_bars": len(rows), "from": first, "to": last,
            "observed_span_days": observed_span_days,
            "requested_days": int(days), "gap": gap}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", default="5m", choices=list(INTERVALS))
    ap.add_argument("--days", type=int, default=60,
                    help="requested window; capped to Yahoo's ceiling")
    ap.add_argument("--symbols", default="",
                    help="comma-separated subset")
    ap.add_argument("--out", default="", help="write to this db instead")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--skip-real", action="store_true",
                    help="skip symbols that already have real bars, so a "
                         "rate-limited run can be resumed")
    args = ap.parse_args(argv)

    if args.list:
        print("%d symbols map to a Yahoo ticker:" % len(YAHOO_MAP))
        for k, v in sorted(YAHOO_MAP.items()):
            print("  %-10s %s" % (k, v))
        print("\n%d unresolved (no ticker, reported not guessed):" % len(UNRESOLVED))
        for k, why in sorted(UNRESOLVED.items()):
            print("  %-10s %s" % (k, why))
        return 0

    ceiling = MAX_HISTORY.get(args.interval, 60)
    days = int(args.days)
    if ceiling and days > ceiling:
        print("refusing: Yahoo serves %s only %d days back, and asking for "
              "more returns a short response with no error -- so the request "
              "would silently under-deliver. Use --interval %s for a longer "
              "window." % (args.interval, ceiling,
                            _next_interval(args.interval)))
        return 2

    db_path = args.out or os.path.join(ROOT, "data", "trading.db")
    if args.dry_run:
        print("dry run: %d symbols, interval=%s, %d days"
              % (len(YAHOO_MAP), args.interval, days))
        for k, v in sorted(YAHOO_MAP.items())[:5]:
            print("  %-10s -> %s" % (k, v))
        print("  ... (%d more)" % max(0, len(YAHOO_MAP) - 5))
        return 0

    want = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    targets = {k: v for k, v in YAHOO_MAP.items()
               if not want or k in want}

    if not os.path.exists(db_path):
        print("no database at %s" % db_path)
        return 1
    conn = sqlite3.connect(db_path, timeout=30.0)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(candles_1m)")}
    if "source" not in cols:
        print("candles_1m has no `source` column; start the app once so the "
              "migration runs, or ingest via data.db.connect()")
        return 1

    ok, failed, empty, skipped = [], [], [], []

    def already_real(sym, interval, skip_real):
        """True when this symbol already carries real bars at this interval.

        Lets a rate-limited run be resumed without re-fetching what landed,
        which is what turned one throttled window into "ingested 0".
        """
        if not skip_real:
            return False
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM candles_1m WHERE symbol = ?"
                " AND source = ?", (sym, SOURCE)).fetchone()
            return int(row[0] or 0) > 0
        except Exception:
            return False

    print("fetching %d symbols at %s over %d days"
          % (len(targets), args.interval, days))
    for sym, ticker in sorted(targets.items()):
        if already_real(sym, args.interval, args.skip_real):
            skipped.append(sym)
            print("  skip %-10s already has real %s bars"
                  % (sym, args.interval))
            continue
        rows, err = fetch_one(ticker, args.interval, days, verbose=True)
        if err:
            failed.append((sym, err))
            print("  FAIL %-10s %s" % (sym, err))
            time.sleep(DELAY_S)
            continue
        if not rows:
            empty.append(sym)
            print("  EMPTY %-9s no bars returned" % sym)
            time.sleep(DELAY_S)
            continue
        n, _ = ingest(conn, sym, rows)
        cov = coverage_of(rows, days)
        ok.append(sym)
        print("  ok   %-10s %-9s %6d bars  %d..%d%s"
              % (sym, ticker, n, rows[0]["ts"], rows[-1]["ts"],
                 "  [gap: %s]" % cov["gap"] if cov.get("gap") else ""))
        time.sleep(DELAY_S)
    conn.close()

    print()
    print("ingested %d of %d symbols" % (len(ok), len(targets) - len(skipped)))
    if skipped:
        print("  skipped (%d, already real): %s"
              % (len(skipped), ", ".join(sorted(skipped))))
    for sym, why in sorted(UNRESOLVED.items()):
        print("  UNRESOLVED %-10s %s" % (sym, why))
    if empty:
        print("  returned no bars: %s" % ", ".join(sorted(empty)))
    if failed:
        print("  failed: %s" % ", ".join(s for s, _ in failed))
        print("  re-run to resume; --skip-real will not retry these")
    # The summary exists so "we ingested 38" cannot be read as "all".
    return 0 if ok else 1


def _next_interval(current):
    order = list(INTERVALS)
    try:
        i = order.index(current)
    except ValueError:
        return "15m"
    return order[min(i + 1, len(order) - 1)]


if __name__ == "__main__":
    sys.exit(main())

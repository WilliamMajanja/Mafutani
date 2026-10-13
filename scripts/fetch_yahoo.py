"""Fetch daily OHLCV from Yahoo's chart API. Stdlib only, no API key.

    python3 scripts/fetch_yahoo.py --list
    python3 scripts/fetch_yahoo.py --dry-run
    python3 scripts/fetch_yahoo.py --out data/real_daily.db

Why this exists
---------------
42 of this deployment's symbols -- equities, commodities and FX -- have no
free 1-minute source. Binance covers the crypto pairs; nothing free covers
intraday for the rest. Yahoo's chart API carries *years* of daily OHLCV,
and that is what real data genuinely means for these instruments.

It goes into `candles_1d`, never into `candles_1m`. Everything downstream is
resolution-aware in ways that fail quietly: `engines/backtest.py` annualises
with `sqrt(252 * 390)`, a 1-minute constant; indicators are parameterised per
bar; and a daily bar filed as a minute bar makes every return and every
significance figure wrong in a direction that looks entirely plausible.
Resampling daily OHLC into minutes would invent information that was never
in the data, which is the exact defect `scripts/seed.py` already committed.
So the data is stored at the resolution it actually has.

Yahoo is an undocumented, unsupported endpoint
----------------------------------------------
It is unauthenticated and free, and it can change or rate-limit without
notice. Two consequences are built in rather than discovered later:

  * A modest delay between requests. Yahoo returns 429 readily under load,
    and a backfill that dies at symbol 30 of 44 is worse than a slow one.
  * Failures are per-symbol and reported. One delisted ticker must not
    abort the other 43, and the summary says exactly which ones did not
    resolve, because "we ingested 41 symbols" quietly reads as "all".

The three LME base metals (NICKEL, TIN, LEAD) have no Yahoo ticker; they
are reported as unresolved rather than mapped to something approximate.
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

# Our symbol -> Yahoo ticker. Two different naming worlds: we carry the
# instrument names a broker uses (ALUM, BRENT, XAUUSD), Yahoo carries
# exchange-native futures and FX codes (ALI=F, BZ=F, EURUSD=X).
#
# Only mappings that were checked against a live response are listed. The
# three with no Yahoo equivalent are deliberately absent rather than
# guessed at -- see UNRESOLVED below.
YAHOO_MAP = {
    # equities
    "AAPL": "AAPL", "MSFT": "MSFT", "NVDA": "NVDA",
    # fx
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
    "EURJPY": "EURJPY=X", "GBPJPY": "GBPJPY=X", "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X", "USDCAD": "CAD=X", "USDCHF": "CHF=X",
    "EURGBP": "EURGBP=X", "AUDJPY": "AUDJPY=X",
    # precious metals
    "XAUUSD": "GC=F", "XAGUSD": "SI=F", "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # energy
    "WTI": "CL=F", "BRENT": "BZ=F", "NGAS": "NG=F",
    "GASOLINE": "RB=F", "HEATOIL": "HO=F",
    # LME / COMEX / ICE metals
    "ALUM": "ALI=F", "ZINC": "ZNC=F", "COPPER": "HG=F",
    # softs
    "COFFEE": "KC=F", "COCOA": "CC=F", "COTTON": "CT=F", "SUGAR": "SB=F",
    "OJ": "OJ=F", "SOYBEANS": "ZS=F", "SOYMEAL": "ZM=F", "SOYOIL": "ZL=F",
    "WHEAT": "ZW=F", "CORN": "ZC=F", "OATS": "ZO=F", "RRICE": "ZR=F",
    # livestock
    "LHOGS": "HE=F", "LCATTLE": "LE=F", "FCATTLE": "GF=F",
}

# No Yahoo ticker exists for these. Reported as unresolved every run rather
# than mapped to a near-miss, because a plausible-looking wrong metal is
# worse than a visible gap.
UNRESOLVED = {
    "NICKEL": "Yahoo carries no LME nickel future",
    "TIN": "Yahoo carries no LSE tin future",
    "LEAD": "Yahoo carries no LME lead future",
}

DEFAULT_DELAY_S = 0.35
UA = "Mozilla/5.0 (compatible; mafutani/1)"

# A daily bar moving more than this fraction in one session is treated as a
# bad print rather than a market event.
#
# Real markets do jump -- COVID, a currency peg break, a limit-up -- but none
# of the 41 symbols below produced a single daily move above 12%. This
# filter exists because Yahoo does serve garbage: ZNC=F carries 3660.00 flat
# across six sessions against a true 2297.00, with zero volume, and
# ingesting it gave ZINC a raw kurtosis of 1205. That one bad print would
# dominate any volatility or significance figure computed over the series,
# and would do so invisibly, because the row looks exactly like a real bar.
#
# It is a *detection* filter, not a silent correction: offending sessions
# are counted and reported, never quietly interpolated away. A gap in the
# tape is honest; a fabricated bar is the thing this project spent a day
# removing.
# A daily move this large is *reported*, not deleted.
#
# It was originally a deletion threshold, and that removed 179 of NGAS's
# 1258 bars -- including the +46% and -47% sessions of January 2022, when
# European gas did exactly that. Those are real. EURGBP's +11.6% and
# -10.5% are the UK mini-budget of October 2022, also real.
#
# Deleting genuine history because a threshold judged it too volatile is
# the same class of error as fabricating history: both make the tape stop
# describing the world. So detection and deletion are separated -- see
# `strip_bad_prints`, which now only removes what `is_degenerate` would
# refuse anyway, and requires an explicit --drop-implausible to act.
IMPLAUSIBLE_DAILY_MOVE = 0.35

# Absolute threshold used when --drop-implausible is given. A 100% daily
# move is not credible for any instrument in this set; 35% plainly is.
DROP_THRESHOLD = 1.0

# A session with no volume at all is a Yahoo placeholder for "no session".
MIN_VOLUME = 1e-9

# A session with no volume at all is a Yahoo placeholder for "no session".
MIN_VOLUME = 1e-9


def find_bad_prints(rows, threshold=IMPLAUSIBLE_DAILY_MOVE):
    """Indices of bars whose move from the previous close is implausible."""
    bad = []
    for i in range(1, len(rows)):
        prev = rows[i - 1]["close"]
        if prev <= 0:
            continue
        if abs(rows[i]["close"] / prev - 1.0) > threshold:
            bad.append(i)
    return bad


def strip_bad_prints(rows, threshold=DROP_THRESHOLD):
    """Remove only bars that no credible market produced. -> (kept, dropped).

    The threshold defaults to `DROP_THRESHOLD` (100% in a session), not to
    `IMPLAUSIBLE_DAILY_MOVE`. The lower figure is used for *reporting* only.

    An earlier version applied the 35% figure as a deletion rule and lost
    179 of NGAS's 1258 bars, including both halves of the January 2022
    European gas spike. Those moves are the market. It also applied a
    zero-volume flat-run rule, which removed 1254 of ZINC's 1255 bars
    because Yahoo reports no volume at all for that thin contract.

    Iterated so a sustained level shift goes as a block rather than leaving
    five identical bars behind the one that triggered it.
    """
    if len(rows) < 3:
        return list(rows), []
    kept = list(rows)
    dropped = []
    while len(kept) >= 3:
        bad = find_bad_prints(kept, threshold)
        if not bad:
            break
        badset = set(bad)
        dropped.extend(kept[i].get("ts") for i in bad)
        kept = [r for k, r in enumerate(kept) if k not in badset]
    return kept, dropped


# A daily series with fewer than this many *distinct* closes is not a market.
#
# Yahoo serves a plausible-looking but entirely dead series for some thinly
# traded contracts. ZNC=F returns 2297.00 for 1171 consecutive sessions --
# one distinct close, zero non-zero returns. Ingested as-is it becomes a
# real-looking instrument with exactly zero volatility, which is worse than
# missing: a backtest on it reports no risk and no return, and that reads as
# a calm market rather than as no data at all.
MIN_DISTINCT_CLOSES = 30


def is_degenerate(rows, min_distinct=MIN_DISTINCT_CLOSES):
    """True when the series carries no usable movement. -> (bool, why)."""
    closes = {round(r["close"], 6) for r in rows}
    if len(closes) < min_distinct:
        return True, ("only %d distinct close(s) across %d bars"
                      % (len(closes), len(rows)))
    rets = [(rows[i]["close"] / rows[i - 1]["close"] - 1.0)
            for i in range(1, len(rows)) if rows[i - 1]["close"] > 0]
    if not rets:
        return True, "no computable returns"
    moved = sum(1 for r in rets if abs(r) > 1e-9)
    if moved < len(rets) * 0.05:
        return True, ("only %d of %d sessions moved at all"
                      % (moved, len(rets)))
    return False, ""


def fetch_daily(ticker, rng="5y", timeout=25, retries=3):
    """One ticker of daily OHLCV -> list of candle dicts.

    Yahoo returns timestamps plus parallel OHLCV arrays, and it routinely
    emits nulls for a bar it has no session for. Nulls are dropped rather
    than coerced to 0: a zero close would show up later as a -100% return.
    """
    url = CHART_URL + urllib.parse.quote(ticker) + "?range=%s&interval=1d" % rng
    delay = 1.0
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503):
                last = e
                time.sleep(delay)
                delay = min(delay * 2, 20.0)
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            last = e
            if attempt + 1 >= retries:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 20.0)
    else:
        raise last

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(str(chart["error"])[:120])
    results = chart.get("result") or []
    if not results:
        return []
    r0 = results[0]
    ts = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c, v = (q.get(k) or [] for k in
                     ("open", "high", "low", "close", "volume"))
    out = []
    for i, t in enumerate(ts):
        vals = (o[i] if i < len(o) else None, h[i] if i < len(h) else None,
                l[i] if i < len(l) else None, c[i] if i < len(c) else None,
                v[i] if i < len(v) else None)
        if any(x is None for x in vals[:4]):
            continue
        op, hi, lo, cl = (float(x) for x in vals[:4])
        if not (hi >= lo > 0 and op > 0 and cl > 0):
            continue
        out.append({"ts": int(t), "open": op, "high": hi, "low": lo,
                    "close": cl,
                    "volume": float(vals[4]) if vals[4] is not None else 0.0})
    return out


def ingest(conn, symbol, rows):
    """Upsert daily bars. Returns (written, rejected)."""
    batch = [(symbol, r["ts"], r["open"], r["high"], r["low"], r["close"],
              r["volume"]) for r in rows]
    if not batch:
        return 0, 0
    last = None
    for attempt in range(20):
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO candles_1d"
                " (symbol, ts, open, high, low, close, volume)"
                " VALUES (?,?,?,?,?,?,?)", batch)
            conn.commit()
            break
        except sqlite3.OperationalError as e:
            last = e
            time.sleep(1.0 + attempt * 0.5)
    else:
        raise last
    return len(batch), 0


def report(rows, symbol, ticker):
    """Print the properties that distinguish real data from the seeder's."""
    if len(rows) < 30:
        return
    closes = [r["close"] for r in rows]
    rets = [(closes[i] / closes[i - 1] - 1.0)
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    if not rets:
        return
    m = sum(rets) / len(rets)
    m2 = sum((x - m) ** 2 for x in rets) / len(rets)
    m4 = sum((x - m) ** 4 for x in rets) / len(rets)
    kurt = (m4 / (m2 * m2)) if m2 > 0 else 0.0
    sq = [x * x for x in rets]
    ms = sum(sq) / len(sq)
    var = sum((x - ms) ** 2 for x in sq) / max(len(sq) - 1, 1)
    acf = (sum((sq[i] - ms) * (sq[i + 1] - ms)
               for i in range(len(sq) - 1)) / (len(sq) - 1) / var
           if var > 0 else 0.0)
    # Daily equity/FX kurtosis runs 3-5 routinely, so the bar that
    # means "suspicious" here is well above what a normal daily series
    # shows. A 1-minute crypto threshold of 5 flagged half the book.
    flag = "" if kurt > 40 else "   <- check this series"
    print("    %-9s %-9s %5d bars  raw kurtosis %6.2f  ACF(r^2) %+.4f%s"
          % (symbol, ticker, len(rows), kurt, acf, flag))


def main():
    ap = argparse.ArgumentParser(
        description="Fetch daily OHLCV into candles_1d (never candles_1m).")
    ap.add_argument("--db", default=os.environ.get(
        "DB_PATH", os.path.join("data", "trading.db")))
    ap.add_argument("--out", help="write here instead of --db")
    ap.add_argument("--range", dest="rng", default="5y",
                    help="Yahoo range: 1y, 2y, 5y, 10y (default 5y)")
    ap.add_argument("--symbols", nargs="*", help="restrict to these symbols")
    ap.add_argument("--list", action="store_true",
                    help="print the symbol map and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write nothing")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_S)
    ap.add_argument("--drop-implausible", action="store_true",
                    help="delete sessions moving more than 100%%, instead of "
                         "only reporting them. Off by default: the flagged "
                         "moves in this universe have all turned out to be "
                         "real (UK mini-budget 2022, European gas 2022), and "
                         "deleting real history is the same error as "
                         "fabricating it.")
    args = ap.parse_args()

    if args.list:
        print("  mapped to Yahoo (%d):" % len(YAHOO_MAP))
        for s, t in sorted(YAHOO_MAP.items()):
            print("    %-9s -> %s" % (s, t))
        print("  no Yahoo ticker (%d):" % len(UNRESOLVED))
        for s, why in sorted(UNRESOLVED.items()):
            print("    %-9s    %s" % (s, why))
        return 0

    wanted = [s.upper() for s in (args.symbols or [])] or sorted(YAHOO_MAP)
    target = args.out or args.db

    from data.db import init_db, connect
    if not args.dry_run:
        try:
            c = init_db(target)
            c.close()
        except Exception:
            pass
    conn = connect(target)

    ok, failed, empty, dropped_syms = [], [], [], []
    try:
        for sym in wanted:
            ticker = YAHOO_MAP.get(sym)
            if not ticker:
                failed.append((sym, UNRESOLVED.get(sym, "no mapping")))
                continue
            try:
                rows = fetch_daily(ticker, rng=args.rng)
            except Exception as e:
                failed.append((sym, "%s: %s" % (type(e).__name__,
                                                str(e)[:60])))
                sys.stderr.write("    %-9s %s -> %s\n"
                                 % (sym, ticker, type(e).__name__))
                time.sleep(args.delay)
                continue
            if not rows:
                empty.append(sym)
                sys.stderr.write("    %-9s %s -> no bars returned\n"
                                 % (sym, ticker))
                time.sleep(args.delay)
                continue

            raw_count = len(rows)
            suspect = len(find_bad_prints(rows, IMPLAUSIBLE_DAILY_MOVE))
            if args.drop_implausible:
                rows, dropped = strip_bad_prints(rows)
            else:
                dropped = []
            if suspect:
                # Reported even when nothing is deleted. EURGBP's +11.6% and
                # NGAS's +46% land here and are real market events; the
                # operator needs to see that a run of days was flagged, and
                # needs the fact that they were kept.
                dropped_syms.append((sym, suspect, raw_count,
                                     len(dropped)))
                sys.stderr.write(
                    "    %-9s %d session(s) moved >%.0f%%; %s\n"
                    % (sym, suspect, IMPLAUSIBLE_DAILY_MOVE * 100,
                       "dropped %d" % len(dropped) if dropped
                       else "kept (real market move)"))
            if not rows:
                empty.append(sym)
                continue

            dead, why = is_degenerate(rows)
            if dead:
                # Refuse rather than store. A zero-volatility instrument
                # masquerading as a real one is a worse failure than an
                # absent one.
                failed.append((sym, "degenerate series: %s" % why))
                sys.stderr.write("    %-9s %s -> %s\n" % (sym, ticker, why))
                continue

            report(rows, sym, ticker)
            if not args.dry_run:
                conn.execute("INSERT OR IGNORE INTO symbols (symbol) VALUES (?)",
                             (sym,))
                conn.commit()
                w, rej = ingest(conn, sym, rows)
                print("      wrote %d rows (rejected %d)" % (w, rej))
            ok.append(sym)
            time.sleep(args.delay)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print()
    print("  ingested %d / %d" % (len(ok), len(wanted)))
    if empty:
        print("  returned no bars: %s" % ", ".join(empty))
    if dropped_syms:
        print("  dropped suspect bars (%d symbol(s)):" % len(dropped_syms))
        for s, n, total, actually in dropped_syms:
            print("    %-9s %d session(s) >%.0f%% flagged of %d bars; %d removed"
                  % (s, n, IMPLAUSIBLE_DAILY_MOVE * 100, total, actually))
    if failed:
        print("  unresolved (%d):" % len(failed))
        for s, why in failed:
            print("    %-9s %s" % (s, why))
    if args.dry_run:
        print("  dry run; nothing written")
    return 0 if not failed and not empty else 1


if __name__ == "__main__":
    sys.exit(main())

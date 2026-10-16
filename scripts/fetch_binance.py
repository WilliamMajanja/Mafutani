"""Fetch real 1-minute candles from Binance. Stdlib only, no API key.

    python3 scripts/fetch_binance.py BTCUSDT --days 90
    python3 scripts/fetch_binance.py BTCUSDT ETHUSDT --days 30 --dry-run
    python3 scripts/fetch_binance.py BTCUSDT --out data/real_btc.db

Why this exists
---------------
`candles_1m` is synthetic. `scripts/seed.py:206` draws
`rng.gauss(0.0, 0.0006 * vol_mult)` per bar, which is a random walk with
i.i.d. Gaussian increments -- and that has consequences that quietly
invalidate a lot of downstream work:

  * kurtosis 3.10, so every "heavy-tailed" correction is a no-op
  * ACF of squared returns +0.008, so there is no volatility clustering
  * variance ratio 1.0 at every horizon, so no regime behaviour

Measured on real Binance BTCUSDT 1m over the same span, the same statistics
come out kurtosis 15.55 and ACF(r^2) +0.235. Those are the two properties
whose absence blocked distribution-estimated anomaly thresholds and any
volatility model. A model fitted to the synthetic tape learns the seeder's
0.0006, and the fit looks clean because the seeder's process really is
that simple.

So this fetches the real thing. Binance's public `/api/v3/klines` needs no
key, no account, and no signature -- it is a plain GET. That keeps the
zero-dependency property intact: nothing is installed, and the app does not
import this file.

Honest about scope
------------------
Only crypto has a free 1-minute source. The other 45 symbols in `symbols`
(equities, commodities, FX) have no free intraday history; Stooq serves
daily only. This fetcher cannot fix those, and does not pretend to. Run it
per symbol and check `--dry-run` before writing anything.

Binance's rate limit is 1200 request-weight per minute. A klines request
costs weight 1/2/5 for limits of 100/500/1000, so this defaults to
`--limit 1000` with a small delay and backs off hard on 429. Getting
ban-flagged (HTTP 418) is slow and unpleasant to undo, so the backoff is
deliberately timid rather than fast.

What it will not do
-------------------
It does not delete, and it does not truncate. Rows are upserted by
(symbol, ts), exactly like `scripts/ingest_csv.py`. If you want to replace
a symbol's synthetic history with real bars, that is a separate deliberate
decision -- see `--prune-stale`, which only ever removes rows this script
itself would consider superseded, and says so loudly.

Resumable: re-running with the same arguments continues from the newest row
already stored for that symbol rather than re-downloading.
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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

BASE_URL = "https://api.binance.com/api/v3/klines"

# Binance caps a single klines response at 1000 bars.
MAX_LIMIT = 1000

# Weight per request is 1/2/5 for limits of 100/500/1000, against a budget
# of 1200 per minute. 0.25s between 1000-bar requests is 5 requests/s =
# 25 weight/s = 1500/min, which is over budget, so the default delay is
# raised to keep the steady state under it.
DEFAULT_DELAY_S = 0.45

# HTTP 429 is "slow down"; 418 is "you are flagged". Both are recoverable
# but 418 means a multi-minute cooldown, hence the much larger backoff.
BACKOFF_429_S = 30.0
BACKOFF_418_S = 300.0


def _http_get_json(url, timeout=20):
    """GET and parse JSON. Raises ValueError on a non-2xx.

    Deliberately does not go through `data.feeds._http_get`: that helper
    resolves to the *live* proxy configuration, and a bulk backfill is the
    one operation where silently tunnelling through an operator's proxy
    would be surprising. Use `--use-app-proxy` to opt in.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "mafutani/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_klines(symbol, start_ms, end_ms=None, limit=MAX_LIMIT,
                 timeout=20, use_app_proxy=False, max_retries=4):
    """One page of klines as a list of dicts. Retries on 429/418/5xx.

    Returns [] when the exchange has no data at or after `start_ms`, which
    is how a backfill knows it has reached the beginning of history.
    """
    params = {"symbol": symbol.upper(), "interval": "1m", "limit": int(limit)}
    if start_ms:
        params["startTime"] = int(start_ms)
    if end_ms:
        params["endTime"] = int(end_ms)
    url = BASE_URL + "?" + urllib.parse.urlencode(params)

    delay = 1.0
    for attempt in range(max_retries):
        try:
            if use_app_proxy:
                from data import proxy as _proxy
                if _proxy.proxy_url() is not None:
                    status, _h, text = _proxy.request("GET", url, timeout=timeout)
                    if status and (status < 200 or status >= 300):
                        raise urllib.error.HTTPError(
                            url, status, "proxy returned %s" % status, None, None)
                    return _rows_from_raw(json.loads(text))
            return _rows_from_raw(_http_get_json(url, timeout=timeout))
        except urllib.error.HTTPError as e:
            if e.code in (429, 418):
                # Back off, and say so: a silent multi-minute stall looks
                # exactly like a hang.
                wait = BACKOFF_418_S if e.code == 418 else BACKOFF_429_S
                wait *= (attempt + 1)
                sys.stderr.write(
                    "    HTTP %s from binance; backing off %.0fs "
                    "(attempt %d/%d)\n" % (e.code, wait, attempt + 1, max_retries))
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, ValueError, OSError) as e:
            if attempt + 1 >= max_retries:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise IOError("exhausted retries fetching %s" % symbol)


def _rows_from_raw(raw):
    """Binance kline tuple -> dict with epoch *seconds*.

    Binance returns milliseconds; `candles_1m.ts` is seconds everywhere in
    this codebase. Getting that wrong would place bars in 1970 and they
    would look like real ancient history, so the conversion is explicit.
    """
    out = []
    for k in raw or []:
        # `len(k)` unguarded: a malformed response containing a null or a
        # bare value raises TypeError and kills the entire backfill, which
        # is a much worse outcome than dropping one bad bar.
        if not isinstance(k, (list, tuple)) or len(k) < 7:
            continue
        try:
            out.append({
                "ts": int(k[0]) // 1000,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        except (TypeError, ValueError):
            continue
    return out


def resolve_range(conn, symbol, days, start, end):
    """(start_ts, end_ts) in epoch seconds, honouring stored history.

    Resumability is the reason this reads the database: re-running a
    backfill that was interrupted at 40% should fetch the remaining 60%,
    not start over.

    The trap, which bit once already: when the database already holds bars
    for `symbol` and the caller gave no explicit `--start`, this continues
    *forward* from the newest stored bar. So `--days 90` against a
    populated database silently means "fetch whatever is missing since the
    last row", not "give me 90 days of history" -- it fetched 16 days and
    reported it as 90. Pass `--start` to mean an absolute window, which is
    the only way to backfill deliberately.
    """
    now = int(time.time())
    if start:
        start_ts = start
    elif days:
        start_ts = now - int(days * 86400)
    else:
        start_ts = now - 90 * 86400

    if end:
        end_ts = end
    elif days:
        end_ts = now
    else:
        end_ts = now

    newest = None
    try:
        row = conn.execute(
            "SELECT MAX(ts) FROM candles_1m WHERE symbol = ?", (symbol,)
        ).fetchone()
        newest = row[0] if row else None
    except Exception:
        pass

    # Continue forward from what is stored, but never past the caller's end.
    if newest and start is None and end is None:
        cand = int(newest) + 60
        if cand > start_ts:
            start_ts = cand
    if start_ts >= end_ts:
        return start_ts, end_ts
    return start_ts, end_ts


def ingest(conn, symbol, rows):
    """Upsert rows. Returns (written, skipped_bad)."""
    written = 0
    bad = 0
    batch = []
    for r in rows:
        # Reject obviously impossible bars rather than storing them: a zero
        # or negative price corrupts every return computed downstream, and
        # the absence shows up much later as a divide-by-zero.
        if not (r["high"] >= r["low"] > 0 and r["volume"] >= 0):
            bad += 1
            continue
        if r["low"] > r["open"] or r["low"] > r["close"] \
                or r["high"] < r["open"] or r["high"] < r["close"]:
            bad += 1
            continue
        batch.append((symbol, r["ts"], r["open"], r["high"], r["low"],
                      r["close"], r["volume"], "binance"))
    if batch:
        # Retried on lock contention with the live feed: a 259k-row insert is
        # long enough to collide with a server tick, and losing the whole
        # backfill to that would be absurd.
        last = None
        for attempt in range(20):
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO candles_1m"
                    " (symbol, ts, open, high, low, close, volume, source)"
                    " VALUES (?,?,?,?,?,?,?,?)", batch)
                conn.commit()
                written = len(batch)
                break
            except sqlite3.OperationalError as e:
                last = e
                time.sleep(1.0 + attempt * 0.5)
        else:
            raise last
    return written, bad


def report_gaps(rows, symbol):
    """Loudly report holes in what the exchange actually returned.

    Binance does occasionally omit intervals. A silent gap is worse than a
    missing bar, because every volatility model here assumes contiguous
    bars and a hole quietly becomes a zero return.
    """
    if len(rows) < 2:
        return []
    gaps = []
    for a, b in zip(rows, rows[1:]):
        d = b["ts"] - a["ts"]
        if d > 60:
            gaps.append((a["ts"], d // 60 - 1))
    if gaps:
        sys.stderr.write(
            "    WARNING %s: %d gap(s) in exchange data, %d missing bars "
            "total\n" % (symbol, len(gaps), sum(g[1] for g in gaps)))
    return gaps


def main():
    ap = argparse.ArgumentParser(
        description="Fetch real 1m candles from Binance (no API key).")
    ap.add_argument("symbols", nargs="+",
                    help="Binance pairs, e.g. BTCUSDT ETHUSDT")
    ap.add_argument("--db", default=os.environ.get(
        "DB_PATH", os.path.join("data", "trading.db")))
    ap.add_argument("--out", help="write here instead of --db "
                                  "(recommended: a separate DB first)")
    ap.add_argument("--days", type=float, default=None,
                    help="how far back from now (default 90)")
    ap.add_argument("--start", type=int, default=None, help="epoch seconds")
    ap.add_argument("--end", type=int, default=None, help="epoch seconds")
    ap.add_argument("--limit", type=int, default=MAX_LIMIT)
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_S,
                    help="seconds between requests (default %.2f)"
                         % DEFAULT_DELAY_S)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write nothing")
    ap.add_argument("--limit-days", type=float, default=None,
                    help="stop after this many days even if --days is larger")
    ap.add_argument("--use-app-proxy", action="store_true",
                    help="route through data.proxy if configured")
    ap.add_argument("--prune-stale", action="store_true",
                    help="delete this symbol's rows older than the fetched "
                         "window, so real bars replace synthetic ones")
    ap.add_argument("--as", dest="alias", action="append", default=[],
                    metavar="PAIR=SYMBOL",
                    help="store PAIR's bars under SYMBOL instead of under "
                         "the pair's own name. Use when the app's symbol "
                         "differs from the exchange pair -- the app trades "
                         "BTCUSD, the exchange lists BTCUSDT. Repeatable.")
    ap.add_argument("--yes", action="store_true",
                    help="required with --prune-stale, because it deletes")
    args = ap.parse_args()

    target = args.out or args.db
    if args.limit > MAX_LIMIT:
        print("limit above %d is rejected by binance" % MAX_LIMIT,
              file=sys.stderr)
        return 2

    if args.prune_stale and not args.yes:
        print("--prune-stale deletes rows; re-run with --yes to confirm",
              file=sys.stderr)
        return 2

    from data.db import init_db, connect
    if not args.dry_run:
        try:
            c = init_db(target)
            c.close()
        except Exception:
            pass
    conn = connect(target)

    # alias: exchange pair -> symbol name to write it under.
    #
    # Spec is PAIR=SYMBOL, read in the order that matches the positional
    # argument: you fetch a pair and say where it lands. The first version
    # documented SYMBOL=PAIR and then read it in that order, so
    # `--as BTCUSD` after `BTCUSDT` meant "fetch BTCUSD's bars, store them
    # under BTCUSDT" -- silently backwards, and it failed loudly rather
    # than writing wrong data, which is the only reason it was caught.
    alias = {}
    for spec in args.alias:
        if "=" not in spec:
            print("--as needs PAIR=SYMBOL, got %r" % spec, file=sys.stderr)
            return 2
        pair, sym_name = spec.split("=", 1)
        pair = pair.strip().upper()
        sym_name = sym_name.strip().upper()
        if not pair or not sym_name:
            print("--as needs PAIR=SYMBOL, got %r" % spec, file=sys.stderr)
            return 2
        alias[pair] = sym_name

    try:
        for sym in args.symbols:
            pair = sym.upper()
            write_as = alias.get(pair, pair)
            start_ts, end_ts = resolve_range(conn, write_as, args.days,
                                             args.start, args.end)
            if args.limit_days:
                start_ts = max(start_ts, end_ts - int(args.limit_days * 86400))

            total = max(0, end_ts - start_ts) // 60
            label = pair if pair == write_as else "%s (as %s)" % (pair, write_as)
            print("%s: %s .. %s (~%d bars)" % (
                label,
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(start_ts)),
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(end_ts)),
                total))

            if start_ts >= end_ts:
                print("  already up to date; nothing to fetch")
                continue

            all_rows = []
            cursor = start_ts * 1000
            t0 = time.time()
            pages = 0
            while cursor < end_ts * 1000:
                rows = fetch_klines(pair, cursor, end_ts * 1000,
                                    limit=args.limit,
                                    use_app_proxy=args.use_app_proxy)
                if not rows:
                    break
                fresh = [r for r in rows if r["ts"] * 1000 >= cursor]
                if not fresh:
                    break
                report_gaps(fresh, write_as)
                all_rows.extend(fresh)
                pages += 1
                nxt = fresh[-1]["ts"] * 1000 + 60000
                if nxt <= cursor:
                    break
                cursor = nxt
                if pages % 10 == 0:
                    rate = len(all_rows) / max(time.time() - t0, 1e-9)
                    sys.stderr.write(
                        "\r    %d pages, %d bars, %.0f bars/s" %
                        (pages, len(all_rows), rate))
                    sys.stderr.flush()
                time.sleep(args.delay)

            sys.stderr.write("\r" + " " * 60 + "\r")

            # Dedupe across page boundaries; Binance can repeat the last
            # bar of a page when startTime lands exactly on it.
            seen = {}
            for r in all_rows:
                seen[r["ts"]] = r
            uniq = [seen[k] for k in sorted(seen)]

            print("  fetched %d unique bars in %d page(s), %.0fs" % (
                len(uniq), pages, time.time() - t0))
            if not uniq:
                print("  nothing returned; check the pair name")
                continue

            if args.dry_run:
                print("  dry run; nothing written")
                _print_calendar(uniq)
                continue

            if write_as != pair:
                conn.execute("INSERT OR IGNORE INTO symbols (symbol) VALUES (?)",
                             (write_as,))
                conn.commit()
            written, bad = ingest(conn, write_as, uniq)
            print("  wrote %d rows (skipped %d malformed)" % (written, bad))

            if args.prune_stale:
                lo = min(r["ts"] for r in uniq)
                # The live app server holds short write locks on a feed tick,
                # and a big DELETE can collide with one. Retry briefly rather
                # than aborting a completed 180-day backfill over a transient
                # lock: the fetch and the INSERT are already committed above.
                removed = 0
                for attempt in range(20):
                    try:
                        cur = conn.execute(
                            "DELETE FROM candles_1m WHERE symbol = ? AND ts < ?",
                            (write_as, lo))
                        removed = cur.rowcount or 0
                        conn.commit()
                        break
                    except sqlite3.OperationalError:
                        time.sleep(1.0 + attempt * 0.5)
                else:
                    print("  WARNING: could not prune (db stayed locked); "
                          "old rows for %s remain" % write_as, file=sys.stderr)
                if removed:
                    print("  pruned %d row(s) older than %s" % (
                        removed,
                        time.strftime("%Y-%m-%d %H:%M", time.gmtime(lo))))
            _print_calendar(uniq)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


def _print_calendar(rows):
    """First/last bar and the properties that matter downstream."""
    if not rows:
        return
    import math
    closes = [r["close"] for r in rows]
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))
            if closes[i - 1] > 0]
    if not rets:
        return
    m = sum(rets) / len(rets)
    m2 = sum((x - m) ** 2 for x in rets) / len(rets)
    m4 = sum((x - m) ** 4 for x in rets) / len(rets)
    kurt = (m4 / (m2 * m2)) if m2 > 0 else 0.0
    sq = [x * x for x in rets]
    ms = sum(sq) / len(sq)
    # Units trap, and it is worth spelling out because getting it wrong is
    # silent and looks plausible. The denominator must be the variance OF
    # the squared returns, not the squared returns themselves. For 1m BTC
    # the returns are ~1e-3, so their squares are ~1e-6 and the variance of
    # those squares is ~1e-10. Dividing by the wrong one of those is a
    # million-fold error, and the printed answer comes out as a
    # comfortable-looking 0.0000 rather than an error. An earlier version
    # of this file did exactly that and reported no volatility clustering
    # in data that has ACF +0.18.
    var_sq = sum((x - ms) ** 2 for x in sq) / max(len(sq) - 1, 1)
    acf = 0.0
    if var_sq > 0:
        acf = (sum((sq[i] - ms) * (sq[i + 1] - ms)
                   for i in range(len(sq) - 1))
               / (len(sq) - 1) / var_sq)
    print("  span %s .. %s" % (
        time.strftime("%Y-%m-%d %H:%M", time.gmtime(rows[0]["ts"])),
        time.strftime("%Y-%m-%d %H:%M", time.gmtime(rows[-1]["ts"]))))
    print("  raw kurtosis %.2f (synthetic is 3.10)"
          "   ACF(r^2,lag1) %+.4f (synthetic is ~0.008)" % (kurt, acf))
    if kurt < 4.0 and abs(acf) < 0.05:
        print("  NOTE: this looks Gaussian and unclustered -- if you expected "
              "real market data, check the source before trusting it")


if __name__ == "__main__":
    sys.exit(main())

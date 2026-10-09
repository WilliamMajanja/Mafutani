"""Ingest candles CSV. Stdlib only.

Usage: python3 scripts/ingest_csv.py FILE.csv --symbol BTCUSD [--db PATH]

CSV columns (header required, flexible names):
  ts,o,h,l,c,v
Accepted aliases: time/t, open, high, low, close, volume.
ts may be epoch seconds or ISO-8601.
Upserts into candles_1m (INSERT OR REPLACE by symbol+ts).
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse_ts(v):
    v = str(v).strip()
    try:
        return int(float(v))
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(v, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        raise ValueError("unparseable ts: {!r}".format(v))


def _pick(row, *names):
    low = {k.lower(): k for k in row.keys()}
    for n in names:
        if n.lower() in low:
            return row[low[n.lower()]]
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvfile")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--db", default=os.environ.get("DB_PATH", os.path.join("data", "trading.db")))
    args = ap.parse_args()

    from data.db import init_db, connect
    try:
        from data.normalize import normalize_candle
    except ImportError:
        normalize_candle = None

    c = init_db(args.db)
    try:
        c.close()
    except Exception:
        pass
    conn = connect(args.db)
    conn.execute("INSERT OR IGNORE INTO symbols (symbol) VALUES (?)", (args.symbol,))

    n = 0
    batch = []
    with open(args.csvfile, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            print("empty csv", file=sys.stderr)
            sys.exit(1)
        for row in reader:
            try:
                raw = {
                    "symbol": args.symbol,
                    "ts": _parse_ts(_pick(row, "ts", "time", "t", "timestamp", "date", "datetime")),
                    "open": float(_pick(row, "o", "open") or 0),
                    "high": float(_pick(row, "h", "high") or 0),
                    "low": float(_pick(row, "l", "low") or 0),
                    "close": float(_pick(row, "c", "close") or 0),
                    "volume": float(_pick(row, "v", "volume", "vol") or 0),
                }
            except (TypeError, ValueError) as e:
                print("skip row: {}".format(e), file=sys.stderr)
                continue
            if normalize_candle is not None:
                try:
                    raw = normalize_candle(raw)
                except Exception:
                    pass
            batch.append((args.symbol, int(raw["ts"]), raw["open"], raw["high"],
                          raw["low"], raw["close"], raw["volume"]))
            if len(batch) >= 2000:
                conn.executemany(
                    "INSERT OR REPLACE INTO candles_1m (symbol, ts, open, high, low, close, volume)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
                n += len(batch)
                batch = []
    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO candles_1m (symbol, ts, open, high, low, close, volume)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
        n += len(batch)
    conn.commit()
    # Candles just changed, so the materialised volume summary is now wrong.
    # Flagging it stale makes the next screener read rebuild it; without
    # this the screener would keep ranking on pre-import totals.
    try:
        conn.execute("UPDATE symbol_volume SET stale = 1")
        conn.commit()
    except Exception:
        pass
    conn.close()
    print("ingested {} candles for {}".format(n, args.symbol))
    print("volume summary flagged stale; next screener read will rebuild it")


if __name__ == "__main__":
    main()

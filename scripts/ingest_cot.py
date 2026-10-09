"""Ingest CFTC Commitments of Traders. Stdlib only.

Usage:
  python3 scripts/ingest_cot.py weekly                 # all 7 current reports
  python3 scripts/ingest_cot.py weekly --report disagg_comb
  python3 scripts/ingest_cot.py history --report disagg_comb --from 2019
  python3 scripts/ingest_cot.py summary

The weekly pull is the one you run on Fridays. CFTC publishes at 15:30 ET
for the preceding Tuesday, so a Friday evening run has that week's report
and nothing newer exists.

`history` is idempotent: rows upsert on
(report, contract_code, report_date, category), so re-running a year
costs bandwidth and time but corrupts nothing. Use --cache to keep the raw
downloads and replay them offline; the same is true for weekly, which is
worth doing because today's file does not change.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import cot as C            # noqa: E402
from data import cotstore as S       # noqa: E402

CACHE_DIR = os.environ.get("COT_CACHE_DIR", os.path.join("data", "cotcache"))


def _connect(path):
    from data.db import init_db
    return init_db(path)


def cmd_weekly(args):
    conn = _connect(args.db)
    reports = [args.report] if args.report else list(C.REPORTS.keys())
    bad = [r for r in reports if r not in C.REPORTS]
    if bad:
        print("unknown report(s): %s" % ", ".join(bad))
        print("known: %s" % ", ".join(sorted(C.REPORTS)))
        return 2
    print("fetching %d current-week report(s)\n" % len(reports))
    failures = 0
    for r in reports:
        stats = {}
        res = S.ingest_weekly(conn, r, cache_dir=args.cache_dir or None,
                              max_age=args.max_age)
        line = "%-13s %-6s %7s rows  date=%s" % (
            r, "ok" if res["ok"] else "FAIL", res["written"],
            res["report_date"] or "-")
        if res["error"]:
            line += "\n              %s" % res["error"]
            failures += 1
        print(line)
    conn.close()
    print("\n%s" % ("one or more reports failed" if failures else "all ok"))
    return 1 if failures else 0


def cmd_history(args):
    conn = _connect(args.db)
    if args.report not in C.REPORTS:
        print("unknown report: %s" % args.report)
        print("known: %s" % ", ".join(sorted(C.REPORTS)))
        return 2
    lo = args.from_year or C.FIRST_YEAR[args.report]
    hi = args.to_year or time.gmtime().tm_year
    if lo < C.FIRST_YEAR[args.report]:
        print("%s starts in %d" % (args.report, C.FIRST_YEAR[args.report]))
        lo = C.FIRST_YEAR[args.report]
    total = 0
    failures = []
    print("%s %d..%d\n" % (args.report, lo, hi))
    for y in range(lo, hi + 1):
        res = S.ingest_historical(conn, args.report, y,
                                  cache_dir=args.cache_dir or None)
        total += res["written"]
        status = "ok" if res["ok"] else "FAIL"
        extra = ""
        if res["error"]:
            extra = "  %s" % res["error"][:70]
            failures.append(y)
        print("  %d  %-4s %8s rows%s" % (y, status, res["written"], extra))
    conn.close()
    print("\n%d rows written" % total)
    if failures:
        print("failed years: %s" % ", ".join(str(y) for y in failures))
    return 1 if failures else 0


def cmd_summary(args):
    conn = _connect(args.db)
    print("stored COT data\n")
    rows = S.table_summary(conn, args.report)
    if not rows:
        print("  (nothing stored -- run `ingest_cot.py weekly` first)")
        conn.close()
        return 0
    print("  %-14s %-9s %10s %10s  %s" % ("report", "family", "rows",
                                          "contracts", "newest"))
    for r in rows:
        print("  %-14s %-9s %10d %10d  %s" % (
            r["report"], r["family"], r["rows"], r["contracts"],
            r["newest"] or "-"))
    print("\nrecent fetches")
    for f in S.fetch_history(conn, limit=args.report and 3 or 12):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["fetched_at"]))
        print("  %-14s %-11s %-6s %7s rows  %s%s" % (
            f["report"], f["kind"], "ok" if f["ok"] else "FAIL",
            f["rows_ok"], ts, ("  " + f["error"][:50]) if f["error"] else ""))
    conn.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("weekly", "history", "summary"))
    ap.add_argument("--db", default=os.environ.get(
        "DB_PATH", os.path.join("data", "trading.db")))
    ap.add_argument("--report", default=None, choices=sorted(C.REPORTS))
    ap.add_argument("--cache-dir", dest="cache_dir", default=CACHE_DIR)
    ap.add_argument("--no-cache", dest="cache_dir", action="store_const",
                    const=None)
    ap.add_argument("--max-age", dest="max_age", type=int, default=3600,
                    help="reuse a cached weekly download younger than this")
    ap.add_argument("--from", dest="from_year", type=int, default=None)
    ap.add_argument("--to", dest="to_year", type=int, default=None)
    args = ap.parse_args()
    return {"weekly": cmd_weekly, "history": cmd_history,
            "summary": cmd_summary}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

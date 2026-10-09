"""COT persistence and queries. Stdlib only.

Everything that touches SQLite lives here so `data.cot` stays a pure
fetch/parse module (import-safe, no DB) and `engines.cot` stays a pure
analytics module (import-safe, no I/O). Those two boundaries are what
make the arithmetic testable without a database and the fetching testable
without a network.

One rule throughout: **a failed fetch must not damage stored data.** Every
write path upserts by natural key, and a fetch that fails records the
failure in `cot_fetches` and returns. The positions already in the table
are still the CFTC's most recent published numbers; deleting them because
today's download timed out would turn a transient network problem into
lost history.
"""

import time

from data import cot as _cot

# Which categories carry the most information per family, for a compact
# read. The full set stays queryable; this is only the default view.
PRIMARY_CATEGORIES = {
    "disagg": ["m_money", "prod_merc", "swap"],
    "tff": ["lev_money", "asset_mgr", "dealer"],
    "legacy": ["noncomm", "comm"],
    "cit": ["cit", "noncomm_no_cit", "comm_no_cit"],
}

DEFAULT_REPORT = "disagg_comb"


# ------------------------------------------------------------------ write
_COLS = ("report", "family", "combined", "market", "market_code",
         "region_code", "commodity_code", "contract_code", "report_date",
         "as_of_yymmdd", "open_interest", "category", "long_pos",
         "short_pos", "spread_pos", "has_spread")

_INSERT = ("INSERT INTO cot_positions ({}, fetched_at) VALUES ({}, ?)"
           " ON CONFLICT(report, contract_code, report_date, category)"
           " DO UPDATE SET market=excluded.market,"
           " market_code=excluded.market_code,"
           " region_code=excluded.region_code,"
           " commodity_code=excluded.commodity_code,"
           " as_of_yymmdd=excluded.as_of_yymmdd,"
           " open_interest=excluded.open_interest,"
           " long_pos=excluded.long_pos, short_pos=excluded.short_pos,"
           " spread_pos=excluded.spread_pos,"
           " has_spread=excluded.has_spread,"
           " fetched_at=excluded.fetched_at"
           ).format(",".join(_COLS), ",".join("?" * len(_COLS)))


def store(conn, rows, report=None, kind="weekly", url="", error="",
          year=0, fetched_at=None):
    """Upsert parsed rows and record the fetch outcome.

    Returns {"ok", "written", "report_date", "fetch_id"}. `ok` is False
    when `rows` is empty -- an empty parse is a failure even when the HTTP
    request succeeded, because a 200 carrying an unparseable body is not
    data.
    """
    now = int(fetched_at if fetched_at is not None else time.time())
    rows = list(rows or [])
    rep = report or (rows[0].get("report") if rows else "")
    written = 0
    latest = ""
    if rows:
        payload = [tuple(int(r.get(c) or 0) if c in (
            "combined", "open_interest", "long_pos", "short_pos",
            "spread_pos", "has_spread") else (r.get(c) or "")
            for c in _COLS) + (now,) for r in rows]
        try:
            conn.executemany(_INSERT, payload)
            written = len(payload)
        except Exception as e:
            error = error or "store failed: %s" % (e,)
            written = 0
        latest = _cot.latest_date(rows) or ""
    cur = conn.execute(
        "INSERT INTO cot_fetches (report, kind, year, url, rows_ok,"
        " report_date, ok, error, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (rep, kind, int(year or 0), url or "", written, latest,
         1 if written else 0, error or "", now))
    conn.commit()
    return {"ok": bool(written), "written": written, "report_date": latest,
            "fetch_id": cur.lastrowid, "error": error or None}


def ingest_weekly(conn, report=DEFAULT_REPORT, cache_dir=None,
                  max_age=3600, year=0):
    """Fetch one current-week report and store it. Never raises."""
    try:
        res = _cot.fetch_parse(report, cache_dir=cache_dir,
                               max_age=max_age)
        meta = res.get("meta") or {}
        return store(conn, res["rows"], report=report, kind="weekly",
                     url=meta.get("url", ""), error=res.get("error") or "")
    except Exception as e:
        return store(conn, [], report=report, kind="weekly",
                     error="%s: %s" % (type(e).__name__, e))


def ingest_historical(conn, report, year, cache_dir=None):
    """Fetch one annual archive and store it. Never raises."""
    try:
        res = _cot.fetch_historical_parse(report, year, cache_dir=cache_dir)
        meta = res.get("meta") or {}
        return store(conn, res["rows"], report=report, kind="historical",
                     url=meta.get("url", ""), error=res.get("error") or "",
                     year=year)
    except Exception as e:
        return store(conn, [], report=report, kind="historical", year=year,
                     error="%s: %s" % (type(e).__name__, e))


def ingest_all_weekly(conn, reports=None, cache_dir=None, max_age=3600):
    """Every current-week file. One report failing does not stop the rest."""
    reports = list(reports or _cot.REPORTS.keys())
    out = {}
    for r in reports:
        out[r] = ingest_weekly(conn, r, cache_dir=cache_dir,
                               max_age=max_age)
    return out


# ------------------------------------------------------------------- read
def _rows_to_dicts(rows):
    return [{k: r[k] for k in r.keys()} for r in rows]


def latest_report_date(conn, report=DEFAULT_REPORT):
    r = conn.execute(
        "SELECT MAX(report_date) d FROM cot_positions WHERE report = ?",
        (report,)).fetchone()
    return (r["d"] if r else None) or None


def list_markets(conn, report=DEFAULT_REPORT, limit=500):
    """Distinct contracts in a report, newest-first by market name."""
    rows = conn.execute(
        "SELECT DISTINCT contract_code, market, market_code, commodity_code"
        " FROM cot_positions WHERE report = ? ORDER BY market LIMIT ?",
        (report, int(limit))).fetchall()
    return _rows_to_dicts(rows)


def search_markets(conn, query, report=DEFAULT_REPORT, limit=50):
    q = "%" + str(query or "").strip().upper() + "%"
    rows = conn.execute(
        "SELECT DISTINCT contract_code, market, market_code, commodity_code"
        " FROM cot_positions WHERE report = ? AND"
        " (UPPER(market) LIKE ? OR UPPER(market_code) LIKE ?"
        "  OR UPPER(contract_code) LIKE ?)"
        " ORDER BY market LIMIT ?",
        (report, q, q, q, int(limit))).fetchall()
    return _rows_to_dicts(rows)


def positions(conn, report=DEFAULT_REPORT, contract_code=None,
              category=None, limit=4000, since=None):
    """Raw stored rows for one contract (and/or category), oldest first.

    Default limit 4000 is ~77 years of weekly observations for one
    contract; in practice a contract's life is a few hundred weeks. Capped
    anyway so a missing contract_code cannot pull the whole table.
    """
    sql = "SELECT * FROM cot_positions WHERE report = ?"
    args = [report]
    if contract_code:
        sql += " AND contract_code = ?"
        args.append(str(contract_code))
    if category:
        sql += " AND category = ?"
        args.append(str(category))
    if since:
        sql += " AND report_date >= ?"
        args.append(str(since))
    sql += " ORDER BY report_date ASC LIMIT ?"
    args.append(int(limit))
    return _rows_to_dicts(conn.execute(sql, args).fetchall())


def contract_meta(conn, contract_code, report=DEFAULT_REPORT):
    r = conn.execute(
        "SELECT market, market_code, commodity_code, region_code"
        " FROM cot_positions WHERE report = ? AND contract_code = ?"
        " LIMIT 1", (report, str(contract_code))).fetchone()
    return {k: r[k] for k in r.keys()} if r else None


def latest_positions(conn, report=DEFAULT_REPORT, contract_code=None,
                     categories=None, date=None):
    """One date's rows for a contract. Defaults to the newest stored date."""
    if not date:
        if contract_code:
            r = conn.execute(
                "SELECT MAX(report_date) d FROM cot_positions"
                " WHERE report = ? AND contract_code = ?",
                (report, str(contract_code))).fetchone()
        else:
            r = conn.execute(
                "SELECT MAX(report_date) d FROM cot_positions"
                " WHERE report = ?", (report,)).fetchone()
        date = (r["d"] if r else None) or None
    if not date:
        return []
    sql = "SELECT * FROM cot_positions WHERE report = ? AND report_date = ?"
    args = [report, date]
    if contract_code:
        sql += " AND contract_code = ?"
        args.append(str(contract_code))
    rows = _rows_to_dicts(conn.execute(sql, args).fetchall())
    if categories:
        keep = set(categories)
        rows = [r for r in rows if r["category"] in keep]
    return rows


def fetch_history(conn, report=DEFAULT_REPORT, limit=10):
    """Recent fetch outcomes, newest first. Provenance, not positions."""
    rows = conn.execute(
        "SELECT report, kind, year, url, rows_ok, report_date, ok, error,"
        " fetched_at FROM cot_fetches ORDER BY fetched_at DESC, id DESC"
        " LIMIT ?", (int(limit),)).fetchall()
    return _rows_to_dicts(rows)


def last_success(conn, report=DEFAULT_REPORT):
    r = conn.execute(
        "SELECT report_date, fetched_at FROM cot_fetches"
        " WHERE report = ? AND ok = 1 ORDER BY fetched_at DESC LIMIT 1",
        (report,)).fetchone()
    return {k: r[k] for k in r.keys()} if r else None


def table_summary(conn, report=None):
    """Row counts and newest date per report. Cheap; safe on an empty table."""
    sql = ("SELECT report, family, COUNT(*) rows, COUNT(DISTINCT contract_code)"
           " contracts, MAX(report_date) newest FROM cot_positions")
    args = []
    if report:
        sql += " WHERE report = ?"
        args.append(report)
    sql += " GROUP BY report, family ORDER BY report"
    return _rows_to_dicts(conn.execute(sql, args).fetchall())


# ------------------------------------------------------------- analytics
def analyse_contract(conn, contract_code, report=DEFAULT_REPORT,
                     weeks=None, categories=None):
    """Full COT read for one contract: per-category plus a consensus.

    `weeks` defaults to the Williams three-year window. The window is
    echoed in the result so a caller can tell a 2-year percentile from a
    5-year one.
    """
    from engines import cot as C
    if weeks is None:
        weeks = C.DEFAULT_WEEKS
    meta = contract_meta(conn, contract_code, report) or {}
    fam = meta.get("family") or _cot.REPORTS.get(report, {}).get("family", "")
    all_rows = positions(conn, report=report, contract_code=contract_code)
    cats = categories or PRIMARY_CATEGORIES.get(fam) or []
    per = []
    for c in cats:
        rows = [r for r in all_rows if r["category"] == c]
        if not rows:
            continue
        a = C.analyse(rows, c, weeks)
        a["pct_of_open_interest"] = C.pct_of_open_interest(
            rows[-1]["long_pos"], rows[-1]["short_pos"],
            rows[-1]["open_interest"])
        a["cot_ratio"] = C.cot_ratio(rows[-1]["long_pos"], rows[-1]["short_pos"])
        a["crowding"] = C.crowding(rows[-1]["long_pos"], rows[-1]["short_pos"],
                                   rows[-1]["open_interest"], series=C.net_series(rows, c),
                                   weeks=weeks)
        per.append(a)
    latest = latest_report_date(conn, report) if not contract_code else \
        (all_rows[-1]["report_date"] if all_rows else None)
    return {
        "contract_code": contract_code,
        "report": report,
        "family": fam,
        "market": meta.get("market", ""),
        "market_code": meta.get("market_code", ""),
        "window_weeks": int(weeks),
        "window_years": round(weeks / 52.0, 2),
        "report_date": per[0]["report_date"] if per else latest,
        "staleness": C.staleness(per[0]["report_date"] if per else latest),
        "open_interest": all_rows[-1]["open_interest"] if all_rows else None,
        "categories": per,
        "consensus": C.consensus(per),
        "basis": "futures+options combined" if meta.get("combined") else "futures only",
    }


def contract_history(conn, contract_code, report=DEFAULT_REPORT,
                     category=None, limit=200):
    """Oldest-first history for charting. Compact: the numbers, not the row."""
    rows = positions(conn, report=report, contract_code=contract_code,
                     category=category)
    out = []
    for r in rows[-int(limit):]:
        out.append({
            "date": r["report_date"],
            "long": r["long_pos"],
            "short": r["short_pos"],
            "spread": r["spread_pos"],
            "oi": r["open_interest"],
            "net": r["long_pos"] - r["short_pos"],
        })
    return out

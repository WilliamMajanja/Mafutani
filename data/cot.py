"""CFTC Commitments of Traders: fetch, parse, normalise. Stdlib only.

No key, no account, no scraping of the HTML report tables -- the CFTC
publishes plain-text current-week files and annual zipped archives, and
those are what this reads. `urllib.request`, `csv`, `io`, `zipfile`.

What COT is
-----------
A weekly snapshot of *positions held*, not of prices traded. Every Tuesday,
firms with 20+ large traders report their open interest split by who owns
it; the CFTC publishes it the following Friday at 15:30 ET. Two
consequences shape everything below:

* **It is a positioning measure, not a price series.** It cannot be joined
  to `candles_1m` as if it were a tape and cannot be backtested as one. It
  is a slow, weekly, lagged overlay -- an input to a thesis about *who is
  on the other side*, never a substitute for what a venue quotes.
* **It is a week stale on arrival and stays that way.** The report dated
  Tuesday T is not published until Friday T+3. Any consumer has to know
  that, or it will build a backtest on data it could not have had.

The four report families, and why all four are here
---------------------------------------------------
The trader classifications are not comparable across families, so the
family is part of the primary key. Mixing them silently is the classic way
to produce a confident wrong number:

============  ==========================================  ==================
family        what it splits open interest into           history from
============  ==========================================  ==================
``disagg``    Producer/Merchant, Swap Dealers,            Jun 2006
              Managed Money, Other Reportables
``tff``       Dealer/Intermediary, Asset Manager,         Jun 2006
              Leveraged Funds, Other Reportables
``legacy``    Non-Commercial, Commercial                  Jan 1986
``cit``       Non-Commercial/Commercial *excluding* index  Jan 2006
              traders, plus the index traders themselves
============  ==========================================  ==================

For a physical commodity, ``disagg`` is the modern read and ``legacy`` is
the long history. For financials, ``tff`` supersedes ``legacy`` because
"commercial" for an equity index means the market makers, which tells you
nothing about positioning.

Column layouts are pinned by index, not by header name
-----------------------------------------------------
The weekly files have **no header row** -- the first line is data. The
annual zips do have one, and its names differ between families
(``Swap__Positions_Short_All`` with a double underscore in one place,
``Change_Comm_Short_All NoCIT`` with a trailing space in another). Reading
the annual header and applying it to the weekly file would be a silent
misalignment the moment CFTC renamed a column.

So each family carries an explicit index map, and every parsed row is
validated: field 2 must be ``YYYY-MM-DD`` and field 3 a 6-digit contract
code. A file whose layout shifted raises rather than writing
``Prod_Merc`` short positions into the ``Swap`` column.

Where it refuses
----------------
* Fewer than two positions on a side cannot produce a net or a COT index;
  both return ``None`` rather than 0.
* A three-year percentile needs the three years. With less history the
  window shrinks and the result says which window it used, rather than
  quietly reporting a percentile of whatever it happened to have.
* ``fetch_*`` never raises into a caller that is trying to serve a page.
  It returns rows plus an explicit error, and a failed refresh still
  leaves the previously stored data intact and marked stale.
"""

import csv
import io
import ssl
import time
import urllib.error
import urllib.request
import zipfile

BASE = "https://www.cftc.gov"
UA = {"User-Agent": "mafutani-cta/1.1 (personal-research; contact: local)"}
TIMEOUT = 45

# CFTC publishes Friday 15:30 ET for the preceding Tuesday. Records how
# stale a row is, because a COT number read on Monday describes a market
# as it stood nine days earlier.
WEEKLY = "weekly"
HISTORICAL = "historical"

# --------------------------------------------------------------- layouts
#
# (open_interest_idx, {category: (long_idx, short_idx, spread_idx_or_None)})
#
# All on the "_All" (futures + options combined) basis, which is the
# economically meaningful one: the combined file is what a trader holding
# an option and a future is actually exposed to.
#
# Indices verified against the 2025 and 2026 annual archives for every
# family; tests/test_cot.py pins them against live-shaped fixtures.

# Canonical categories, one vocabulary across all four families. The
# legacy family has no spread column for its commercial side and the
# disaggregated has none for producer/merchant; those are None, not 0.
LAYOUTS = {
    "disagg": {
        "oi": 7,
        "categories": {
            "prod_merc": (8, 9, None),
            "swap": (10, 11, 12),
            "m_money": (13, 14, 15),
            "other_rept": (16, 17, 18),
            "total_rept": (19, 20, None),
            "nonrept": (21, 22, None),
        },
    },
    "tff": {
        "oi": 7,
        "categories": {
            "dealer": (8, 9, 10),
            "asset_mgr": (11, 12, 13),
            "lev_money": (14, 15, 16),
            "other_rept": (17, 18, 19),
            "total_rept": (20, 21, None),
            "nonrept": (22, 23, None),
        },
    },
    "legacy": {
        "oi": 7,
        "categories": {
            "noncomm": (8, 9, 10),
            "comm": (11, 12, None),
            "total_rept": (13, 14, None),
            "nonrept": (15, 16, None),
        },
    },
    "cit": {
        "oi": 7,
        "categories": {
            "noncomm_no_cit": (8, 9, 10),
            "comm_no_cit": (11, 12, None),
            "total_rept": (13, 14, None),
            "nonrept": (15, 16, None),
            "cit": (17, 18, None),
        },
    },
}

# The seven current-week files CFTC publishes. `family` selects the
# layout; `combined` marks the futures+options basis.
REPORTS = {
    "disagg_fut": {"family": "disagg", "combined": False,
                   "weekly": "/dea/newcot/f_disagg.txt"},
    "disagg_comb": {"family": "disagg", "combined": True,
                    "weekly": "/dea/newcot/c_disagg.txt"},
    "tff_fut": {"family": "tff", "combined": False,
                "weekly": "/dea/newcot/FinFutWk.txt"},
    "tff_comb": {"family": "tff", "combined": True,
                 "weekly": "/dea/newcot/FinComWk.txt"},
    "legacy_fut": {"family": "legacy", "combined": False,
                   "weekly": "/dea/newcot/deafut.txt"},
    "legacy_comb": {"family": "legacy", "combined": True,
                    "weekly": "/dea/newcot/deacom.txt"},
    "cit": {"family": "cit", "combined": True,
            "weekly": "/dea/newcot/deacit.txt"},
}

# Annual archive filename per family, for the Historical Compressed page.
HIST_TEMPLATES = {
    "disagg_fut": "fut_disagg_txt_{year}.zip",
    "disagg_comb": "com_disagg_txt_{year}.zip",
    "tff_fut": "fut_fin_txt_{year}.zip",
    "tff_comb": "com_fin_txt_{year}.zip",
    "legacy_fut": "deacot{year}.zip",
    "legacy_comb": "deahistfo{year}.zip",
    "cit": "dea_cit_txt_{year}.zip",
}

# Earliest year each family exists. Asking for 1990 disaggregated data is
# a 404; asking for it by accident should be an explicit refusal instead.
FIRST_YEAR = {
    "disagg_fut": 2010, "disagg_comb": 2010,
    "tff_fut": 2010, "tff_comb": 2010,
    "legacy_fut": 1986, "legacy_comb": 1995,
    "cit": 2006,
}

# A contract is only reported while it has 20+ large traders, so markets
# appear and disappear between years. Upserting on
# (report, contract, date, category) is what makes that harmless.


class CotError(Exception):
    """Raised by the strict parse helpers. Never escapes fetch_*()."""


# ----------------------------------------------------------------- fetch
def _http(url):
    req = urllib.request.Request(url, headers=UA)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return r.read()


def _cache_path(cache_dir, report):
    import os
    return os.path.join(cache_dir, "cot_%s.txt" % report)


def fetch_weekly(report, cache_dir=None, max_age=3600):
    """Fetch one current-week file. Returns (text, meta).

    `cache_dir` writes the raw bytes to disk so a weekly pull can be
    replayed offline -- COT is a once-a-week release, and re-downloading
    470KB every call to look at the same Tuesday is both rude and slow.
    Cached bytes are reused while younger than `max_age`.
    """
    import os
    spec = REPORTS.get(report)
    if spec is None:
        raise CotError("unknown report: %r" % (report,))
    url = BASE + spec["weekly"]
    now = int(time.time())

    if cache_dir:
        path = _cache_path(cache_dir, report)
        try:
            age = now - int(os.path.getmtime(path))
            if age < max_age:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read(), {"url": url, "cached": True,
                                      "fetched_at": now - age, "bytes":
                                      os.path.getsize(path)}
        except Exception:
            pass

    raw = _http(url)
    text = raw.decode("utf-8", "replace")
    meta = {"url": url, "cached": False, "fetched_at": now,
            "bytes": len(raw), "error": None}
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(_cache_path(cache_dir, report), "w",
                      encoding="utf-8") as f:
                f.write(text)
        except Exception:
            meta["error"] = "cache write failed (fetch itself succeeded)"
    return text, meta


def fetch_historical(report, year, cache_dir=None, max_age=86400 * 30):
    """Fetch one annual archive zip. Returns (text, meta)."""
    import os
    if report not in HIST_TEMPLATES:
        raise CotError("unknown report: %r" % (report,))
    year = int(year)
    if year < FIRST_YEAR.get(report, 1986) or year > time.gmtime().tm_year:
        raise CotError("%s has no data for %d" % (report, year))
    fname = HIST_TEMPLATES[report].format(year=year)
    url = "%s/files/dea/history/%s" % (BASE, fname)
    now = int(time.time())

    if cache_dir:
        path = os.path.join(cache_dir, "cot_%s_%d.zip" % (report, year))
        try:
            age = now - int(os.path.getmtime(path))
            if age < max_age:
                with open(path, "rb") as f:
                    return _unzip_text(f.read()), {
                        "url": url, "cached": True, "fetched_at": now - age,
                        "bytes": os.path.getsize(path), "year": year}
        except Exception:
            pass

    raw = _http(url)
    meta = {"url": url, "cached": False, "fetched_at": now,
            "bytes": len(raw), "year": year, "error": None}
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir,
                                   "cot_%s_%d.zip" % (report, year)),
                      "wb") as f:
                f.write(raw)
        except Exception:
            meta["error"] = "cache write failed (fetch itself succeeded)"
    return _unzip_text(raw), meta


def _unzip_text(raw):
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = [n for n in zf.namelist() if n.lower().endswith((".txt", ".csv"))]
    if not names:
        raise CotError("no text member in archive: %r" % (zf.namelist(),))
    # Archives hold exactly one member; if that ever changes, prefer the
    # largest rather than silently reading whichever sorts first.
    name = max(names, key=lambda n: zf.getinfo(n).file_size)
    return zf.read(name).decode("utf-8", "replace")


# ----------------------------------------------------------------- parse
def _int(v, default=None):
    """CFTC pads numbers with spaces and uses '' for absent."""
    s = str(v or "").strip().replace(",", "")
    if not s or s in ("-", "X", "N/A", "."):
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _rows(text):
    """Yield non-empty CSV rows."""
    for r in csv.reader(io.StringIO(text)):
        if r and any(str(c).strip() for c in r):
            yield r


def parse(text, report, stats=None):
    """Parse a COT file into normalised rows.

    Returns a list of dicts, one per (contract, category, report_date).
    Raises `CotError` on a layout it does not recognise rather than
    writing shifted numbers -- see the module docstring on why the
    indices are pinned. `stats`, if given, is filled with kept/skipped
    counts.
    """
    spec = REPORTS.get(report)
    if spec is None:
        raise CotError("unknown report: %r" % (report,))
    family = spec["family"]
    layout = LAYOUTS[family]
    width = layout["oi"] + 1

    raw = list(_rows(text))
    if not raw:
        raise CotError("empty %s document" % report)

    # Weekly files have no header; annual archives do.
    #
    # The test cannot be "field 2 is not a valid date": a weekly *data*
    # row's field 2 is exactly "2026-09-29", so matching on that alone
    # would swallow the first contract of every weekly file. And testing
    # only for "not a date" is just as bad in the other direction -- a
    # genuinely corrupt first row would be consumed as a header and the
    # file would parse to nothing without complaint.
    #
    # So: a header is a field 2 that is not a date *and* looks like a
    # column name. CFTC headers are "Report_Date_as_YYYY-MM-DD" and
    # "As of Date in Form YYYY-MM-DD" -- both carry an underscore or a
    # space. A corrupt value ("garbage", "20260929") carries neither and
    # falls through to the strict per-row check below, which raises.
    first = [str(c).strip() for c in raw[0]]
    has_header = False
    if len(first) > 3 and not _valid_date(first[2]):
        f2 = first[2]
        has_header = ("_" in f2) or (" " in f2)
    body = raw[1:] if has_header else raw

    out = []
    skipped = 0
    for row in body:
        if len(row) < width:
            # A short line is a truncated write, not a valid record.
            continue
        report_date = str(row[2]).strip()
        if not _valid_date(report_date):
            raise CotError(
                "unexpected layout: field 2 is %r, not YYYY-MM-DD. The CFTC "
                "column order changed and the pinned indices for %r are no "
                "longer valid." % (report_date, report))
        contract = str(row[3]).strip()
        # Codes are alphanumeric (ZB9105 is a real 2018 bond contract), so
        # this is a sanity check, not a layout guard -- one odd contract
        # must not cost the whole file. The date check above is what
        # actually detects a column shift, and that stays fatal.
        if not contract or not contract.isalnum():
            skipped += 1
            continue

        oi = _int(row[layout["oi"]], 0) or 0
        for cat, (li, si, spi) in layout["categories"].items():
            lng = _int(row[li]) if li < len(row) else None
            sht = _int(row[si]) if si < len(row) else None
            spr = _int(row[spi]) if (spi is not None and spi < len(row)) else None
            if lng is None and sht is None:
                continue
            out.append({
                "report": report,
                "family": family,
                "combined": 1 if spec["combined"] else 0,
                "market": str(row[0]).strip(),
                "market_code": str(row[4]).strip() if len(row) > 4 else "",
                "region_code": str(row[5]).strip() if len(row) > 5 else "",
                "commodity_code": str(row[6]).strip() if len(row) > 6 else "",
                "contract_code": contract,
                "report_date": report_date,
                "as_of_yymmdd": str(row[1]).strip(),
                "open_interest": oi,
                "category": cat,
                "long_pos": lng if lng is not None else 0,
                "short_pos": sht if sht is not None else 0,
                "spread_pos": spr if spr is not None else 0,
                "has_spread": 1 if spr is not None else 0,
            })
    if isinstance(stats, dict):
        # Surfaced rather than silent. A whole file is not being dropped,
        # but a caller that wants to know N records were malformed should
        # not have to diff two parses to find out.
        stats["skipped"] = skipped
        stats["kept"] = len(out)
    return out

def _valid_date(s):
    parts = str(s).split("-")
    if len(parts) != 3:
        return False
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return False
    return 1900 < y < 2200 and 1 <= m <= 12 and 1 <= d <= 31


def fetch_parse(report, cache_dir=None, max_age=3600, stats=None):
    """Fetch + parse one current-week report.

    Returns {"ok", "report", "rows", "meta", "error"}. `error` is a
    string, not an exception: the caller is usually serving a page and
    needs to say "CFTC unreachable" rather than 500. A parse failure and
    a network failure are reported the same way on purpose -- from the
    caller's side they are the same fact, which is that today's data is
    unavailable.
    """
    try:
        text, meta = fetch_weekly(report, cache_dir=cache_dir,
                                  max_age=max_age)
        rows = parse(text, report, stats=stats)
        meta.update({"ok": True, "report": report, "rows": len(rows)})
        return {"ok": True, "report": report, "rows": rows, "meta": meta,
                "error": None}
    except CotError as e:
        return {"ok": False, "report": report, "rows": [], "meta": {},
                "error": str(e)}
    except urllib.error.URLError as e:
        return {"ok": False, "report": report, "rows": [], "meta": {},
                "error": "CFTC unreachable: %s" % (e.reason,)}
    except Exception as e:
        return {"ok": False, "report": report, "rows": [], "meta": {},
                "error": "%s: %s" % (type(e).__name__, e)}


def fetch_historical_parse(report, year, cache_dir=None, stats=None):
    """Fetch + parse one annual archive."""
    try:
        text, meta = fetch_historical(report, year, cache_dir=cache_dir)
        rows = parse(text, report, stats=stats)
        meta.update({"ok": True, "report": report, "rows": len(rows)})
        return {"ok": True, "report": report, "year": year, "rows": rows,
                "meta": meta, "error": None}
    except CotError as e:
        return {"ok": False, "report": report, "year": year, "rows": [],
                "meta": {}, "error": str(e)}
    except urllib.error.URLError as e:
        return {"ok": False, "report": report, "year": year, "rows": [],
                "meta": {}, "error": "CFTC unreachable: %s" % (e.reason,)}
    except Exception as e:
        return {"ok": False, "report": report, "year": year, "rows": [],
                "meta": {}, "error": "%s: %s" % (type(e).__name__, e)}


def latest_date(rows):
    """Most recent report_date in a parsed batch, or None."""
    best = None
    for r in rows or ():
        d = r.get("report_date")
        if d and (best is None or d > best):
            best = d
    return best

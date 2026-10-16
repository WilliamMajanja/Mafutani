"""Candle provenance: which bars are real, and what a result built on them
means. Stdlib only.

The problem
-----------
`scripts/seed.py` writes 1-minute bars from a seeded RNG for any symbol with
no history, and they land in `candles_1m` beside a genuine Binance backfill.
Nothing downstream could tell them apart, so a backtest over AAPL read a
random walk and returned its Sharpe as though it measured a strategy.

Measured on this deployment: of 49 symbols, 14 had a real venue route, 40
had real *daily* bars in `candles_1d` (back to 2021), and all 40 of those
also carried 43,200 synthetic minute bars each. The synthetic bars were the
ones the backtester read, because it only ever reads `candles_1m`.

Why refuse rather than warn
---------------------------
A warning is read once. The bars stay, the number stays, and the next
person to look at that Sharpe has no way to know it measured nothing.

So the default is refusal, and `allow_synthetic=True` is how you opt in --
which makes synthetic analysis *deliberate* rather than accidental. This is
the same rule the macro engine follows for an unmeasured regime, applied to
data instead of to a signal.

What counts as real
-------------------
Provenance is the `source` column, not the symbol. A synthetic row for a
symbol that also has real rows is still synthetic. The column is the only
authority, because a symbol being routable says nothing about whether *these
particular bars* came from a venue.
"""

__all__ = [
    "REAL_SOURCES", "SYNTHETIC_SOURCES", "is_real_source", "classify",
    "coverage", "gate", "describe",
]

# Venue names and upstream hosts whose output is a real observation.
REAL_SOURCES = frozenset([
    "binance", "coinbase", "kraken", "okx", "bybit",
    "yahoo-chart", "stooq", "alphavantage", "fred", "cftc",
])

# Explicitly not observations of a market.
SYNTHETIC_SOURCES = frozenset(["synthetic", "generated", "seed", ""])


def is_real_source(source):
    """True only for a named upstream.

    Unknown sources are treated as NOT real. A new feed that has not declared
    itself is a new feed whose provenance nobody has checked, and defaulting
    it to real would make the gate silently open as data sources are added.
    """
    s = str(source or "").strip().lower()
    if s in SYNTHETIC_SOURCES:
        return False
    return s in REAL_SOURCES


def classify(source):
    """`real`, `synthetic`, or `unknown`.

    Three states, not two. "We did not record where this came from" and
    "we recorded that we made this up" are different claims, and collapsing
    them is how an unlabelled row ends up treated as evidence.
    """
    s = str(source or "").strip().lower()
    if s in SYNTHETIC_SOURCES:
        return "synthetic"
    if s in REAL_SOURCES:
        return "real"
    return "unknown"


def coverage(conn, symbol, table="candles_1m"):
    """What this symbol's bars in `table` are made of.

    Returns {"symbol", "table", "total", "real", "synthetic", "unknown",
             "sources", "verdict"}. Never raises: a table without the column
    (pre-migration) reports unknown, which is the honest answer rather than
    an optimistic one.
    """
    out = {"symbol": str(symbol or ""), "table": str(table or ""),
           "total": 0, "real": 0, "synthetic": 0, "unknown": 0,
           "sources": {}, "verdict": "no data"}
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(%s)" % out["table"]).fetchall()}
    except Exception:
        return out
    sym = out["symbol"]
    try:
        out["total"] = int(conn.execute(
            "SELECT COUNT(*) FROM %s WHERE symbol = ?" % out["table"],
            (sym,)).fetchone()[0] or 0)
    except Exception:
        return out
    if not out["total"]:
        return out
    if "source" not in cols:
        out["unknown"] = out["total"]
        out["verdict"] = "unknown provenance"
        return out
    try:
        rows = conn.execute(
            "SELECT COALESCE(source,'') AS s, COUNT(*) AS n FROM %s"
            " WHERE symbol = ? GROUP BY s" % out["table"], (sym,)).fetchall()
    except Exception:
        out["unknown"] = out["total"]
        out["verdict"] = "unknown provenance"
        return out
    for r in rows:
        s = str(r[0] or "")
        n = int(r[1] or 0)
        out["sources"][s or "(blank)"] = n
        kind = classify(s)
        out[kind if kind != "unknown" else "unknown"] += n
    if out["real"] == out["total"]:
        out["verdict"] = "real"
    elif out["synthetic"] == out["total"]:
        out["verdict"] = "synthetic"
    elif out["real"] > 0:
        out["verdict"] = "mixed: %d real, %d synthetic" % (out["real"],
                                                            out["synthetic"])
    else:
        out["verdict"] = "unknown provenance"
    return out


def gate(conn, symbol, table="candles_1m", allow_synthetic=False):
    """May a result computed from these bars be quoted?

    Returns {"allowed", "verdict", "coverage", "reason"}. Synthetic data is
    allowed only on an explicit opt-in, and the reason always states what the
    number would then mean.
    """
    cov = coverage(conn, symbol, table)
    if cov["total"] == 0:
        return {"allowed": False, "verdict": "no data", "coverage": cov,
                "reason": ("no %s bars for %s: nothing to measure"
                           % (table, symbol))}
    if cov["verdict"] == "real":
        return {"allowed": True, "verdict": "real", "coverage": cov,
                "reason": "%d bars from %s" % (
                    cov["real"], ", ".join(sorted(cov["sources"])) or "a venue")}
    if allow_synthetic:
        return {"allowed": True, "verdict": cov["verdict"], "coverage": cov,
                "reason": ("ALLOWED BY REQUEST: these bars are not "
                           "observations of a market (%d of %d synthetic). "
                           "Any figure from them describes the generator, not "
                           "the instrument, and must not be quoted as "
                           "evidence of an edge." % (cov["synthetic"],
                                                     cov["total"]))}
    return {"allowed": False, "verdict": cov["verdict"], "coverage": cov,
            "reason": ("%d of %d %s bars for %s are synthetic -- generated by "
                       "scripts/seed.py, not observed from a venue. A figure "
                       "from them measures the generator. Real data is in "
                       "candles_1d for this symbol; use it, or pass "
                       "allow_synthetic=True if you are exercising a code "
                       "path rather than testing a strategy."
                       % (cov["synthetic"], cov["total"], table, symbol))}


def describe(conn, symbol=None, table="candles_1m"):
    """Coverage for one symbol, or for every symbol with bars in `table`."""
    if symbol:
        return gate(conn, symbol, table)
    out = []
    try:
        syms = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM %s ORDER BY symbol" % table
        ).fetchall()]
    except Exception:
        return []
    for s in syms:
        out.append(coverage(conn, s, table))
    return out

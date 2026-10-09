"""Tape integrity: does the candle series agree with the live quote?

The gap this exists to catch
----------------------------
`scripts/seed.py` backfills candles from a hardcoded BASE_PRICE table
(BTCUSD 65,000, ETHUSD 3,500) while the quote feed tracks the real
market. Nothing reconciles the two, so the candle tape walks away from
the price the book is actually trading at. Observed on this host:

    BTCUSD   quote 83,740   last candle close 69,908   +19.8%
    ETHUSDT  quote  2,687   last candle close  3,785   -29.0%

Everything computed off that tape is then confidently wrong. The anomaly
radar reported "close 66,897 below 60-bar low" for BTCUSDT as a fresh
breakdown while the market was at 83,740 -- a real level break inside a
stale series, and a phantom signal to anyone acting on it. The chart
showed 69,907 next to a ticker reading 83,683.

So this is a data-integrity check with teeth, not a nicety: a divergence
means the chart, the indicators, the detectors and the backtester are all
describing a market that does not exist.

`divergence()` reports; `reanchor()` repairs by rescaling the stale tail
of the series onto the live quote, preserving the shape (returns and
relative levels) while moving the price level to the market. Rescaling
rather than appending is deliberate: a jump insert would fabricate a
single enormous bar and light up every detector at once.
"""

# A gap beyond this fraction is worth *reporting*. 0.5% is inside normal
# drift, so breaching it means the tape and the quote are not describing
# the same moment.
DIVERGENCE_PCT = 0.5
# A gap beyond this fraction means the tape is anchored to a different
# market entirely, and only then is rescaling justified. Below it the
# series is merely older than the quote, and rewriting 43,200 bars of
# history to chase a 0.5% drift destroys more than it fixes. Rescaling
# only the recent tail is not an option either: it plants a synthetic
# step in the middle of the series, which every level detector would
# then read as a genuine breakout.
REPAIR_PCT = 3.0


def _last_quote(conn, symbol):
    try:
        r = conn.execute(
            "SELECT last, bid, ask, ts FROM quotes_l1 WHERE symbol = ?"
            " ORDER BY ts DESC LIMIT 1", (symbol,)).fetchone()
    except Exception:
        return None
    if r is None:
        return None
    try:
        for k in ("last", "ask", "bid"):
            v = float(r[k] or 0)
            if v > 0:
                return v
    except Exception:
        return None
    return None


def _last_close(conn, symbol):
    try:
        r = conn.execute(
            "SELECT close, ts FROM candles_1m WHERE symbol = ?"
            " ORDER BY ts DESC LIMIT 1", (symbol,)).fetchone()
    except Exception:
        return None
    if r is None:
        return None
    try:
        v = float(r["close"] or 0)
        return v if v > 0 else None
    except Exception:
        return None


def divergence(conn, symbols=None, threshold_pct=DIVERGENCE_PCT):
    """Report every symbol whose tape and quote disagree.

    Returns a list of dicts sorted worst-first, so the top of the list
    is the thing to fix. Empty list means the universe is coherent.
    """
    if symbols is None:
        try:
            symbols = [r[0] for r in conn.execute(
                "SELECT symbol FROM symbols WHERE enabled = 1 ORDER BY symbol")]
        except Exception:
            symbols = []
    out = []
    for sym in symbols:
        q = _last_quote(conn, sym)
        c = _last_close(conn, sym)
        if not q or not c:
            out.append({"symbol": sym, "quote": q, "close": c,
                        "pct": None, "status": "missing_quote" if not q
                        else "missing_candles"})
            continue
        pct = (q - c) / c * 100.0
        if abs(pct) >= threshold_pct:
            out.append({"symbol": sym, "quote": q, "close": c,
                        "pct": round(pct, 4), "status": "diverged",
                        "factor": round(q / c, 8)})
    out.sort(key=lambda r: (r.get("pct") is None,
                            -abs(r["pct"] or 0)))
    return out


def reanchor(conn, symbol, bars=0, dry_run=False):
    """Rescale a symbol's recent candles onto its live quote.

    Multiplies the trailing window's OHLC by (quote / last_close) so the
    newest bar lands on the market. The *shape* of the series -- its
    returns and relative levels -- is preserved exactly, which is what
    the indicators and detectors read. Older bars are left alone: they
    are genuine history at their own price level, and rewriting 30 days
    of tape to hide a seeding mistake destroys more than it fixes.
    """
    sym = str(symbol or "").strip().upper()
    q = _last_quote(conn, sym)
    c = _last_close(conn, sym)
    if not q or not c:
        return {"symbol": sym, "ok": False,
                "reason": "need both a quote and candles to reanchor"}
    factor = q / c
    if abs(factor - 1.0) < 1e-9:
        return {"symbol": sym, "ok": True, "changed": 0, "factor": 1.0,
                "note": "already aligned"}
    try:
        if bars and bars > 0:
            rows = conn.execute(
                "SELECT id FROM candles_1m WHERE symbol = ?"
                " ORDER BY ts DESC LIMIT ?", (sym, int(bars))).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM candles_1m WHERE symbol = ?"
                " ORDER BY ts DESC", (sym,)).fetchall()
        ids = [r["id"] for r in rows]
    except Exception as e:
        return {"symbol": sym, "ok": False, "reason": str(e)}
    if dry_run:
        return {"symbol": sym, "ok": True, "dry_run": True, "changed": len(ids),
                "factor": round(factor, 8), "from": c, "to": q}
    try:
        for cid in ids:
            row = conn.execute(
                "SELECT open, high, low, close FROM candles_1m WHERE id = ?",
                (cid,)).fetchone()
            if row is None:
                continue
            conn.execute(
                "UPDATE candles_1m SET open = ?, high = ?, low = ?, close = ?"
                " WHERE id = ?",
                (float(row["open"] or 0) * factor,
                 float(row["high"] or 0) * factor,
                 float(row["low"] or 0) * factor,
                 float(row["close"] or 0) * factor, cid))
        conn.commit()
    except Exception as e:
        return {"symbol": sym, "ok": False, "reason": str(e)}
    return {"symbol": sym, "ok": True, "changed": len(ids),
            "factor": round(factor, 8), "from": c, "to": q}


def reanchor_all(conn, bars=0, dry_run=False, threshold_pct=REPAIR_PCT):
    """Repair every symbol whose tape is anchored to a different market.

    Defaults to REPAIR_PCT, not DIVERGENCE_PCT: a sub-3% gap is drift
    and is reported but left alone. Returns per-symbol results, including
    the ones deliberately skipped and why.
    """
    results = []
    for row in divergence(conn, threshold_pct=DIVERGENCE_PCT):
        if row.get("status") != "diverged":
            results.append({"symbol": row["symbol"], "ok": True,
                            "skipped": row.get("status"), "pct": row.get("pct")})
            continue
        if abs(row.get("pct") or 0.0) < threshold_pct:
            results.append({"symbol": row["symbol"], "ok": True,
                            "skipped": "drift within tolerance",
                            "pct": row.get("pct")})
            continue
        results.append(reanchor(conn, row["symbol"], bars=bars,
                                dry_run=dry_run))
    return results

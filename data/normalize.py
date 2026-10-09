"""Normalization helpers for candles/quotes. Stdlib only, import-safe."""


def _f(x, default=0.0):
    try:
        if x is None or x == "":
            return float(default)
        return float(x)
    except (TypeError, ValueError):
        return float(default)


def _i(x, default=0):
    try:
        if x is None or x == "":
            return int(default)
        return int(float(x))
    except (TypeError, ValueError):
        return int(default)


def normalize_candle(raw):
    """Normalize a raw candle dict to canonical form.

    Accepts keys: symbol/s, ts/time/t, o/open, h/high, l/low, c/close,
    v/volume. Returns {symbol, ts, open, high, low, close, volume}
    with high>=max(o,c), low<=min(o,c).
    """
    if not isinstance(raw, dict):
        raise ValueError("candle must be a dict")
    symbol = raw.get("symbol", raw.get("s", raw.get("sym", "")))
    ts = raw.get("ts", raw.get("time", raw.get("t", 0)))
    o = _f(raw.get("open", raw.get("o", 0)))
    h = _f(raw.get("high", raw.get("h", 0)))
    l = _f(raw.get("low", raw.get("l", 0)))
    c = _f(raw.get("close", raw.get("c", 0)))
    v = _f(raw.get("volume", raw.get("v", 0)))
    hi = max(h, o, c)
    lo = min(l, o, c) if (l or o or c) else 0.0
    # if high/low missing (0), fall back to o/c range
    if h == 0:
        hi = max(o, c)
    if l == 0:
        lo = min(o, c)
    return {
        "symbol": str(symbol or ""),
        "ts": _i(ts),
        "open": o,
        "high": hi,
        "low": lo,
        "close": c,
        "volume": v,
    }


def normalize_quote(raw):
    """Normalize a raw L1 quote dict to {symbol, ts, bid, ask, bid_size, ask_size, last}."""
    if not isinstance(raw, dict):
        raise ValueError("quote must be a dict")
    symbol = raw.get("symbol", raw.get("s", raw.get("sym", "")))
    ts = raw.get("ts", raw.get("time", raw.get("t", 0)))
    bid = _f(raw.get("bid", raw.get("b", 0)))
    ask = _f(raw.get("ask", raw.get("a", 0)))
    # ensure bid <= ask when both present and crossed
    if bid > 0 and ask > 0 and bid > ask:
        bid, ask = ask, bid
    last = raw.get("last", raw.get("price", raw.get("p", None)))
    last = _f(last, (bid + ask) / 2.0 if (bid and ask) else (ask or bid or 0))
    return {
        "symbol": str(symbol or ""),
        "ts": _i(ts),
        "bid": bid,
        "ask": ask,
        "bid_size": _f(raw.get("bid_size", raw.get("bs", raw.get("bidSize", 0)))),
        "ask_size": _f(raw.get("ask_size", raw.get("as", raw.get("askSize", 0)))),
        "last": last,
    }


def spread_bps(bid, ask):
    """Spread in basis points: (ask-bid)/mid * 10000. Returns 0.0 on bad input."""
    try:
        bid = float(bid)
        ask = float(ask)
    except (TypeError, ValueError):
        return 0.0
    mid = (bid + ask) / 2.0
    if mid <= 0 or ask < bid:
        return 0.0
    return (ask - bid) / mid * 10000.0

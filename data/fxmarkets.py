"""FX pair universe and currency metadata. Stdlib only, import-safe.

Where the app's currency pairs are declared, which free venue quotes
each one, and the idempotent installer that puts them in `symbols`.

Why the FX universe needed its own module
-----------------------------------------
`scripts/seed.py` carried a single FX pair, EURUSD. That is enough to
quote a rate and not enough to *arbitrage* one: a cross-rate needs three
pairs (EURUSD against EURGBP x GBPUSD), and a triangular needs more still.
Keeping the growing FX universe in the seeder -- which is a dev
convenience, not a source of truth -- is how a pair needed by arbitrage
would end up missing from a deployed database.

Quote convention, stated once because everything here depends on it
--------------------------------------------------------------------
Every pair is quoted as *units of quote per 1 unit of base*: EURUSD =
1.08 means one euro buys 1.08 dollars. With GBPUSD = 1.27 and
EURGBP = 0.85, the cross is a plain multiplication, 0.85 x 1.27 = 1.08.
Getting the convention backwards inverts the trade and produces a
scanner that reports edges which vanish on contact with the real market.

`decimals` exists because the same pair needs a different display
precision at each end: EURUSD quotes to 5 decimals and USDJPY to 3, and
a UI that rounds both to 2 hides the entire edge.
"""

# (symbol, name, base, quote, venue, sector, tick_size, lot_size, base_price)
FX_SYMBOLS = (
    ("EURUSD", "Euro / US Dollar",     "EUR", "USD", "stooq", "FX", 0.00001, 1000.0, 1.08),
    ("GBPUSD", "Pound / US Dollar",    "GBP", "USD", "stooq", "FX", 0.00001, 1000.0, 1.27),
    ("USDJPY", "US Dollar / Yen",      "USD", "JPY", "stooq", "FX", 0.001,    1000.0, 157.20),
    ("AUDUSD", "Aussie / US Dollar",   "AUD", "USD", "stooq", "FX", 0.00001, 1000.0, 0.66),
    ("USDCHF", "US Dollar / Franc",    "USD", "CHF", "stooq", "FX", 0.00001, 1000.0, 0.90),
    ("USDCAD", "US Dollar / Loonie",   "USD", "CAD", "stooq", "FX", 0.00001, 1000.0, 1.36),
    ("NZDUSD", "Kiwi / US Dollar",     "NZD", "USD", "stooq", "FX", 0.00001, 1000.0, 0.61),
    ("EURGBP", "Euro / Pound",         "EUR", "GBP", "stooq", "FX", 0.00001, 1000.0, 0.85),
    ("EURJPY", "Euro / Yen",           "EUR", "JPY", "stooq", "FX", 0.001,    1000.0, 170.00),
    ("GBPJPY", "Pound / Yen",          "GBP", "JPY", "stooq", "FX", 0.001,    1000.0, 199.60),
    ("AUDJPY", "Aussie / Yen",         "AUD", "JPY", "stooq", "FX", 0.001,    1000.0, 104.00),
)

# Currency -> what a one-unit position is worth in USD. Only used for
# notional display, never for pricing: an edge is computed in the pair's
# own quote currency and only then converted for the size readout.
CURRENCY_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "JPY": 1.0 / 157.20,
    "AUD": 0.66,
    "CHF": 1.11,
    "CAD": 1.0 / 1.36,
    "NZD": 0.61,
}

SYMBOL_META = {
    s[0]: {"name": s[1], "base": s[2], "quote": s[3],
           "exchange": s[4].upper(), "sector": s[5],
           "tick_size": s[6], "lot_size": s[7], "base_price": s[8]}
    for s in FX_SYMBOLS
}


def split_pair(symbol):
    """(base, quote) for a symbol, from the registry or by parsing.

    Parsing the fallback is 6-char-convention only and is deliberately
    conservative: it refuses anything that is not three letters of base
    followed by three of quote, because a wrong decomposition produces a
    confidently inverted cross.
    """
    sym = str(symbol or "").strip().upper()
    meta = SYMBOL_META.get(sym)
    if meta:
        return meta["base"], meta["quote"]
    if len(sym) == 6 and sym.isalpha():
        return sym[:3], sym[3:]
    return None, None


def usd_value(amount, currency):
    """Convert `amount` of `currency` to a USD notional. Best effort."""
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return None
    rate = CURRENCY_USD.get(str(currency or "USD").strip().upper())
    if rate is None or amt == 0:
        return None
    return amt * rate


def decimals_for(symbol):
    """Display precision implied by the pair's tick size.

    Derived by counting the decimal places in the tick, because a
    hardcoded per-pair table is a table that goes stale the moment a
    pair is added. USDJPY ticks at 0.001 and so displays 3 places;
    EURUSD ticks at 0.00001 and displays 5.
    """
    tick = SYMBOL_META.get(str(symbol or "").strip().upper(), {}).get("tick_size")
    if not tick:
        return 5
    text = "{:.10f}".format(float(tick)).rstrip("0")
    if "." not in text:
        return 0
    return min(8, len(text.split(".")[1]))


def install(conn):
    """Idempotently ensure the FX symbols exist. Safe to re-run.

    INSERT OR IGNORE, matching data.cryptomarkets.install: a symbol an
    operator disabled locally, or relabelled into a different sector,
    must survive a boot migration untouched.

    tick_size/lot_size are written only on insert. Re-stating them on
    every boot would overwrite an operator's own tick adjustment, and
    tick size is exactly the kind of per-venue setting they tune.
    """
    rows = []
    for symbol, name, _base, quote, venue, sector, tick, lot, _px in FX_SYMBOLS:
        rows.append((symbol, "fx", name, venue.upper(), quote, sector,
                     float(tick), float(lot)))
    conn.executemany(
        "INSERT OR IGNORE INTO symbols"
        " (symbol, asset_type, name, exchange, currency, sector, tick_size,"
        "  lot_size, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)", rows)
    conn.commit()
    return len(rows)

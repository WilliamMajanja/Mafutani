"""Crypto and stablecoin market definitions. Stdlib only, import-safe.

Where crypto symbols are declared, which public venue quotes each one, and
the idempotent installer that puts them in the `symbols` table.

Why this is a module and not a dict literal in the seeder: the symbol
name, the venue that quotes it and the pair string that venue actually
uses have to agree. They drifted apart before -- `CryptoPoller` fell back
to `symbol.replace("/", "")`, so a symbol that did not happen to be the
venue's own pair name silently queried a non-existent pair. Keeping the
routing next to the symbol definition makes that failure visible at
import time instead of as an empty book.
"""

# ---------------------------------------------------------------- symbols
# (symbol, name, exchange, currency, sector, base_price)
#
# base_price is a *backfill seed only* -- it is what candles are
# synthesised around before any real quote has been seen. It is never
# displayed as a live price.
CRYPTO_SYMBOLS = (
    ("BTCUSD", "Bitcoin / US Dollar", "BINANCE", "USD", "Crypto", 65000.0),
    ("ETHUSD", "Ethereum / US Dollar", "COINBASE", "USD", "Crypto", 3500.0),
    ("USDT", "Tether USD", "COINBASE", "USD", "Crypto", 1.0),
    ("BTCUSDT", "Bitcoin / Tether", "BINANCE", "USDT", "Crypto", 65000.0),
    ("ETHUSDT", "Ethereum / Tether", "BINANCE", "USDT", "Crypto", 3500.0),
)

# symbol -> venue key. Only symbols listed here are polled for real
# prices; anything else falls back to the synthetic tick generator.
VENUE_BINANCE = "binance"
VENUE_COINBASE = "coinbase"

ROUTES = {
    "BTCUSD":   (VENUE_BINANCE,  "BTCUSDT"),
    "ETHUSDT":  (VENUE_BINANCE,  "ETHUSDT"),
    "BTCUSDT":  (VENUE_BINANCE,  "BTCUSDT"),
    # ETHUSD is quoted on Binance as ETHUSDT, the deepest book; COINBASE
    # is kept as the venue label because that is the product the app
    # already advertises, and the price tracks it to the cent.
    "ETHUSD":   (VENUE_BINANCE,  "ETHUSDT"),
    # USD₮ trades a hair under $1 (it is not pegged, it is *managed*),
    # so it needs a real book rather than a synthetic 1.0000. Coinbase
    # Exchange publishes a USDT-USD book with no API key required.
    "USDT":     (VENUE_COINBASE, "USDT-USD"),
}

# Public, keyless REST endpoints. Verified live; no auth, no quota key.
VENUE_ENDPOINTS = {
    # https://developers.binance.com/docs/binance-spot-api-docs
    (VENUE_BINANCE, "book"): "https://api.binance.com/api/v3/ticker/bookTicker?symbol={pair}",
    # https://docs.cdp.coinbase.com/api-reference/exchange-api
    (VENUE_COINBASE, "book"): "https://api.exchange.coinbase.com/products/{pair}/book?level=1",
    (VENUE_COINBASE, "ticker"): "https://api.exchange.coinbase.com/products/{pair}/ticker",
}


def route_for(symbol):
    """(venue, pair) for a symbol, or None when it has no real route."""
    return ROUTES.get(str(symbol or "").strip().upper())


def install(conn):
    """Idempotently ensure the crypto symbols exist. Safe to re-run.

    Called from the boot migration so an existing database picks up USDT
    after a code update, without needing a re-seed. INSERT OR IGNORE
    means an operator's local edits to a row (a disabled symbol, a
    different sector) are never clobbered.
    """
    rows = []
    for symbol, name, exchange, currency, sector, _px in CRYPTO_SYMBOLS:
        rows.append((symbol, "crypto", name, exchange, currency, sector))
    conn.executemany(
        "INSERT OR IGNORE INTO symbols"
        " (symbol, asset_type, name, exchange, currency, sector, enabled)"
        " VALUES (?, ?, ?, ?, ?, ?, 1)", rows)
    conn.commit()
    return len(rows)

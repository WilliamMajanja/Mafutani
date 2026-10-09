"""Per-venue book registry: which venue quotes which symbol, and at what cost.

Stdlib only, import-safe. Single source of truth for *where a second
price for an instrument comes from*, which is what arbitrage is made of.

Why this module exists
----------------------
`data.cryptomarkets.ROUTES` is 1:1 -- symbol -> one (venue, pair). That
is correct for the primary tape, which wants one honest price per
instrument, and it is why cross-venue arbitrage was not expressible
before: BTCUSD pointed at Binance's BTCUSDT book, and Coinbase's
BTC-USD book had nowhere to go.

This module adds the second dimension, keyed by (symbol, venue), and is
deliberately *separate* rather than a widening of ROUTES:

* ROUTES answers "which book represents this symbol's price?" -- exactly
  one answer per symbol, by construction.
* VENUE_ROUTES answers "which venues can I see this symbol on?" -- many
  answers per symbol, which is the whole point.

Widening ROUTES to a list would have forced every one of its existing
callers (`feeds.CryptoPoller.fetch_one`, `app._real_quotes`,
`tests/test_crypto_markets.py`) to decide which element to take. Keeping
them apart means the primary tape cannot accidentally start averaging
two venues into one invented price.

The cost model
--------------
An edge that ignores fees is not an edge. `VENUE_FEES_BPS` is per-venue
taker cost in basis points of notional, and the scanner nets two legs of
it against the gross spread. These are *round-trip taker* figures, not
VIP/maker rates -- optimistic numbers would make an unprofitable spread
look tradable, which is the specific failure mode that makes an
arbitrage scanner worse than no scanner at all.

Synthetic venues
----------------
A venue with no fee entry has no real book, and the scanner marks it
`synthetic` and refuses to price an edge from it. `VENUE_FEES_BPS` is
therefore the authority on which venues exist, not `VENUE_ROUTES`: a
venue's identity must not depend on which symbols it is currently
routed for. Those two tables once disagreed, and `kraken` -- which had a
26 bps fee -- was reported as synthetic, so the scanner refused a real
venue for being real.

Symbols listed in `SYNTHETIC_ONLY` have no free keyless source at all
(no exchange publishes a keyless book for silver, WTI or corn), so their
venue quotes are generated. `source` carries that provenance through
from the poll, and the spread model refuses to price them as executable.

A note on why stooq is gone
---------------------------
FX and commodity were originally backed by stooq's delayed CSV, which
publishes a *mid price only* -- bid == ask, a zero-width book. That
forced a notional "execution penalty" on every FX cross to stop the
scanner printing edges that cost nothing to capture and could not
actually be traded. The endpoint now returns 404 on every path
(including the pre-existing `EquityPoller`'s own URL form), so it was
replaced by kraken, which publishes a real two-sided book for each FX
pair. The penalty machinery went with it rather than being kept as a
configurable knob: it was compensating for a data limitation that no
longer exists.
"""

# Venue -> taker fee in bps, charged on both the buy and the sell leg.
# Reference retail figures; deliberately pessimistic.
#
# Every venue with a real book MUST appear here, and this table is the
# authority on that. `known_venues()` derives from here rather than from
# VENUE_ROUTES, which is what stops a fee-priced venue from also being
# reported as synthetic: the two tables used to disagree, and kraken
# (26 bps here, no route) was simultaneously "known" and "synthetic".
VENUE_FEES_BPS = {
    "binance": 10.0,     # 0.10% spot taker, no BNB discount assumed
    "coinbase": 60.0,    # ~0.06% taker + spread crossing
    "kraken": 26.0,      # 0.26% FX/metal taker
    "synthetic": 50.0,   # worst case, never a reason to "profit"
}

# Default when a venue is not in the table above. Unknown venue must be
# *expensive*, not free: an unpriced fee silently inflates every edge.
DEFAULT_FEE_BPS = 50.0

# Symbols whose only venue is a generated book, never a real endpoint.
#
# These are the instruments with no free keyless second source. Their
# arbitrage is a *relationship* between two of them (the Brent-WTI crack,
# gold/silver) rather than venue disagreement, so the scanner computes
# the ratio, reports it against its own history, and refuses to price an
# executable edge -- because there is no executable book to price.
#
# Gold is the exception and is deliberately absent from this list: both
# Binance and Kraken publish XAUTUSD, so gold cross-venue arbitrage is
# real. See the comment on VENUE_ROUTES.
SYNTHETIC_ONLY = (
    "XAGUSD", "XPTUSD", "XPDUSD", "COPPER", "ALUM", "NICKEL", "ZINC",
    "BRENT", "WTI", "NGAS", "GASOLINE", "HEATOIL",
    "CORN", "WHEAT", "OATS", "RRICE", "SOYBEANS", "SOYMEAL", "SOYOIL",
    "SUGAR", "COFFEE", "COCOA", "COTTON", "OJ",
    "FCATTLE", "LCATTLE", "LHOGS",
)

# Symbol -> tuple of (venue, venue_pair). The app's symbol name on the
# left, the pair string that feed actually publishes on the right.
#
# Crypto gets genuine second venues. FX gets kraken, which publishes a
# real two-sided book for every pair below -- not a mid-price, so the
# cross-rate models can be costed honestly instead of carrying a
# notional execution penalty.
#
# stooq used to back FX and commodity here. Every one of its quote
# endpoints now returns 404 (including the pre-existing EquityPoller's
# own URL form), so it was removed rather than left to fail quietly.
#
# Commodity: gold is the only contract with two free keyless venues
# (Binance XAUTUSD, Kraken XAUTUSD), so gold cross-venue arbitrage is
# genuinely executable. There is no free source for silver, platinum,
# palladium, base metals, energy or ags -- those fall to the synthetic
# generator and are labelled as such.
VENUE_ROUTES = {
    # -- crypto: two real venues each, which is what makes cross-venue
    #    arbitrage possible at all.
    "BTCUSD":   (("binance", "BTCUSDT"), ("coinbase", "BTC-USD")),
    "ETHUSD":   (("binance", "ETHUSDT"), ("coinbase", "ETH-USD")),
    "BTCUSDT":  (("binance", "BTCUSDT"), ("coinbase", "BTC-USDT")),
    "ETHUSDT":  (("binance", "ETHUSDT"), ("coinbase", "ETH-USDT")),
    "USDT":     (("coinbase", "USDT-USD"), ("binance", "USDTUSD")),

    # -- FX: kraken publishes a real two-sided book for each of these.
    "EURUSD":   (("kraken", "EURUSD"),),
    "GBPUSD":   (("kraken", "GBPUSD"),),
    "USDJPY":   (("kraken", "USDJPY"),),
    "AUDUSD":   (("kraken", "AUDUSD"),),
    "USDCHF":   (("kraken", "USDCHF"),),
    "USDCAD":   (("kraken", "USDCAD"),),
    "EURGBP":   (("kraken", "EURGBP"),),
    "EURJPY":   (("kraken", "EURJPY"),),
    # GBPJPY and NZDUSD are listed upstream but return
    # "EQuery:Unknown asset pair", so they are excluded here rather than
    # routed into a scan that can never price them.

    # -- commodity: gold only, on two venues.
    "XAUUSD":   (("binance", "XAUTUSDT"), ("kraken", "XAUTUSD")),
}

# asset_type -> venues that may quote it. Used by the scanner to keep a
# crypto venue from being credited with an FX book.
VENUE_ASSET_CLASS = {
    "binance": "multi",   # crypto spot + the XAUTUSD metal quote
    "coinbase": "crypto",
    "kraken": "multi",   # FX + metals + crypto
    "synthetic": "multi",
}


def venues_for(symbol):
    """Tuple of (venue, pair) for a symbol. Empty when it has no route."""
    return VENUE_ROUTES.get(str(symbol or "").strip().upper(), ())


def venue_names(symbol):
    """Just the venue names for a symbol, in registry order."""
    return tuple(v for v, _p in venues_for(symbol))


def pair_for(symbol, venue):
    """The pair string `venue` publishes for `symbol`, or None."""
    v = str(venue or "").strip().lower()
    for name, pair in venues_for(symbol):
        if name == v:
            return pair
    return None


def fee_bps(venue):
    """Round-trip taker cost for a venue in bps. Unknown venue = DEFAULT."""
    return float(VENUE_FEES_BPS.get(str(venue or "").strip().lower(),
                                    DEFAULT_FEE_BPS))


def known_venues():
    """Every venue that has a real, executable book.

    Derived from `VENUE_FEES_BPS` -- the table that prices a venue -- and
    not from `VENUE_ROUTES`, the table that says which symbols a venue
    happens to carry right now.

    Reading the routes table instead is what made `kraken` simultaneously
    "known" (it had a 26 bps fee) and "synthetic" (it had no entry in
    the routes dict at the time), so a real venue was refused by the
    scanner for being real. A venue's identity must not depend on which
    symbols it is currently routed for.
    """
    return {v for v in VENUE_FEES_BPS if v and v != "synthetic"}


def is_synthetic(venue):
    """True when a venue has no real, executable book.

    A name with no fee entry is treated as synthetic on purpose. The
    alternative -- assuming an unrecognised venue is real and free --
    would let any unpriced string into the scanner produce an edge that
    cannot be traded.
    """
    v = str(venue or "").strip().lower()
    if not v or v in ("synthetic", "generated"):
        return True
    return v not in known_venues()


def is_synthetic_only(symbol):
    """True when a symbol's venue books are generated, not fetched.

    Used by the scanner to decide whether a commodity *relationship*
    (the Brent-WTI crack, gold/silver) can be presented as executable.
    With both legs generated there is no book to trade against, so the
    ratio is reported against history and never priced as an edge.
    """
    return str(symbol or "").strip().upper() in SYNTHETIC_ONLY


def asset_class_of(venue):
    """Coarse asset class a venue quotes: crypto / multi / ''."""
    return VENUE_ASSET_CLASS.get(str(venue or "").strip().lower(), "")


def all_venues():
    """Every venue the registry knows, for /api/arbitrage/venues."""
    out = []
    for v in sorted(known_venues() | {"synthetic"}):
        out.append({
            "venue": v,
            "fee_bps": fee_bps(v),
            "asset_class": asset_class_of(v),
            "synthetic": is_synthetic(v),
            "symbols": sorted(s for s, routes in VENUE_ROUTES.items()
                              if v in (name for name, _p in routes)),
        })
    return out

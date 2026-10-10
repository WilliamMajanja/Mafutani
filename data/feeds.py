"""Market-data pollers + live order router. Stdlib only, import-safe.

- No server imports. Normalizers imported lazily with fallback so this
  module stays import-safe from any working directory.
- Each poller isolates per-symbol failures (try/except per symbol).
- Live trading is gated by LIVE_TRADING_ENABLED=1 env flag.
"""
import csv
import json
import os
import random
import time
import urllib.parse
import urllib.request

try:
    from data.normalize import normalize_candle, normalize_quote, spread_bps
except ImportError:  # pragma: no cover - fallback for alternate sys.path
    try:
        from normalize import normalize_candle, normalize_quote, spread_bps
    except ImportError:
        normalize_candle = None
        normalize_quote = None
        spread_bps = None


def _now():
    return int(time.time())


def _http_get(url, timeout=8):
    """GET a URL, through the operator's proxy when one is configured.

    Delegates to data.proxy when a proxy is set, and to urllib otherwise.
    With no proxy configured this is byte-for-byte the previous behaviour,
    so adding proxy support cannot change how a deployment that never set
    MAFUTANI_PROXY behaves.
    """
    try:
        from data import proxy as _proxy
    except ImportError:  # pragma: no cover - alternate sys.path
        return _urllib_get(url, timeout)
    if _proxy.proxy_url() is None:
        return _urllib_get(url, timeout)
    status, _headers, text = _proxy.request("GET", url, timeout=timeout)
    if status and (status < 200 or status >= 300):
        raise ValueError("HTTP {} from {}".format(status, url))
    return text


def _urllib_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "mafutani-tcc/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ ticks
def synthetic_tick(symbol, last_price=100.0, spread_bps_target=5.0):
    """Generate a synthetic L1 quote around last_price (for dev/SSE)."""
    try:
        px = float(last_price)
    except (TypeError, ValueError):
        px = 100.0
    if px <= 0:
        px = 100.0
    half = px * float(spread_bps_target) / 20000.0
    jitter = px * 0.0001 * random.uniform(-1, 1)
    mid = px + jitter
    quote = {
        "symbol": str(symbol),
        "ts": _now(),
        "bid": round(mid - half, 4),
        "ask": round(mid + half, 4),
        "bid_size": round(random.uniform(0.1, 5), 4),
        "ask_size": round(random.uniform(0.1, 5), 4),
        "last": round(mid, 4),
    }
    if normalize_quote is not None:
        try:
            return normalize_quote(quote)
        except Exception:
            return quote
    return quote


# ------------------------------------------------------------------ pollers
def _num(value, default=0.0):
    """Venue prices arrive as strings; None/''/'0' must not become prices."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f > 0 else default


def _parse_binance_book(text, symbol):
    """Binance `/api/v3/ticker/bookTicker`: best bid/ask as strings."""
    raw = json.loads(text)
    if not isinstance(raw, dict) or "bidPrice" not in raw:
        raise ValueError("unexpected binance book shape: {}".format(
            str(raw)[:120]))
    return {
        "symbol": symbol,
        "ts": _now(),
        "bid": _num(raw.get("bidPrice")),
        "ask": _num(raw.get("askPrice")),
        "bid_size": _num(raw.get("bidQty")),
        "ask_size": _num(raw.get("askQty")),
    }


def _parse_coinbase_book(text, symbol):
    """Coinbase Exchange level-1 book.

    Shape is `{"bids": [[price, size], ...], "asks": [[price, size], ...]}`
    with best-first ordering, so the first entry on each side is the top.
    """
    raw = json.loads(text)
    if not isinstance(raw, dict) or not raw.get("bids") or not raw.get("asks"):
        raise ValueError("empty coinbase book: {}".format(str(raw)[:120]))
    bid = raw["bids"][0]
    ask = raw["asks"][0]
    return {
        "symbol": symbol,
        "ts": _now(),
        "bid": _num(bid[0]),
        "ask": _num(ask[0]),
        "bid_size": _num(bid[1]),
        "ask_size": _num(ask[1]),
    }


def _parse_kraken_ticker(text, symbol):
    """Kraken `/0/public/Ticker`: one real two-sided book.

    Shape is `{"error": [], "result": {"ZEURZUSD": {"a": [price, whole,
    lots], "b": [...], "c": [...]}}}`. Two things make this fiddly and
    both are handled here rather than by the caller:

    * The result key is the venue's *own* pair name, not the one we asked
      for. `EURUSD` comes back as `ZEURZUSD`, `GBPUSD` as `ZGBPZUSD`,
      but `EURGBP` as plain `EURGBP`. Taking `result[0]` blindly would
      work only by luck of ordering.
    * Errors arrive as HTTP 200 with `{"error": ["EQuery:Unknown asset
      pair"]}`. A parser that only checked for a missing key would read
      that as an empty book, and an absent venue would look like a venue
      quoting nothing rather than a venue that does not exist.
    """
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("unexpected kraken shape: {}".format(str(raw)[:120]))
    if raw.get("error"):
        raise ValueError("kraken rejected {}: {}".format(
            symbol, "; ".join(str(e) for e in raw["error"])))
    result = raw.get("result") or {}
    if not result:
        raise ValueError("kraken returned no book for {}".format(symbol))
    row = result.get(symbol)
    if row is None:
        # Fall back to the single entry only when it is unambiguous.
        if len(result) != 1:
            raise ValueError("kraken returned {} books for {}, expected {}"
                             .format(len(result), symbol, symbol))
        row = list(result.values())[0]
    a, b = row.get("a") or [], row.get("b") or []
    if not a or not b:
        raise ValueError("kraken returned a one-sided book for {}".format(symbol))
    return {
        "symbol": symbol,
        "ts": _now(),
        "bid": _num(b[0]),
        "ask": _num(a[0]),
        "bid_size": _num(b[2]),
        "ask_size": _num(a[2]),
        "last": _num((row.get("c") or [0])[0]),
    }


# venue -> (endpoint template, parser). Kept as data so a new venue is a
# new row here, not a new forked poller class.
_BOOK_SOURCES = {
    "binance": (
        "https://api.binance.com/api/v3/ticker/bookTicker?symbol={pair}",
        _parse_binance_book,
    ),
    "coinbase": (
        "https://api.exchange.coinbase.com/products/{pair}/book?level=1",
        _parse_coinbase_book,
    ),
    # Kraken publishes real two-sided books for FX pairs and metals with
    # no key. Replaces stooq, whose quote endpoints now 404 and which
    # only ever published a mid price anyway (bid == ask), which forced
    # a notional execution penalty onto every FX cross.
    # https://docs.kraken.com/api/docs/rest-api/get-ticker-information
    "kraken": (
        "https://api.kraken.com/0/public/Ticker?pair={pair}",
        _parse_kraken_ticker,
    ),
}


def fetch_venue_book(symbol, venue, pair, timeout=8):
    """One real top-of-book quote from a public venue endpoint.

    Raises on anything unexpected rather than returning a zero price: a
    caller that cannot distinguish "no data" from "price is zero" will
    publish a 0.00 quote, and in a trading app a 0.00 quote is worse than
    a missing one.
    """
    source = _BOOK_SOURCES.get(str(venue or "").lower())
    if source is None:
        raise ValueError("no book source for venue '{}'".format(venue))
    template, parser = source
    url = template.format(pair=urllib.parse.quote(str(pair), safe="-_/"))
    quote = parser(_http_get(url, timeout=timeout), symbol)
    if not (quote["bid"] > 0 and quote["ask"] > 0):
        raise ValueError("venue returned a non-positive book for {} "
                         "(bid={} ask={})".format(symbol, quote["bid"],
                                                   quote["ask"]))
    if quote["ask"] < quote["bid"]:
        # A crossed book means we misread the shape; refuse rather than
        # publish a negative spread that would look like a real quote.
        raise ValueError("crossed book for {} (bid={} > ask={})".format(
            symbol, quote["bid"], quote["ask"]))
    if normalize_quote is not None:
        return normalize_quote(quote)
    return quote


class CryptoPoller:
    """Real top-of-book quotes for crypto symbols, routed per venue.

    Symbols are routed through `data.cryptomarkets.ROUTES`, which maps the
    app's symbol name to the pair string that venue actually lists. The
    previous fallback -- `symbol.replace("/", "")` -- meant any symbol
    that was not already the venue's own pair name silently queried a
    pair that does not exist.
    """

    def __init__(self, symbols, timeout=8):
        self.symbols = list(symbols or [])
        self.timeout = timeout

    def fetch_one(self, symbol):
        """Return a normalized quote, or {'symbol':.., 'error':..}."""
        try:
            from data import cryptomarkets as _cm
        except ImportError:  # pragma: no cover - alternate sys.path
            import cryptomarkets as _cm
        route = _cm.route_for(symbol)
        if route is None:
            return {"symbol": symbol, "ts": _now(),
                    "error": "no venue route for {}".format(symbol)}
        venue, pair = route
        try:
            quote = fetch_venue_book(symbol, venue, pair, timeout=self.timeout)
        except Exception as e:
            return {"symbol": symbol, "ts": _now(), "error": str(e),
                    "venue": venue}
        quote["venue"] = venue
        quote["source"] = venue
        return quote

    def poll_once(self):
        out = {}
        for sym in self.symbols:
            try:
                out[sym] = self.fetch_one(sym)
            except Exception as e:  # never let one symbol kill the poll
                out[sym] = {"symbol": sym, "ts": _now(), "error": str(e)}
        return out

class VenuePoller:
    """Top-of-book per (symbol, venue) for arbitrage.

    Unlike `CryptoPoller`, this keeps every venue rather than collapsing
    to one: the second venue's price is the entire input to a cross-venue
    edge, so any "pick the best one" reduction here would delete the
    thing being measured.

    Routes come from `data.venuebooks.VENUE_ROUTES`, not
    `cryptomarkets.ROUTES`, for the same reason.
    """

    def __init__(self, symbols, timeout=8, workers=8):
        self.symbols = [str(s or "").strip().upper() for s in (symbols or [])]
        self.timeout = timeout
        self.workers = max(1, int(workers or 1))

    def routes(self):
        """[(symbol, venue, pair)] for every routable venue."""
        try:
            from data import venuebooks as _vb
        except ImportError:  # pragma: no cover - alternate sys.path
            import venuebooks as _vb
        out = []
        for sym in self.symbols:
            for venue, pair in _vb.venues_for(sym):
                out.append((sym, venue, pair))
        return out

    @staticmethod
    def enabled():
        """True unless MAFUTANI_ARB_FEED=0 turns venue polling off.

        Venue polling fans out to up to two HTTP calls per symbol on
        every tick. That is a real cost against third-party rate limits,
        so an operator can switch it off without a code change -- but it
        defaults to on, because an arbitrage scanner with no books is a
        scanner that always says "no data".
        """
        return os.environ.get("MAFUTANI_ARB_FEED", "1") != "0"

    def fetch(self, symbol, venue, pair):
        """One book, or {'error':..}. Never raises."""
        try:
            quote = fetch_venue_book(symbol, venue, pair, timeout=self.timeout)
        except Exception as e:
            return {"symbol": symbol, "venue": venue, "ts": _now(),
                    "error": str(e)}
        quote["venue"] = venue
        quote["source"] = venue
        return quote

    def poll_once(self):
        """{(symbol, venue): quote}. Errors are recorded per book, not raised."""
        jobs = self.routes()
        if not jobs:
            return {}
        out = {}

        def _run(job):
            sym, venue, pair = job
            return (sym, venue), self.fetch(sym, venue, pair)

        try:
            from concurrent.futures import ThreadPoolExecutor
            from concurrent.futures import wait as _wait
        except ImportError:  # pragma: no cover - stdlib, but keep the fallback
            for job in jobs:
                key, quote = _run(job)
                out[key] = quote
            return out

        pool = ThreadPoolExecutor(max_workers=min(self.workers, len(jobs)))
        try:
            futures = [pool.submit(_run, job) for job in jobs]
            # One shared deadline for the whole batch. Per-book timeouts
            # would make a tick's wall clock the sum of every venue's
            # latency, which is the stall this batching exists to avoid.
            done, _pending = _wait(futures, timeout=self.timeout)
            for fut in done:
                try:
                    key, quote = fut.result()
                except Exception:
                    continue
                if quote:
                    out[key] = quote
        finally:
            # wait=False: a book still blocked on a dead socket must not
            # hold up the tick. Its result is discarded.
            pool.shutdown(wait=False)
        return out


def persist_venue_quotes(conn, books):
    """Write a {(symbol, venue): quote} batch to venue_quotes.

    Quotes carrying an `error` are skipped rather than stored: a failed
    fetch is not a price, and writing bid=0 would surface as a bogus
    infinite spread on the very next scan.

    `INSERT OR IGNORE` on (symbol, venue, ts) makes a re-poll within the
    same second idempotent, which matters because the feed loop and the
    scanner can both write in the same tick.
    """
    rows = []
    now = _now()
    for key, quote in (books or {}).items():
        if not isinstance(quote, dict) or quote.get("error"):
            continue
        symbol, venue = key if isinstance(key, (tuple, list)) and len(key) == 2 \
            else (quote.get("symbol"), quote.get("venue"))
        bid, ask = quote.get("bid"), quote.get("ask")
        try:
            bid, ask = float(bid or 0), float(ask or 0)
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        rows.append((str(symbol), str(venue),
                     int(quote.get("ts") or now), bid, ask,
                     float(quote.get("bid_size") or 0),
                     float(quote.get("ask_size") or 0),
                     quote.get("last"),
                     str(quote.get("source") or quote.get("venue") or "venue")))
    if not rows:
        return 0
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO venue_quotes"
            " (symbol, venue, ts, bid, ask, bid_size, ask_size, last, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    except Exception:
        return 0
    return len(rows)


def venue_persist_enabled():
    """True when polled venue books should be written to venue_quotes.

    Off by default.

    The scanner does not need this: `live_venue_books()` fetches and
    caches books on demand, so arbitrage works from a cold start with no
    history at all. Persisting buys one thing -- a record of what the
    books looked like earlier -- and costs a growing table on any
    long-lived deployment.

    So the default is to fetch on demand and keep nothing, and an
    operator who wants the history opts in with MAFUTANI_ARB_PERSIST=1.
    That keeps a production database from silently accumulating a second
    market-data table nobody asked for.
    """
    return os.environ.get("MAFUTANI_ARB_PERSIST", "0") == "1"


# On-demand venue books, cached briefly in-process.
#
# The TTL is what makes this safe to call from a UI that re-scans on a
# timer and a terminal that a user will hold on one key. Every venue
# call is an outbound request to a third party, so a scan must not mean
# one request per venue per keystroke.
_LIVE_BOOK_CACHE = {"ts": 0, "books": {}}
LIVE_BOOK_TTL_S = 15


def live_venue_books(symbols, ttl=None, timeout=6):
    """Fresh {symbol: {venue: book}}, cached for a few seconds.

    Falls back to the cached set when every fetch fails, so a venue
    outage degrades to the last good books rather than to "no data".
    Returns {} only when there was never a successful fetch.
    """
    try:
        from data import venuebooks as _vb
    except ImportError:  # pragma: no cover - alternate sys.path
        import venuebooks as _vb
    syms = [str(s or "").strip().upper() for s in (symbols or [])]
    syms = [s for s in syms if _vb.venues_for(s)]
    if not syms:
        return {}
    window = float(LIVE_BOOK_TTL_S if ttl is None else ttl)
    now = time.time()
    if _LIVE_BOOK_CACHE["books"] and (now - _LIVE_BOOK_CACHE["ts"]) < window:
        cached = _LIVE_BOOK_CACHE["books"]
        if all(s in cached for s in syms):
            return {s: cached[s] for s in syms}

    poller = VenuePoller(syms, timeout=timeout, workers=8)
    try:
        flat = poller.poll_once()
    except Exception:
        flat = {}
    out = {}
    for key, quote in (flat or {}).items():
        if not isinstance(quote, dict) or quote.get("error"):
            continue
        symbol, venue = key if isinstance(key, (tuple, list)) and len(key) == 2 \
            else (quote.get("symbol"), quote.get("venue"))
        bid, ask = quote.get("bid"), quote.get("ask")
        try:
            bid, ask = float(bid or 0), float(ask or 0)
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        out.setdefault(str(symbol), {})[str(venue)] = {
            "symbol": symbol, "venue": venue, "ts": _now(),
            "bid": bid, "ask": ask,
            "bid_size": quote.get("bid_size", 0) or 0,
            "ask_size": quote.get("ask_size", 0) or 0,
            "last": quote.get("last"),
            "source": str(quote.get("source") or venue),
        }
    if out:
        _LIVE_BOOK_CACHE["books"].update(out)
        _LIVE_BOOK_CACHE["ts"] = now
        return out
    return dict(_LIVE_BOOK_CACHE["books"])


# Retention for venue_quotes, in rows per (symbol, venue) book.
#
# Both bounds were 40 / 2h, which sounds generous and is not. Measured on
# this deployment: 37 rows per instrument covering 1.6 hours, and 36 rows
# covering 27 minutes. Every microstructure question is unanswerable at that
# depth -- order-flow imbalance needs a sequence, a "persistent gap" needs
# to have persisted, and neither can be distinguished from a transient with
# 37 points.
#
# 1440 rows is roughly one day at the current poll cadence, which is the
# minimum span at which "this dislocation lasted" is a claim with evidence
# behind it rather than an assumption. It is still bounded: the cap and the
# age limit both remain, so a long-lived deployment cannot grow without
# limit.
#
# Overridable because the right number depends on the poll interval, which
# is not fixed here: MAFUTANI_VQ_KEEP rows/book, MAFUTANI_VQ_MAX_AGE_S
# seconds. Set the cap low if the database is on slow storage, because
# venue_quotes sits next to candles_1m and competes for the same pages.
DEFAULT_VQ_KEEP = 1440
DEFAULT_VQ_MAX_AGE_S = 3 * 24 * 3600


def _vq_keep():
    raw = os.environ.get("MAFUTANI_VQ_KEEP")
    if not raw:
        return DEFAULT_VQ_KEEP
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_VQ_KEEP
    # A cap below ~200 makes microstructure work impossible; below 40 it
    # breaks the cross-venue comparison the scanner exists to do. Refusing
    # is better than silently accepting a value that guarantees a
    # single-venue read of a two-venue market.
    return max(40, v)


def _vq_max_age_s():
    raw = os.environ.get("MAFUTANI_VQ_MAX_AGE_S")
    if not raw:
        return DEFAULT_VQ_MAX_AGE_S
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_VQ_MAX_AGE_S
    return max(60, v)


def prune_venue_quotes(conn, keep_per_book=None, max_age_s=None):
    """Trim venue_quotes per (symbol, venue), and drop anything stale.

    Two independent conditions, both necessary. The per-book cap bounds the
    table between the first poll of an hour and the first prune; the age
    limit is what stops an overnight gap from filling the table with history
    nobody reads. Either alone leaves a path to unbounded growth.

    Partitioning by (symbol, venue) rather than symbol alone is essential.
    Partitioning on symbol would let a busy venue evict the only record of a
    quiet one, and the scanner would then see a symbol as single-venue --
    silently reporting "no dislocation" for a market that has two. That is
    the specific failure this partitioning exists to prevent.
    """
    # Clamp an explicit override too, not just the env-derived value. A
    # caller passing keep_per_book=5 would otherwise reintroduce exactly the
    # single-venue-read failure the partitioning above prevents.
    keep = max(40, _vq_keep() if keep_per_book is None
               else int(keep_per_book))
    age = max(60, _vq_max_age_s() if max_age_s is None else int(max_age_s))
    try:
        cutoff = _now() - age
        cur = conn.execute(
            "DELETE FROM venue_quotes WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ts, ROW_NUMBER() OVER ("
            "      PARTITION BY symbol, venue ORDER BY ts DESC, id DESC) AS rn"
            "    FROM venue_quotes"
            "  ) WHERE rn > ? OR ts < ?)",
            (keep, cutoff))
        removed = cur.rowcount or 0
        if removed:
            conn.commit()
        return int(removed)
    except Exception:
        return 0


def venue_quote_depth(conn):
    """Per (venue, symbol): rows, distinct timestamps, span seconds.

    Exists because "is there enough history to measure anything?" is the
    question that has to be answered before adding a microstructure model,
    and the answer was previously invisible. Depth is the binding constraint
    on everything in this area, so it is reported rather than assumed.
    """
    try:
        rows = conn.execute(
            "SELECT venue, symbol, COUNT(*) AS rows,"
            " COUNT(DISTINCT ts) AS dts, MIN(ts) AS lo, MAX(ts) AS hi"
            " FROM venue_quotes GROUP BY venue, symbol"
            " ORDER BY rows DESC"
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "venue": d.get("venue"),
            "symbol": d.get("symbol"),
            "rows": int(d.get("rows") or 0),
            "distinct_ts": int(d.get("dts") or 0),
            "span_s": int(d.get("hi") or 0) - int(d.get("lo") or 0),
        })
    return out


class EquityPoller:
    """Poll Stooq free CSV quotes per symbol. Errors isolated per symbol."""

    def __init__(self, symbols, timeout=8):
        self.symbols = list(symbols or [])
        self.timeout = timeout

    def fetch_one(self, symbol):
        try:
            q = symbol.lower() + ".us" if "." not in symbol.lower() else symbol.lower()
            url = "https://stooq.com/q/l/?s=" + urllib.parse.quote(q) + "&f=sd2t2ohlcv&h&e=csv"
            text = _http_get(url, timeout=self.timeout)
            rows = list(csv.DictReader(text.splitlines()))
            if not rows:
                raise ValueError("empty quote response")
            r = rows[0]
            close = float(r.get("Close") or 0)
            quote = {
                "symbol": symbol,
                "ts": _now(),
                "bid": close,
                "ask": close,
                "bid_size": 0,
                "ask_size": 0,
                "last": close,
                "volume": float(r.get("Volume") or 0),
            }
            if normalize_quote is not None:
                nq = normalize_quote(quote)
                nq["volume"] = quote["volume"]
                return nq
            return quote
        except Exception as e:
            return {"symbol": symbol, "ts": _now(), "error": str(e)}

    def poll_once(self):
        out = {}
        for sym in self.symbols:
            try:
                out[sym] = self.fetch_one(sym)
            except Exception as e:
                out[sym] = {"symbol": sym, "ts": _now(), "error": str(e)}
        return out


# ------------------------------------------------------------- tick retention
#
# quotes_l1 is append-only from the feed loop, but nothing in the codebase
# ever reads more than the latest tick per symbol -- every consumer is
# `... WHERE symbol = ? ORDER BY ts DESC LIMIT 1`. So the history is pure
# write amplification: ~7,200 rows/hour, growing forever, inflating
# COUNT(*) and index size for data that is never read.
#
# Retention is bounded both by a per-symbol cap and by an age limit; see
# prune_quotes() for why both are needed.

# Every consumer of quotes_l1 asks for "the latest tick for this symbol"
# (`... WHERE symbol = ? ORDER BY ts DESC LIMIT 1`) -- the terminal, the
# screener, the SSE stream, the anomaly detector. None of them reads
# history. At a 30s cadence, 200 rows per symbol is 100 minutes of tape
# and buys nothing, so the cap was cut to 60.
#
# The 60 is not arbitrary: it is enough that a restarted feed, a slow
# render, or an operator reading the table by hand all still see a
# meaningful recent sequence. A stalled feed must still look stalled
# rather than looking like a quiet market, and 60 rows at 30s is two
# hours of evidence for that. On a 49-symbol universe this drops the
# table from ~9,800 rows to ~2,940.
QUOTE_KEEP_PER_SYMBOL = 60
QUOTE_KEEP_MAX_AGE_S = 6 * 3600


def prune_quotes(conn, keep_per_symbol=QUOTE_KEEP_PER_SYMBOL,
                 max_age_s=QUOTE_KEEP_MAX_AGE_S):
    """Trim quotes_l1 to the most recent N ticks per symbol, and drop ticks
    older than `max_age_s` even when they are inside that cap.

    Both conditions are applied independently (OR, not AND): capping
    recent data is what actually keeps the table bounded between the
    first tick of the day and the first prune, and an age limit alone
    would let a 30s tick loop accumulate 24h of rows per symbol.

    Ranking is by (ts DESC, id DESC) so two ticks written in the same
    second still have a deterministic order, and the newest rows are the
    ones kept -- a reader racing the prune still finds a current quote.

    Returns the number of rows removed; 0 on any failure. Pruning is
    maintenance, so it must never be able to break the feed.
    """
    try:
        cutoff = _now() - int(max_age_s)
        cur = conn.execute(
            "DELETE FROM quotes_l1 WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ts, ROW_NUMBER() OVER ("
            "      PARTITION BY symbol ORDER BY ts DESC, id DESC) AS rn"
            "    FROM quotes_l1"
            "  ) WHERE rn > ? OR ts < ?)",
            (int(keep_per_symbol), cutoff))
        removed = cur.rowcount or 0
        if removed:
            conn.commit()
        return int(removed)
    except Exception:
        return 0


# ------------------------------------------------------------------ live router
def _live_enabled():
    return os.environ.get("LIVE_TRADING_ENABLED", "0") == "1"


def cancel_live_order(order_row, broker="binance", user=None):
    """Cancel a live order at the venue. Raises if the venue refuses.

    Split out from `place_live_order` because cancelling is the operation
    an operator reaches for when something has gone wrong, and it needs
    the same honesty: if the exchange did not cancel it, say so and leave
    the local row alone. Marking the row 'cancelled' regardless produces
    an unexpected fill later with no local record of why.
    """
    if not _live_enabled():
        raise RuntimeError("live trading disabled (set LIVE_TRADING_ENABLED=1 to enable)")
    row = dict(order_row or {})
    venue_order_id = str(row.get("venue_order_id") or "").strip()
    if not venue_order_id:
        # No exchange id means we never got a confirmed acknowledgement.
        # Cancelling is impossible and pretending otherwise is worse than
        # saying so.
        raise RuntimeError(
            "order has no venue order id, so it cannot be cancelled at the "
            "exchange. Check the venue directly.")
    try:
        from brokers import get_broker
    except ImportError as e:
        raise RuntimeError("broker layer unavailable: {}".format(e))
    ud = user or {}
    venue = get_broker(broker, live=True, confirm=True,
                       tier_live_enabled=ud.get("tier"),
                       user_id=ud.get("id"))
    result = venue.cancel_order(venue_order_id, row.get("symbol", ""))
    status = str(getattr(result, "status", "") or "unknown")
    if status not in ("cancelled", "filled", "expired"):
        raise RuntimeError("venue returned status={} for cancel".format(status))
    return {"venue_order_id": venue_order_id, "status": status,
            "filled_at_venue": status == "filled", "ts": _now()}


def place_live_order(symbol, side, qty, order_type="market", price=None,
                     broker="binance", user_id=None, tier=None,
                     client_order_id=None):
    """Route a live order to a real venue.

    This used to be a stub that returned `{"status": "routed_stub"}`, and
    the caller treated any non-rejected status as a fill. The visible
    result was a live BTC buy that reported `filled`, opened a position,
    charged a fee and moved P&L -- for an asset the user did not own,
    because nothing had been sent anywhere. That is the single worst
    behaviour this application could exhibit, so the stub is gone rather
    than deprecated.

    What it does now:

    * refuses outright unless a venue is configured and the order carries
      the live tier + env gates;
    * submits through `brokers.binance`, which signs and returns the
      venue's own status;
    * raises on transport failure, so the caller records the order
      `unknown` instead of `filled`.

    `unknown` is a real state and it is not a failure to handle: a
    timeout after the venue accepted the order is exactly when an
    operator most needs to be told "go look at the exchange".
    """
    if not _live_enabled():
        raise RuntimeError(
            "live trading disabled (set LIVE_TRADING_ENABLED=1 to enable)")
    side = str(side).lower()
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy|sell")
    try:
        qty_f = float(qty)
    except (TypeError, ValueError):
        raise ValueError("qty must be numeric")
    if qty_f <= 0:
        raise ValueError("qty must be > 0")

    try:
        from brokers import get_broker
        from brokers.base import BrokerUnavailableError
    except ImportError as e:
        raise BrokerUnavailableError(
            "broker layer unavailable: {}".format(e), broker)

    venue = get_broker(broker, live=True, confirm=True,
                       tier_live_enabled=tier, user_id=user_id)
    order = venue.submit_order({
        "symbol": symbol, "side": side, "qty": qty_f,
        "type": order_type or "market", "price": price,
    })
    status = str(getattr(order, "status", "") or "unknown")
    return {
        # Status is the venue's, never ours.
        "status": status,
        "broker": broker,
        "venue_order_id": getattr(order, "order_id", "") or "",
        "client_order_id": getattr(order, "client_order_id", "") or
                           (client_order_id or ""),
        "symbol": getattr(order, "symbol", symbol),
        "side": side,
        "qty": getattr(order, "filled_qty", 0.0) or 0.0,
        "requested_qty": qty_f,
        "type": order_type or "market",
        "price": getattr(order, "avg_price", None),
        "filled": status == "filled",
        "ts": _now(),
        "source": "venue",
    }

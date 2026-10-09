"""Database adapter for the interactive broker session's Vibe commands.

`BrokerSession` takes a `data` object with three methods and no
dependency of its own. This module is the one place that satisfies that
contract from SQLite, so the broker layer stays importable without a
database and the query shapes live in exactly one file.

Every method returns plain dicts or tuples, and every method fails soft:
a closed connection or a missing table yields an empty result rather than
an exception. The Vibe commands treat "no data" as "nothing to learn
from", and that reading has to be correct when the cause is a fault
rather than an empty table -- so a fault is reported, not disguised.
"""

#: Bars fetched per symbol. 120 gives RSI(14) and a 20-bar volume mean
#: with room to spare on a 1m series, and keeps one `signals` run over
#: every symbol bounded.
DEFAULT_LOOKBACK = 120

#: Fills pulled for `shadow`. FIFO pairing is O(n log n) and a roundtrip
#: journal is a few thousand rows at most, so this is generous; the cap
#: exists so a pathological account cannot make the terminal unresponsive.
DEFAULT_FILL_LIMIT = 5000


class BrokerData(object):
    """Read-only view of the database for the Vibe layer.

    Read-only on purpose: the Vibe commands describe trades, they do not
    make them. Keeping this adapter unable to write means a bug in rule
    extraction cannot mutate a position, an order, or a fill.
    """

    __slots__ = ("conn", "_last_error")

    def __init__(self, conn):
        self.conn = conn
        self._last_error = None

    # -- error surface ---------------------------------------------------
    @property
    def last_error(self):
        """The most recent fault, or None.

        A caller that got an empty result needs to know whether the table
        was empty or the read failed; these are very different things to
        an operator deciding whether to trust a "no signals" line.
        """
        return self._last_error

    def _fail(self, exc, what):
        self._last_error = "{}: {}".format(what, exc)
        return None

    # -- reads -----------------------------------------------------------
    def fills(self, user_id=None, symbol=None, limit=DEFAULT_FILL_LIMIT):
        """Filled trades, oldest first, as dicts.

        `side` is normalised to buy/sell because the Vibe layer requires
        it and the database has no reason to agree on the spelling.
        """
        self._last_error = None
        if self.conn is None:
            return []
        sql = ("SELECT f.symbol AS symbol, f.qty AS qty, f.price AS price, "
               "       f.ts AS ts, o.side AS side "
               "FROM fills f JOIN orders o ON o.id = f.order_id ")
        where, params = [], []
        if user_id is not None:
            where.append("f.user_id = ?")
            params.append(user_id)
        if symbol:
            where.append("f.symbol = ?")
            params.append(symbol)
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        try:
            limit = max(1, min(int(limit), 100000))
        except (TypeError, ValueError):
            limit = DEFAULT_FILL_LIMIT
        # Oldest first, then a deterministic tiebreak: fills recorded in
        # the same second must keep a stable order or the FIFO pairing is
        # arbitrary between runs.
        sql += "ORDER BY f.ts ASC, f.id ASC LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except Exception as e:
            self._fail(e, "fills")
            return []
        out = []
        for r in rows:
            try:
                out.append({
                    "symbol": r["symbol"] or "",
                    "side": (r["side"] or "").strip().lower(),
                    "qty": float(r["qty"] or 0.0),
                    "price": float(r["price"] or 0.0),
                    "ts": int(r["ts"] or 0),
                })
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        return out

    def candles(self, symbol, limit=DEFAULT_LOOKBACK):
        """Recent 1m candles oldest first as (ts, open, high, low, close,
        volume) tuples.

        Tuples, not dicts, because the Vibe layer unpacks them positionally
        and a dict per candle for 120 candles times 39 symbols is a lot
        of allocation for a scan that reads five fields.
        """
        self._last_error = None
        if self.conn is None:
            return []
        try:
            limit = max(1, min(int(limit), 5000))
        except (TypeError, ValueError):
            limit = DEFAULT_LOOKBACK
        try:
            rows = self.conn.execute(
                "SELECT ts, open, high, low, close, volume "
                "FROM candles_1m WHERE symbol = ? "
                "ORDER BY ts DESC LIMIT ?", (symbol, limit)).fetchall()
        except Exception as e:
            self._fail(e, "candles")
            return []
        out = []
        for r in rows:
            try:
                out.append((int(r["ts"]), float(r["open"]), float(r["high"]),
                            float(r["low"]), float(r["close"]),
                            float(r["volume"] or 0.0)))
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        out.reverse()
        return out

    def symbols(self, enabled_only=True):
        """Tradable symbols, cheapest first for a scan."""
        self._last_error = None
        if self.conn is None:
            return []
        sql = "SELECT symbol FROM symbols"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY symbol"
        try:
            return [r["symbol"] for r in self.conn.execute(sql).fetchall()
                    if r["symbol"]]
        except Exception as e:
            self._fail(e, "symbols")
            return []

    def describe(self):
        """One line a command can print to explain an empty result."""
        if self.conn is None:
            return "no database connection"
        if self._last_error:
            return "read failed: {}".format(self._last_error)
        return "ok"


def attach(conn):
    """Convenience constructor used by the app and the terminal."""
    return BrokerData(conn)

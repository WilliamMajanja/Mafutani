"""`PaperBroker` -- the same normalized interface, backed by our own engine.

This is the default broker. It wraps `engines.paper` so the application
has one interface whether it is simulating or trading, and so a bug in a
venue adapter can never be mistaken for a real fill.

Deliberate properties:
  * `is_live` is always False, and it is not configurable.
  * Orders fill against the local quote/candle store with the same
    slippage + fee model as the rest of the platform, so paper results
    stay comparable with backtests.
  * Every state change is written to the same orders/fills/positions
    tables the UI already reads.
"""

import sqlite3
import time

from .base import (Broker, BrokerNotSupported, BrokerValidationError, Fill,
                   Order, Position, AccountInfo, Quote, OrderRequest, BUY,
                   MARKET, NEW, OPEN, FILLED, CANCELLED, REJECTED)


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


class PaperBroker(Broker):
    """Simulated execution against the local database."""

    name = "paper"
    venue = "Mafutani Paper"
    is_live = False
    supports_streaming = False

    def __init__(self, conn=None, user=None, user_id=None, **config):
        super().__init__(credentials=None, **config)
        self._conn = conn
        self._user = user
        self._user_id = user_id if user_id is not None else self._infer_uid(user)

    @staticmethod
    def _infer_uid(user):
        if user is None:
            return None
        if isinstance(user, int):
            return user
        for key in ("id", "user_id"):
            if hasattr(user, key):
                try:
                    return int(getattr(user, key))
                except (TypeError, ValueError):
                    pass
            if isinstance(user, dict) and key in user:
                try:
                    return int(user[key])
                except (TypeError, ValueError):
                    pass
        return None

    @property
    def conn(self):
        """Lazily open a connection so constructing never touches disk."""
        if self._conn is None:
            from data.db import connect as _connect
            self._conn = _connect()
        return self._conn

    def close(self):
        # Only close a connection we opened ourselves.
        if self._conn is not None and self.config.get("owns_conn"):
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._connected = False

    def connect(self):
        self._probe()
        self._connected = True
        return self

    def _probe(self):
        if self._user_id is None:
            raise BrokerValidationError(
                "PaperBroker needs a user_id or user to act for")
        row = self.conn.execute("SELECT id FROM users WHERE id = ?",
                                (self._user_id,)).fetchone()
        if row is None:
            raise BrokerValidationError(
                "no such user: {}".format(self._user_id))
        return True

    # -- trading ---------------------------------------------------------
    def submit_order(self, request):
        if isinstance(request, dict):
            request = OrderRequest(**request)
        req = request.validate()
        if self._user_id is None:
            raise BrokerValidationError("PaperBroker needs a user_id")
        from engines import paper as _paper
        order = {
            "symbol": req.symbol, "side": req.side, "qty": req.qty,
            "type": req.type, "price": req.price, "stop_price": req.stop_price,
            "mode": "paper", "reduce_only": req.reduce_only,
            "client_order_id": req.client_order_id,
        }
        try:
            result = _paper.match(self.conn, self._user, order)
        except TypeError:
            # engines.paper takes (conn, user, order); user may be None
            result = _paper.match(self.conn, {"id": self._user_id}, order)
        return self._to_order(result, req)

    def _to_order(self, result, req):
        """Normalize engines.paper's return value into an `Order`."""
        if isinstance(result, Order):
            return result
        r = result if isinstance(result, dict) else {}
        status = str(r.get("status", NEW)).lower()
        if status not in (NEW, OPEN, FILLED, CANCELLED, REJECTED):
            status = FILLED if _num(r.get("filled_qty")) >= req.qty else OPEN
        return Order(
            symbol=req.symbol, side=req.side, qty=req.qty, type=req.type,
            status=status, price=r.get("price", req.price),
            filled_qty=_num(r.get("filled_qty"), req.qty if status == FILLED else 0.0),
            avg_price=r.get("avg_price", r.get("price")),
            order_id=str(r.get("id") or r.get("order_id") or ""),
            client_order_id=req.client_order_id,
            venue=self.venue, ts=int(time.time()), raw=r,
        )

    def _order_row(self, order_id):
        return self.conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, self._user_id)).fetchone()

    def cancel_order(self, order_id):
        row = self._order_row(order_id)
        if row is None:
            raise BrokerNotSupported("no such order: {}".format(order_id))
        if str(row["status"]) in ("filled", "cancelled", "rejected"):
            raise BrokerValidationError(
                "order {} already {}".format(order_id, row["status"]))
        self.conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ? AND user_id = ?",
            (order_id, self._user_id))
        self.conn.commit()
        return self.get_order(order_id)

    def get_order(self, order_id):
        row = self._order_row(order_id)
        if row is None:
            raise BrokerNotSupported("no such order: {}".format(order_id))
        return self._row_to_order(row)

    def _row_to_order(self, row):
        filled = self.conn.execute(
            "SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(qty*price),0) n"
            " FROM fills WHERE order_id = ?", (row["id"],)).fetchone()
        fq = _num(filled["q"])
        return Order(
            symbol=row["symbol"], side=row["side"], qty=_num(row["qty"]),
            type=row["type"], status=row["status"], price=row["price"],
            filled_qty=fq,
            avg_price=(_num(filled["n"]) / fq) if fq else None,
            order_id=str(row["id"]), client_order_id=row["client_order_id"],
            venue=self.venue, ts=int(row["created_at"] or 0), raw=dict(row),
        )

    def list_orders(self, symbol=None, status=None, limit=100):
        sql = "SELECT * FROM orders WHERE user_id = ?"
        params = [self._user_id]
        if symbol:
            sql += " AND symbol = ?"
            params.append(str(symbol).upper())
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [self._row_to_order(r) for r in self.conn.execute(sql, params)]

    def positions(self):
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE user_id = ? AND qty != 0"
            " ORDER BY symbol", (self._user_id,)).fetchall()
        return [Position(
            symbol=r["symbol"], qty=_num(r["qty"]),
            avg_price=_num(r["avg_price"]),
            realized_pnl=_num(r["realized_pnl"]),
            venue=self.venue, raw=dict(r)) for r in rows]

    def account(self):
        row = self.conn.execute(
            "SELECT cash, starting_cash FROM accounts WHERE user_id = ?",
            (self._user_id,)).fetchone()
        cash = _num(row["cash"]) if row is not None else 0.0
        pos = self.positions()
        equity = cash
        for p in pos:
            q = self.quote(p.symbol)
            equity += p.qty * (q.last or p.avg_price)
        return AccountInfo(
            account_id="paper-{}".format(self._user_id), venue=self.venue,
            currency="USD", cash=cash, equity=equity, buying_power=cash,
            raw={"positions": len(pos)},
        )

    # -- market data -----------------------------------------------------
    def quote(self, symbol):
        symbol = str(symbol).upper()
        row = self.conn.execute(
            "SELECT * FROM quotes_l1 WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
            (symbol,)).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT close FROM candles_1m WHERE symbol = ?"
                " ORDER BY ts DESC LIMIT 1", (symbol,)).fetchone()
            if row is None:
                raise BrokerNotSupported("no market data for {}".format(symbol))
            last = _num(row["close"])
            return Quote(symbol=symbol, bid=last, ask=last, last=last,
                         venue=self.venue, ts=int(time.time()))
        return Quote(
            symbol=symbol, bid=_num(row["bid"]), ask=_num(row["ask"]),
            last=_num(row["last"] or row["bid"] or row["ask"]),
            bid_size=_num(row["bid_size"]), ask_size=_num(row["ask_size"]),
            ts=int(row["ts"] or 0), venue=self.venue,
        )

    def candles(self, symbol, timeframe="1m", limit=100):
        tf = str(timeframe or "1m")
        if tf != "1m":
            raise BrokerNotSupported(
                "paper broker only stores 1m candles (got {})".format(tf))
        rows = self.conn.execute(
            "SELECT ts, open, high, low, close, volume FROM candles_1m"
            " WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (str(symbol).upper(), int(limit))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def symbols(self):
        return [r[0] for r in self.conn.execute(
            "SELECT symbol FROM symbols WHERE enabled = 1 ORDER BY symbol")]

"""Paper-trade matching engine. Stdlib only, import-safe.

match(conn, user, order) -> fill dict.
  - market orders fill at the touch (ask for buys, bid for sells) + slippage.
  - limit orders rest unless they cross: buy fills when limit >=
    adjusted bid, sell fills when limit <= adjusted ask (filled at the
    touch-improved price, capped by the limit).
  - stop orders trigger like market once touched, else rest.
  - writes a fills row, upserts positions (avg-price + realized P&L),
    marks the order filled, and returns the fill dict.
  - non-crossing limit/stop orders stay 'open' and return
    {"filled": False, "status": "resting", ...}.

bracket(...) creates two child OCO rows (take-profit limit + stop-loss
stop) linked to the parent order. The orders table in schema.sql has no
parent_id column, so linkage is stored in parent_id when the column
exists, else encoded in client_order_id ("bracket-<parent>-tp/sl").
"""

import time

try:
    from engines.fees import apply_fee, fee_bps
except ImportError:  # pragma: no cover - direct-path execution
    from fees import apply_fee, fee_bps

DEFAULT_SLIPPAGE_BPS = 2.0


def _now():
    return int(time.time())


def _row_dict(row):
    if row is None:
        return {}
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        pass
    if isinstance(row, dict):
        return dict(row)
    return {}


def _user_id(user):
    if isinstance(user, dict):
        for k in ("id", "user_id", "userId"):
            if k in user and user[k] is not None:
                return int(user[k])
        raise ValueError("user dict lacks id")
    return int(user)


def _tier_of(user, order):
    if isinstance(order, dict) and order.get("tier"):
        return str(order.get("tier"))
    if isinstance(user, dict):
        for k in ("tier", "user_tier"):
            if user.get(k):
                return str(user.get(k))
    return "Free"


def _slip_of(order):
    for k in ("slippage_bps", "slippage", "slip_bps"):
        if isinstance(order, dict) and order.get(k) is not None:
            try:
                return max(0.0, float(order.get(k)))
            except (TypeError, ValueError):
                pass
    return DEFAULT_SLIPPAGE_BPS


def _touch(conn, symbol, order):
    """Return (bid, ask, last) or (0,0,0) when no market data."""
    try:
        q = conn.execute(
            "SELECT bid, ask, last FROM quotes_l1 WHERE symbol = ?"
            " ORDER BY ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    except Exception:
        q = None
    if q is not None:
        d = _row_dict(q)
        try:
            bid = float(d.get("bid") or 0)
            ask = float(d.get("ask") or 0)
            last = float(d.get("last") or 0)
            if bid > 0 or ask > 0 or last > 0:
                return bid, ask, last
        except (TypeError, ValueError):
            pass
    try:
        c = conn.execute(
            "SELECT close FROM candles_1m WHERE symbol = ?"
            " ORDER BY ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    except Exception:
        c = None
    if c is not None:
        try:
            px = float(c["close"] if "close" in c.keys() else c[0])
            if px > 0:
                return px, px, px
        except (TypeError, ValueError, KeyError, IndexError):
            pass
    try:
        px = float((order or {}).get("price") or 0)
        if px > 0:
            return px, px, px
    except (TypeError, ValueError, AttributeError):
        pass
    return 0.0, 0.0, 0.0


def _order_columns(conn):
    try:
        rows = conn.execute("PRAGMA table_info(orders)").fetchall()
        return {str(r["name"]) for r in rows}
    except Exception:
        return set()


def _ensure_order_row(conn, uid, order):
    """Return the canonical order row dict; insert one if order has no id."""
    if isinstance(order, dict) and order.get("id") is not None:
        try:
            row = conn.execute(
                "SELECT * FROM orders WHERE id = ?", (int(order["id"]),)).fetchone()
        except Exception:
            row = None
        if row is not None:
            d = _row_dict(row)
            merged = dict(order)
            merged.update({k: v for k, v in d.items()})
            merged["id"] = d.get("id", order["id"])
            return merged
    cols = _order_columns(conn)
    now = _now()
    o = dict(order or {})
    data = {
        "user_id": uid,
        "symbol": str(o.get("symbol", "") or ""),
        "side": str(o.get("side", "buy") or "buy").lower(),
        "type": str(o.get("type", o.get("order_type", "market")) or "market").lower(),
        "qty": float(o.get("qty", 0) or 0),
        "price": o.get("price"),
        "stop_price": o.get("stop_price", o.get("stopPrice")),
        "status": "open",
        "mode": str(o.get("mode", "paper") or "paper").lower(),
        "client_order_id": str(o.get("client_order_id", o.get("clientOrderId", "")) or ""),
        "created_at": int(o.get("created_at", now) or now),
    }
    if data["type"] not in ("market", "limit", "stop"):
        data["type"] = "market"
    keys = [k for k in data if k in cols]
    conn.execute(
        "INSERT INTO orders ({}) VALUES ({})".format(
            ", ".join(keys), ", ".join("?" for _ in keys)),
        [data[k] for k in keys],
    )
    oid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
    try:
        data["id"] = int(oid["id"] if "id" in oid.keys() else oid[0])
    except Exception:
        data["id"] = None
    conn.commit()
    return data


def _apply_position(conn, uid, symbol, side, qty, price):
    """Upsert positions row with avg-price accounting + realized P&L."""
    try:
        row = conn.execute(
            "SELECT qty, avg_price, realized_pnl FROM positions"
            " WHERE user_id = ? AND symbol = ?",
            (uid, symbol),
        ).fetchone()
    except Exception:
        row = None
    d = _row_dict(row)
    try:
        q0 = float(d.get("qty", 0) or 0)
        p0 = float(d.get("avg_price", 0) or 0)
        rp = float(d.get("realized_pnl", 0) or 0)
    except (TypeError, ValueError):
        q0, p0, rp = 0.0, 0.0, 0.0
    delta = qty if side == "buy" else -qty
    if q0 == 0.0 or (q0 * delta) > 0:
        new_qty = q0 + delta
        avg = (q0 * p0 + delta * price) / new_qty if new_qty != 0 else 0.0
    else:
        closed = min(abs(q0), abs(delta))
        direction = 1.0 if q0 > 0 else -1.0
        rp += closed * (price - p0) * direction
        new_qty = q0 + delta
        if new_qty == 0:
            avg = 0.0
        elif (new_qty > 0) != (q0 > 0):
            avg = price  # flipped through zero: new basis
        else:
            avg = p0
    if row is None:
        conn.execute(
            "INSERT INTO positions (user_id, symbol, qty, avg_price,"
            " realized_pnl, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, symbol, new_qty, avg, rp, _now()),
        )
    else:
        conn.execute(
            "UPDATE positions SET qty = ?, avg_price = ?, realized_pnl = ?,"
            " updated_at = ? WHERE user_id = ? AND symbol = ?",
            (new_qty, avg, rp, _now(), uid, symbol),
        )


def match(conn, user, order):
    """Match one paper order against the current touch. Returns fill dict."""
    uid = _user_id(user)
    slip = _slip_of(order if isinstance(order, dict) else {})
    o = _ensure_order_row(conn, uid, order if isinstance(order, dict) else {})
    symbol = str(o.get("symbol", "") or "")
    side = str(o.get("side", "buy") or "buy").lower()
    otype = str(o.get("type", "market") or "market").lower()
    qty = float(o.get("qty", 0) or 0)
    mode = str(o.get("mode", "paper") or "paper").lower()
    tier = _tier_of(user, o)
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy|sell")
    if not qty > 0:
        raise ValueError("qty must be > 0")
    if not symbol:
        raise ValueError("symbol required")

    bid, ask, last = _touch(conn, symbol, o)
    touch = ask if side == "buy" else bid
    if touch <= 0:
        touch = last
    if touch <= 0:
        raise ValueError("no market data for {}".format(symbol))
    slip_mult = slip / 10000.0

    resting_reason = None
    if otype == "market":
        px = touch * (1.0 + slip_mult) if side == "buy" else touch * (1.0 - slip_mult)
    elif otype == "limit":
        try:
            limit = float(o.get("price") or 0)
        except (TypeError, ValueError):
            limit = 0.0
        if limit <= 0:
            raise ValueError("limit order needs price")
        if side == "buy":
            slipped = (bid if bid > 0 else touch) * (1.0 + slip_mult)
            if limit < slipped:
                resting_reason = "limit-not-crossed"
                px = slipped
            else:
                px = min(slipped, limit)
        else:
            slipped = (ask if ask > 0 else touch) * (1.0 - slip_mult)
            if limit > slipped:
                resting_reason = "limit-not-crossed"
                px = slipped
            else:
                px = max(slipped, limit)
    elif otype == "stop":
        try:
            stop = float(o.get("stop_price") or o.get("price") or 0)
        except (TypeError, ValueError):
            stop = 0.0
        if stop <= 0:
            raise ValueError("stop order needs stop_price")
        triggered = (touch >= stop) if side == "buy" else (touch <= stop)
        if not triggered:
            resting_reason = "stop-not-triggered"
            px = touch
        else:
            px = touch * (1.0 + slip_mult) if side == "buy" else touch * (1.0 - slip_mult)
    else:
        px = touch * (1.0 + slip_mult) if side == "buy" else touch * (1.0 - slip_mult)

    if resting_reason is not None:
        if o.get("id") is not None:
            try:
                conn.execute("UPDATE orders SET status = 'open' WHERE id = ?",
                             (int(o["id"]),))
                conn.commit()
            except Exception:
                pass
        return {"filled": False, "status": "resting", "reason": resting_reason,
                "order_id": o.get("id"), "symbol": symbol, "side": side,
                "qty": qty}

    fee = apply_fee(qty * px, tier, mode)
    ts = _now()
    cur = conn.execute(
        "INSERT INTO fills (order_id, user_id, symbol, side, qty, price, fee, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (int(o["id"]), uid, symbol, str(side).lower(), qty, px, fee, ts),
    )
    try:
        fill_id = int(cur.lastrowid)
    except Exception:
        fill_id = None
    _apply_position(conn, uid, symbol, side, qty, px)
    try:
        conn.execute("UPDATE orders SET status = 'filled' WHERE id = ?",
                     (int(o["id"]),))
    except Exception:
        pass
    conn.commit()
    return {"filled": True, "status": "filled", "order_id": o.get("id"),
            "fill_id": fill_id, "user_id": uid, "symbol": symbol,
            "side": side, "qty": qty, "price": px, "fee": fee, "ts": ts,
            "mode": mode, "tier": tier}


def bracket(conn, user, order, tp_price=None, stop_price=None, **kw):
    """Create TP-limit + SL-stop child OCO rows for a parent order.

    Prices resolve in priority: explicit args > order/parent keys
    (tp_price/take_profit, stop_price/stop_loss) > tp_bps/sl_bps offsets
    from the entry price (defaults 20bps). Returns [tp_child, sl_child]
    dicts with ids.
    """
    uid = _user_id(user)
    o = _ensure_order_row(conn, uid, order if isinstance(order, dict) else {})
    parent_id = o.get("id")
    symbol = str(o.get("symbol", "") or "")
    side = str(o.get("side", "buy") or "buy").lower()
    close_side = "sell" if side == "buy" else "buy"
    try:
        qty = float(o.get("qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if parent_id is None or not symbol or not qty > 0:
        raise ValueError("parent order needs id, symbol, qty")

    # entry reference: last fill price, else order price, else touch
    entry = 0.0
    try:
        f = conn.execute(
            "SELECT price FROM fills WHERE order_id = ? ORDER BY id DESC LIMIT 1",
            (int(parent_id),),
        ).fetchone()
        if f is not None:
            entry = float(f["price"] if "price" in f.keys() else f[0])
    except Exception:
        entry = 0.0
    if not entry > 0:
        try:
            entry = float(o.get("price") or 0)
        except (TypeError, ValueError):
            entry = 0.0
    if not entry > 0:
        _, _, last = _touch(conn, symbol, o)
        entry = last

    def _num(*vals):
        for v in vals:
            if v is None:
                continue
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if x > 0:
                return x
        return 0.0

    tp = _num(tp_price, kw.get("take_profit"), o.get("tp_price"),
              o.get("take_profit"))
    sl = _num(stop_price, kw.get("stop_loss"), o.get("stop_price"),
              o.get("stop_loss"))
    tp_bps = _num(kw.get("tp_bps"), o.get("tp_bps"), 20.0)
    sl_bps = _num(kw.get("sl_bps"), o.get("sl_bps"), 20.0)
    if not tp > 0 and entry > 0:
        tp = entry * (1.0 + tp_bps / 10000.0) if side == "buy" \
            else entry * (1.0 - tp_bps / 10000.0)
    if not sl > 0 and entry > 0:
        sl = entry * (1.0 - sl_bps / 10000.0) if side == "buy" \
            else entry * (1.0 + sl_bps / 10000.0)
    if not tp > 0 or not sl > 0:
        raise ValueError("cannot resolve bracket prices (no entry reference)")

    cols = _order_columns(conn)
    mode = str(o.get("mode", "paper") or "paper").lower()
    now = _now()
    children = []
    specs = [
        ("tp", close_side, "limit", tp, None),
        ("sl", close_side, "stop", None, sl),
    ]
    for tag, cside, ctype, cprice, cstop in specs:
        data = {
            "user_id": uid,
            "symbol": symbol,
            "side": cside,
            "type": ctype,
            "qty": qty,
            "price": cprice,
            "stop_price": cstop,
            "status": "open",
            "mode": mode,
            "client_order_id": "bracket-{}-{}".format(parent_id, tag),
            "created_at": now,
        }
        if "parent_id" in cols:
            data["parent_id"] = int(parent_id)
        keys = [k for k in data if k in cols]
        cur = conn.execute(
            "INSERT INTO orders ({}) VALUES ({})".format(
                ", ".join(keys), ", ".join("?" for _ in keys)),
            [data[k] for k in keys],
        )
        try:
            cid = int(cur.lastrowid)
        except Exception:
            cid = None
        data["id"] = cid
        children.append(data)
    conn.commit()
    return children

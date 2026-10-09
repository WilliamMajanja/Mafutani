"""Pre-trade risk gate: signal -> risk -> execution. Stdlib only.

check(conn, user_id, order, tier) -> (True, "") or (False, reason).

Enforces, in order:
  1. kill-switch
  2. basic order sanity (side/qty/symbol)
  3. symbol allowlist (risk_limits.allowlist_json; empty = all allowed)
  4. max position qty (resulting absolute position)
  5. max notional (order qty x reference price)
  6. max daily loss (realized + unrealized mark-to-market vs -limit)
  7. max trades per hour (orders table count over trailing 60m)
  8. cooldown after N consecutive losing fills

Every rejection is logged to risk_events. Reads limits directly from the
risk_limits table (same defaults as core/risk.py) so this module never
imports server/db layers.
"""

import json
import time

DEFAULTS = {
    "max_position_qty": 100.0,
    "max_notional": 100000.0,
    "max_daily_loss": 1000.0,
    "max_trades_per_hour": 20,
    "cooldown_after_losses": 2,
    "allowlist_json": "[]",
    "eod_flatten": 0,
    "kill_switch": 0,
}

COOLDOWN_SECONDS = 15 * 60  # block window after consecutive losses


def _now():
    return int(time.time())


def _day_start(now=None):
    now = int(now if now is not None else _now())
    return now - (now % 86400)


def _row_dict(row):
    if row is None:
        return {}
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        pass
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def _get_limits(conn, user_id):
    merged = dict(DEFAULTS)
    try:
        row = conn.execute(
            "SELECT * FROM risk_limits WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    except Exception:
        row = None
    d = _row_dict(row)
    for k in DEFAULTS:
        if k in d and d[k] is not None:
            merged[k] = d[k]
    try:
        merged["max_position_qty"] = float(merged["max_position_qty"])
        merged["max_notional"] = float(merged["max_notional"])
        merged["max_daily_loss"] = float(merged["max_daily_loss"])
        merged["max_trades_per_hour"] = int(merged["max_trades_per_hour"])
        merged["cooldown_after_losses"] = int(merged["cooldown_after_losses"])
        merged["kill_switch"] = int(merged["kill_switch"] or 0)
    except (TypeError, ValueError):
        pass
    return merged


def _log(conn, user_id, reason, order):
    try:
        payload = json.dumps(order if isinstance(order, dict) else {"order": str(order)},
                             default=str)
    except Exception:
        payload = "{}"
    try:
        conn.execute(
            "INSERT INTO risk_events (user_id, ts, kind, reason, order_payload_json)"
            " VALUES (?, ?, 'reject', ?, ?)",
            (int(user_id), _now(), str(reason), payload),
        )
        conn.commit()
    except Exception:
        pass


def _ref_price(conn, symbol, order):
    """Latest mark: quotes_l1 mid/last, else candles_1m close, else order price."""
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
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
            last = float(d.get("last") or 0)
            if last > 0:
                return last
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
                return px
        except (TypeError, ValueError, KeyError, IndexError):
            pass
    try:
        px = float((order or {}).get("price") or 0)
        if px > 0:
            return px
    except (TypeError, ValueError, AttributeError):
        pass
    return 0.0


def _positions_map(conn, user_id):
    out = {}
    try:
        rows = conn.execute(
            "SELECT symbol, qty, avg_price, realized_pnl FROM positions"
            " WHERE user_id = ?",
            (int(user_id),),
        ).fetchall()
    except Exception:
        return out
    for r in rows or []:
        d = _row_dict(r)
        try:
            out[str(d.get("symbol", ""))] = {
                "qty": float(d.get("qty") or 0),
                "avg_price": float(d.get("avg_price") or 0),
                "realized_pnl": float(d.get("realized_pnl") or 0),
            }
        except (TypeError, ValueError):
            continue
    return out


def _total_pnl(conn, user_id, positions):
    """Realized + unrealized (marked at current refs)."""
    total = 0.0
    for sym, p in positions.items():
        total += p.get("realized_pnl", 0.0)
        qty = p.get("qty", 0.0)
        if qty:
            ref = _ref_price(conn, sym, None)
            if ref > 0:
                total += (ref - p.get("avg_price", 0.0)) * qty
    return total


def _recent_fills_with_side(conn, user_id, limit):
    """Last N fills (newest first) joined to orders for side/symbol/price."""
    try:
        rows = conn.execute(
            "SELECT f.price AS price, f.ts AS ts, f.symbol AS symbol,"
            " o.side AS side FROM fills f"
            " JOIN orders o ON o.id = f.order_id"
            " WHERE f.user_id = ? ORDER BY f.ts DESC, f.id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows or []:
        d = _row_dict(r)
        try:
            out.append({
                "price": float(d.get("price") or 0),
                "ts": int(d.get("ts") or 0),
                "symbol": str(d.get("symbol") or ""),
                "side": str(d.get("side") or "").lower(),
            })
        except (TypeError, ValueError):
            continue
    return out


def _is_loss(fill, ref):
    if ref <= 0 or fill["price"] <= 0:
        return False
    if fill["side"] == "buy":
        return ref < fill["price"]  # underwater long
    if fill["side"] == "sell":
        return ref > fill["price"]  # underwater short
    return False


def check(conn, user_id, order, tier="Free"):
    """Pre-trade gate. Returns (True, "") or (False, reason)."""
    order = dict(order or {})
    uid = int(user_id)
    limits = _get_limits(conn, uid)

    def reject(reason):
        _log(conn, uid, reason, order)
        return False, reason

    # 1. kill-switch
    if int(limits.get("kill_switch") or 0):
        return reject("kill-switch-on")

    # 2. basic sanity
    symbol = str(order.get("symbol", "") or "").strip()
    side = str(order.get("side", "") or "").strip().lower()
    try:
        qty = float(order.get("qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if not symbol:
        return reject("no-symbol")
    if side not in ("buy", "sell"):
        return reject("bad-side")
    if not qty > 0:
        return reject("bad-qty")

    # 3. allowlist
    try:
        allow = json.loads(limits.get("allowlist_json") or "[]")
    except (TypeError, ValueError):
        allow = []
    if isinstance(allow, list) and len(allow) > 0:
        allowed = {str(s).strip() for s in allow if str(s).strip()}
        if symbol not in allowed:
            return reject("symbol-not-allowlisted")

    # reference price (needed by 4/5/6/8)
    ref = _ref_price(conn, symbol, order)
    if ref <= 0:
        return reject("no-market-data")

    # 4. max position qty (resulting absolute)
    positions = _positions_map(conn, uid)
    cur_qty = positions.get(symbol, {}).get("qty", 0.0)
    delta = qty if side == "buy" else -qty
    try:
        max_q = float(limits.get("max_position_qty", 0) or 0)
    except (TypeError, ValueError):
        max_q = 0.0
    if max_q >= 0 and abs(cur_qty + delta) > max_q:
        return reject("max-position-qty")

    # 5. max notional
    try:
        max_n = float(limits.get("max_notional", 0) or 0)
    except (TypeError, ValueError):
        max_n = 0.0
    if max_n >= 0 and qty * ref > max_n:
        return reject("max-notional")

    # 6. max daily loss (account-level realized + unrealized)
    try:
        max_dl = float(limits.get("max_daily_loss", 0) or 0)
    except (TypeError, ValueError):
        max_dl = 0.0
    if max_dl > 0:
        pnl = _total_pnl(conn, uid, positions)
        if pnl <= -abs(max_dl):
            return reject("max-daily-loss")

    # 7. max trades per hour
    try:
        max_tph = int(limits.get("max_trades_per_hour", 0) or 0)
    except (TypeError, ValueError):
        max_tph = 0
    if max_tph >= 0:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE user_id = ?"
                " AND created_at >= ?",
                (uid, _now() - 3600),
            ).fetchone()
            n = int(row["n"] if "n" in row.keys() else row[0])
        except Exception:
            n = 0
        if n >= max_tph:
            return reject("max-trades-per-hour")

    # 8. cooldown after N consecutive losing fills
    try:
        need = int(limits.get("cooldown_after_losses", 0) or 0)
    except (TypeError, ValueError):
        need = 0
    if need > 0:
        fills = _recent_fills_with_side(conn, uid, need)
        if len(fills) >= need:
            refs = {}
            all_loss = True
            for f in fills:
                sym = f["symbol"] or symbol
                if sym not in refs:
                    refs[sym] = _ref_price(conn, sym, None)
                if not _is_loss(f, refs[sym]):
                    all_loss = False
                    break
            if all_loss and (_now() - fills[0]["ts"]) < COOLDOWN_SECONDS:
                return reject("cooldown-after-losses")

    return True, ""

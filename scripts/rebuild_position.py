"""Rebuild user 1's BTCUSD position from surviving fills.

Removes the 353 paper orders that tests/test_vibe_interactive.py wrote into
data/trading.db, then recomputes the position from the fills that remain --
using the same accounting as engines.paper._apply_position, replayed in
timestamp order. Recomputation rather than decrementing, because
subtracting 34.9 BTC from a 34.907 position leaves an average price that
was computed from a different history and will never reconcile.

Run with --apply to write. Without it, only reports.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.db import connect

# The exact signature of the test writes: PaperBroker(user_id=1) with
# cash=100000.0, three `handle("buy BTCUSD 0.1")` calls per run.
TARGET_USER = 1
TARGET_SYMBOL = "BTCUSD"
TEST_QTY = 0.1
TEST_MODE = "paper"


def find_test_orders(conn):
    """Test-written orders, identified three ways at once.

    All three must hold, so an unrelated order cannot be swept up:

      * user 1 / BTCUSD / paper / qty exactly 0.1
      * no audit_log row -- every order that went through the API is
        audited, and none of these 353 is
      * a matching fill exists with the same qty

    NOT EXISTS, never NOT IN. 13 audit rows have no $.id, json_extract
    returns NULL for those, and a single NULL makes `x NOT IN (...)`
    evaluate to NULL for every row -- a silent false that deletes nothing
    and looks like a clean no-op. That mistake was made once already.
    """
    sql = """
      SELECT o.id, o.created_at, o.status
      FROM orders o
      WHERE o.user_id = ? AND o.symbol = ? AND o.mode = ? AND o.qty = ?
        AND NOT EXISTS (
          SELECT 1 FROM audit_log a
          WHERE a.action LIKE 'order%'
            AND json_extract(a.details_json, '$.id') = o.id)
      ORDER BY o.created_at, o.id
    """
    return [dict(r) for r in conn.execute(sql, (TARGET_USER, TARGET_SYMBOL,
                                                 TEST_MODE, TEST_QTY))]


def replay(fills):
    """Position accounting, identical to engines.paper._apply_position.

    Replays in timestamp order so avg_price and realized_pnl come out the
    same way they did originally. Verified against that function's formula
    rather than approximating it.
    """
    qty = 0.0
    avg = 0.0
    realized = 0.0
    # Iterates rows by key, not by tuple unpacking: `for a, b, c in fills`
    # over dicts yields the dict's *keys* (strings), so the first version
    # raised "bad operand type for unary -: 'str'" on an empty-side
    # phantom. Every fill in this database has a side; the bug was mine.
    for row in fills:
        side = (row.get("side") or "").strip().lower()
        fqty = float(row["qty"])
        price = float(row["price"])
        if side not in ("buy", "sell"):
            raise ValueError(
                "fill {} has side {!r}; refusing to guess a direction"
                .format(row.get("id", "?"), row.get("side")))
        delta = fqty if side == "buy" else -fqty
        if qty == 0.0 or (qty * delta) > 0:
            new_qty = qty + delta
            avg = (qty * avg + delta * price) / new_qty if new_qty != 0 else 0.0
        else:
            closed = min(abs(qty), abs(delta))
            direction = 1.0 if qty > 0 else -1.0
            realized += closed * (price - avg) * direction
            new_qty = qty + delta
            if new_qty == 0:
                avg = 0.0
            elif (new_qty > 0) != (qty > 0):
                avg = price
            else:
                avg = avg
        qty = new_qty
    return qty, avg, realized


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = connect()
    orders = find_test_orders(conn)
    ids = [o["id"] for o in orders]
    print("test orders identified : %d (ids %s..%s)"
          % (len(ids), min(ids) if ids else "-", max(ids) if ids else "-"))

    if not ids:
        print("nothing to do")
        return

    marks = ",".join("?" * len(ids))
    doomed_fills = [dict(r) for r in conn.execute(
        "SELECT id, order_id, qty, price, fee, ts FROM fills"
        " WHERE order_id IN (%s)" % marks, ids)]
    kept = [dict(r) for r in conn.execute(
        "SELECT id, side, qty, price FROM fills WHERE user_id = ? AND symbol = ?"
        " AND order_id NOT IN (%s) ORDER BY ts, id" % marks,
        [TARGET_USER, TARGET_SYMBOL] + ids)]

    before = dict(conn.execute(
        "SELECT qty, avg_price, realized_pnl FROM positions"
        " WHERE user_id = ? AND symbol = ?",
        (TARGET_USER, TARGET_SYMBOL)).fetchone() or {})
    print("fills to delete        : %d" % len(doomed_fills))
    print("fills to keep          : %d" % len(kept))
    print()
    print("position BEFORE        : qty=%.6f avg=%.4f realized=%.4f"
          % (before.get("qty", 0), before.get("avg_price", 0),
             before.get("realized_pnl", 0)))
    qty, avg, realized = replay(kept)
    print("recomputed from kept   : qty=%.6f avg=%.4f realized=%.4f"
          % (qty, avg, realized))
    print("  delta                : qty=%.6f avg=%+.4f realized=%+.4f"
          % (qty - before.get("qty", 0), avg - before.get("avg_price", 0),
             realized - before.get("realized_pnl", 0)))

    # Sanity: deleting a fill set must strictly reduce exposure, and the
    # remaining fills must actually cover the remaining position.
    if qty < 0:
        print("\nABORT: recomputed position is negative -- kept fills do not "
              "explain the position. Not writing.")
        return 1
    if abs(qty) > sum(f["qty"] for f in kept) + 1e-9:
        print("\nABORT: position exceeds total kept fill qty. Not writing.")
        return 1

    if not args.apply:
        print("\ndry run -- pass --apply to write")
        return 0

    nf = conn.execute("DELETE FROM fills WHERE order_id IN (%s)" % marks,
                      ids).rowcount
    no = conn.execute("DELETE FROM orders WHERE id IN (%s)" % marks,
                      ids).rowcount
    if abs(qty) < 1e-9:
        conn.execute("DELETE FROM positions WHERE user_id = ? AND symbol = ?",
                     (TARGET_USER, TARGET_SYMBOL))
        print("position row deleted (flat)")
    else:
        conn.execute(
            "UPDATE positions SET qty = ?, avg_price = ?, realized_pnl = ?,"
            " updated_at = ? WHERE user_id = ? AND symbol = ?",
            (qty, avg, realized, int(time.time()), TARGET_USER, TARGET_SYMBOL))
        print("position row rewritten")
    conn.commit()

    print("\napplied: %d orders, %d fills deleted" % (no, nf))
    after = dict(conn.execute(
        "SELECT qty, avg_price, realized_pnl FROM positions"
        " WHERE user_id = ? AND symbol = ?",
        (TARGET_USER, TARGET_SYMBOL)).fetchone() or {})
    print("position AFTER         : qty=%.6f avg=%.4f realized=%.4f"
          % (after.get("qty", 0), after.get("avg_price", 0),
             after.get("realized_pnl", 0)))
    print("orders remaining       : %d"
          % conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
    print("fills remaining        : %d"
          % conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
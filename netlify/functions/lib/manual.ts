import { db } from "./db";
import { latestPrice } from "./market";

/**
 * Manual execution of an admin-approved ticket.
 *
 * Used when no automated venue is configured (or the venue is unavailable)
 * and an operator has actually executed the trade elsewhere. The price is the
 * operator's execution price, not a quote. Cash and positions move exactly
 * once, and the order cannot be filled twice.
 */
export async function fillOrderManually(orderId: number, price: number, adminId: number) {
  if (!isFinite(price) || price <= 0) throw new Error("fill price must be greater than 0");
  const rows: any = await db()`SELECT * FROM orders WHERE id = ${orderId}`;
  if (!rows.length) throw new Error("order not found");
  const o = rows[0];
  if (!["awaiting_manual_fill", "execution_failed"].includes(String(o.status))) {
    throw new Error("order is not awaiting manual fill");
  }
  const qty = Number(o.qty);
  const symbol = String(o.symbol || "").toUpperCase();
  const side = String(o.side || "").toLowerCase();
  const quote = await latestPrice(db(), symbol);
  if (!quote || !isFinite(quote.last) || quote.last <= 0) throw new Error("no reference quote for this symbol");
  if (price < quote.last * 0.25 || price > quote.last * 4) {
    throw new Error(`fill price ${price} is outside 0.25x–4x the last quote ${quote.last}`);
  }
  const riskRows: any = await db()`SELECT max_notional FROM risk_limits WHERE user_id = ${o.user_id}`;
  const maxNotional = Number(riskRows[0]?.max_notional || 100000);
  if (qty * price > maxNotional) throw new Error(`fill notional ${qty * price} exceeds max_notional ${maxNotional}`);
  if (!symbol || !isFinite(qty) || qty <= 0 || (side !== "buy" && side !== "sell")) {
    throw new Error("order is malformed");
  }

  const posRows: any = await db()`
    SELECT qty, avg_price, realized_pnl FROM positions WHERE user_id = ${o.user_id} AND symbol = ${symbol}
  `;
  const pos = posRows[0] || { qty: 0, avg_price: 0, realized_pnl: 0 };
  const oldQty = Number(pos.qty);
  const signed = side === "buy" ? qty : -qty;
  const newQty = oldQty + signed;
  const avg = Number(pos.avg_price);
  const closing = oldQty !== 0 && (oldQty > 0) !== (signed > 0);
  const closedQty = closing ? Math.min(Math.abs(signed), Math.abs(oldQty)) : 0;
  const realizedDelta = closing ? (price - avg) * closedQty * (oldQty > 0 ? 1 : -1) : 0;

  let newAvg = avg;
  if (oldQty === 0 || (oldQty > 0) === (signed > 0)) {
    newAvg = (Math.abs(oldQty) * avg + qty * price) / (Math.abs(oldQty) + qty);
  } else if (newQty !== 0 && (newQty > 0) !== (oldQty > 0)) {
    newAvg = price;
  }

  const acctRows: any = await db()`SELECT cash FROM accounts WHERE user_id = ${o.user_id}`;
  const cash = acctRows.length ? Number(acctRows[0].cash) : 0;
  const cashAfter = cash - signed * price;

  const updated: any = await db()`
    UPDATE orders SET status = 'filled', price = ${price}, venue_order_id = ${"manual:" + adminId},
      approval_note = ${"manual fill by admin " + adminId}
    WHERE id = ${orderId} AND status IN ('awaiting_manual_fill', 'execution_failed')
    RETURNING *
  `;
  if (!updated.length) throw new Error("order already filled or no longer fillable");
  const order = updated[0];

  const fillRows: any = await db()`
    INSERT INTO fills (order_id, user_id, symbol, side, qty, price, fee, cash_after)
    VALUES (${orderId}, ${o.user_id}, ${symbol}, ${side}, ${qty}, ${price}, 0, ${cashAfter})
    RETURNING *
  `;
  await db()`
    INSERT INTO positions (user_id, symbol, qty, avg_price, realized_pnl, updated_at)
    VALUES (${o.user_id}, ${symbol}, ${newQty}, ${newAvg}, ${Number(pos.realized_pnl) + realizedDelta}, ${Math.floor(Date.now() / 1000)})
    ON CONFLICT (user_id, symbol) DO UPDATE SET
      qty = EXCLUDED.qty, avg_price = EXCLUDED.avg_price,
      realized_pnl = EXCLUDED.realized_pnl, updated_at = EXCLUDED.updated_at
  `;
  await db()`UPDATE accounts SET cash = ${cashAfter} WHERE user_id = ${o.user_id}`;

  return { order, fill: fillRows[0], cash_after: cashAfter, realized_delta: realizedDelta };
}

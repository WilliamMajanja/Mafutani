import { db } from "./db";
import { latestPrice } from "./market";

export type OrderInput = {
  symbol?: string;
  side?: string;
  type?: string;
  qty?: number | string;
  price?: number | string | null;
  mode?: string;
};

const num = (v: unknown): number => {
  const n = typeof v === "number" ? v : parseFloat(String(v ?? ""));
  return isFinite(n) ? n : NaN;
};

/**
 * Execute a paper order against the latest stored quote.
 *
 * The write set is: order row, fill row, position row, cash. All four are
 * derived from the same fill price so the book reconciles, and a rejected
 * order leaves no partial writes. Live mode is refused here: no broker
 * adapter is configured, so accepting it would misrepresent execution.
 */
export async function executeOrder(userId: number, input: OrderInput) {
  const symbol = String(input.symbol || "").trim().toUpperCase();
  const side = String(input.side || "").trim().toLowerCase();
  const type = String(input.type || "market").trim().toLowerCase();
  const qty = num(input.qty);
  const limitPrice = input.price == null ? null : num(input.price);

  if (!symbol) throw httpError(400, "symbol required");
  if (side !== "buy" && side !== "sell") throw httpError(400, "side must be buy or sell");
  if (type !== "market" && type !== "limit") throw httpError(400, "type must be market or limit");
  if (!isFinite(qty) || qty <= 0) throw httpError(400, "qty must be greater than 0");
  if (type === "limit" && (!isFinite(limitPrice as number) || (limitPrice as number) <= 0)) {
    throw httpError(400, "limit orders need a positive price");
  }
  if (String(input.mode || "paper").toLowerCase() === "live") {
    throw httpError(403, "live trading is not configured");
  }

  const sym: any = await db()`SELECT symbol, enabled FROM symbols WHERE symbol = ${symbol}`;
  if (!sym.length || !sym[0].enabled) throw httpError(404, "unknown or disabled symbol");

  const quote = await latestPrice(db(), symbol);
  if (!quote) throw httpError(409, "no quote available for this symbol");

  const riskRows: any = await db()`
    SELECT max_position_qty, max_notional, kill_switch FROM risk_limits WHERE user_id = ${userId}
  `;
  const risk = riskRows[0] || { max_position_qty: 100, max_notional: 100000, kill_switch: 0 };
  if (Number(risk.kill_switch)) throw httpError(403, "kill switch is on");

  // A limit order that is not marketable rests as an open order; it does not
  // pretend to have filled.
  let fillPrice: number;
  if (type === "limit") {
    const lp = limitPrice as number;
    const marketable = side === "buy" ? lp >= quote.ask : lp <= quote.bid;
    if (!marketable) {
      const rows: any = await db()`
        INSERT INTO orders (user_id, symbol, side, type, qty, price, mode, status)
        VALUES (${userId}, ${symbol}, ${side}, 'limit', ${qty}, ${lp}, 'paper', 'open')
        RETURNING *
      `;
      return { order: rows[0], fill: null, cash_after: null, resting: true };
    }
    fillPrice = lp;
  } else {
    fillPrice = side === "buy" ? quote.ask : quote.bid;
  }

  const notional = qty * fillPrice;
  if (notional > Number(risk.max_notional)) {
    throw httpError(403, `order notional ${notional.toFixed(2)} exceeds max_notional ${risk.max_notional}`);
  }

  const posRows: any = await db()`
    SELECT qty, avg_price, realized_pnl FROM positions WHERE user_id = ${userId} AND symbol = ${symbol}
  `;
  const pos = posRows[0] || { qty: 0, avg_price: 0, realized_pnl: 0 };
  const oldQty = Number(pos.qty);
  const signed = side === "buy" ? qty : -qty;
  const newQty = oldQty + signed;
  if (Math.abs(newQty) > Number(risk.max_position_qty)) {
    throw httpError(403, `resulting position ${newQty} exceeds max_position_qty ${risk.max_position_qty}`);
  }

  const acctRows: any = await db()`SELECT cash FROM accounts WHERE user_id = ${userId}`;
  const cash = acctRows.length ? Number(acctRows[0].cash) : 0;
  if (side === "buy" && notional > cash) throw httpError(403, "insufficient cash");

  // Realised P&L is booked only on the portion that closes/reduces exposure.
  const closing = oldQty !== 0 && (oldQty > 0) !== (signed > 0);
  const closedQty = closing ? Math.min(Math.abs(signed), Math.abs(oldQty)) : 0;
  const avg = Number(pos.avg_price);
  const realizedDelta = closing ? (fillPrice - avg) * closedQty * (oldQty > 0 ? 1 : -1) : 0;

  let newAvg = avg;
  if (oldQty === 0 || (oldQty > 0) === (signed > 0)) {
    newAvg = (Math.abs(oldQty) * avg + qty * fillPrice) / (Math.abs(oldQty) + qty);
  } else if (newQty !== 0 && (newQty > 0) !== (oldQty > 0)) {
    newAvg = fillPrice;
  }

  const cashAfter = cash - signed * fillPrice;

  const orderRows: any = await db()`
    INSERT INTO orders (user_id, symbol, side, type, qty, price, mode, status)
    VALUES (${userId}, ${symbol}, ${side}, ${type}, ${qty}, ${fillPrice}, 'paper', 'filled')
    RETURNING *
  `;
  const order = orderRows[0];

  const fillRows: any = await db()`
    INSERT INTO fills (order_id, user_id, symbol, side, qty, price, fee, cash_after)
    VALUES (${order.id}, ${userId}, ${symbol}, ${side}, ${qty}, ${fillPrice}, 0, ${cashAfter})
    RETURNING *
  `;

  await db()`
    INSERT INTO positions (user_id, symbol, qty, avg_price, realized_pnl, updated_at)
    VALUES (${userId}, ${symbol}, ${newQty}, ${newAvg}, ${Number(pos.realized_pnl) + realizedDelta}, ${Math.floor(Date.now() / 1000)})
    ON CONFLICT (user_id, symbol) DO UPDATE SET
      qty = EXCLUDED.qty,
      avg_price = EXCLUDED.avg_price,
      realized_pnl = EXCLUDED.realized_pnl,
      updated_at = EXCLUDED.updated_at
  `;
  await db()`UPDATE accounts SET cash = ${cashAfter} WHERE user_id = ${userId}`;

  return { order, fill: fillRows[0], cash_after: cashAfter, resting: false };
}

export function httpError(status: number, message: string): Error & { status: number } {
  const e = new Error(message) as Error & { status: number };
  e.status = status;
  return e;
}

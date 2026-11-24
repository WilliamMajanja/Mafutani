/**
 * MetaTrader 5 live execution client for the Netlify port.
 *
 * The MT5 terminal cannot run inside a Netlify Function. The supported shape
 * is the one this repository already uses on the Python side: an operator-run
 * bridge next to the terminal (`services/mt5/bridge_server.py`) with the
 * `MafutaniBridge.mq5` EA attached. Netlify talks to that bridge over HTTPS.
 *
 * Acceptance is not a fill. MT5 retcode 10009 means the terminal accepted the
 * request for processing; the position may still be rejected, unfilled, or
 * partially filled. This client therefore reports `status: "open"` unless the
 * EA returns an explicit filled quantity.
 */

const RETCODE_ACCEPTED = new Set([10008, 10009, 10010]);

export type LiveOrderInput = {
  symbol?: string;
  side?: string;
  type?: string;
  qty?: number | string;
  price?: number | string | null;
  confirm?: boolean;
  client_order_id?: string;
};

export function liveConfigured(): boolean {
  return !!process.env.MT5_BRIDGE_URL && !!process.env.MT5_BRIDGE_TOKEN;
}

export function liveDisabledReason(): string {
  if (!process.env.MT5_BRIDGE_URL) return "MT5_BRIDGE_URL is not configured";
  if (!process.env.MT5_BRIDGE_TOKEN) return "MT5_BRIDGE_TOKEN is not configured";
  return "";
}

async function bridge(payload: Record<string, unknown>, timeoutMs = 25_000): Promise<any> {
  const base = (process.env.MT5_BRIDGE_URL || "").replace(/\/+$/, "");
  if (!base) throw liveError(503, "MT5 bridge is not configured");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(`${base}/exec`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Mafutani-Token": process.env.MT5_BRIDGE_TOKEN || "",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
  } catch (e) {
    throw liveError(502, `cannot reach MT5 bridge: ${(e as Error).message}`);
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let body: any = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* handled below */ }
  if (res.status === 401 || res.status === 403) throw liveError(502, "MT5 bridge refused the token");
  if (res.status === 504) {
    throw liveError(504, body?.error || "MT5 terminal did not respond; the order may or may not have executed — reconcile by ticket before retrying");
  }
  if (!res.ok) throw liveError(502, body?.error || `MT5 bridge HTTP ${res.status}`);
  if (!body || typeof body !== "object") throw liveError(502, "MT5 bridge returned an unexpected response");
  return body;
}

export async function submitLiveOrder(input: LiveOrderInput, tierLiveEnabled: boolean) {
  if (!liveConfigured()) throw liveError(503, `live trading unavailable: ${liveDisabledReason()}`);
  if (!tierLiveEnabled) throw liveError(403, "this tier does not have live trading enabled");
  if (input.confirm !== true) throw liveError(428, "live orders require confirm:true");

  const symbol = String(input.symbol || "").trim().toUpperCase();
  const side = String(input.side || "").trim().toLowerCase();
  const type = String(input.type || "market").trim().toLowerCase();
  const qty = Number(input.qty);
  if (!symbol) throw liveError(400, "symbol required");
  if (side !== "buy" && side !== "sell") throw liveError(400, "side must be buy or sell");
  if (!isFinite(qty) || qty <= 0) throw liveError(400, "qty must be greater than 0");

  const order: Record<string, unknown> = {
    symbol,
    side,
    qty,
    type,
    client_order_id: input.client_order_id || `mft-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  };
  if (type === "limit") {
    const price = Number(input.price);
    if (!isFinite(price) || price <= 0) throw liveError(400, "limit orders need a positive price");
    order.price = price;
  }

  const res = await bridge({ op: "submit", order, _timeout: 20 });
  if (!res.ok) throw liveError(502, `MT5 rejected the order: ${res.error || "unspecified"}`);
  const d = res.data || {};
  const retcode = d.retcode == null ? null : Number(d.retcode);
  if (retcode != null && !RETCODE_ACCEPTED.has(retcode)) {
    throw liveError(502, `MT5 rejected ${symbol}: retcode ${retcode}${d.comment ? ` (${d.comment})` : ""}`);
  }
  const filledQty = Number(d.filled_qty || 0);
  return {
    venue: "MT5",
    venue_order_id: String(d.ticket || d.order_id || ""),
    retcode,
    status: filledQty > 0 ? "partially_filled" : "open",
    symbol,
    side,
    qty,
    filled_qty: filledQty,
    price: Number(d.price || d.avg_price || input.price || 0),
    comment: d.comment || "",
  };
}

export function liveError(status: number, message: string): Error & { status: number } {
  const e = new Error(message) as Error & { status: number };
  e.status = status;
  return e;
}

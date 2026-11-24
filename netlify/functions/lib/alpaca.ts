/**
 * Alpaca live REST adapter for US equities and crypto.
 *
 * This is the Option B venue: a serverless-friendly REST API with a simple
 * key/secret model and explicit live/paper hosts. It does not cover FX,
 * commodities, futures or non-US indices; those need another adapter.
 */

const CRYPTO = new Set(["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"]);
const DEFAULT_BASE = "https://api.alpaca.markets";

export function alpacaConfigured(): boolean {
  return !!(process.env.ALPACA_API_KEY && process.env.ALPACA_API_SECRET);
}

export function alpacaBase(): string {
  const raw = (process.env.ALPACA_BASE_URL || DEFAULT_BASE).replace(/\/+$/, "");
  const u = new URL(raw);
  if (u.protocol !== "https:") throw new Error("ALPACA_BASE_URL must use HTTPS");
  return raw;
}

export function alpacaCanTrade(symbol: string): boolean {
  if (CRYPTO.has(symbol)) return true;
  // Alpaca uses plain tickers for US equities.
  return /^[A-Z]{1,5}$/.test(symbol);
}

function venueSymbol(symbol: string): string {
  if (symbol === "BTCUSD") return "BTC/USD";
  if (symbol === "ETHUSD") return "ETH/USD";
  if (symbol === "SOLUSD") return "SOL/USD";
  if (symbol === "XRPUSD") return "XRP/USD";
  return symbol;
}

function isCrypto(symbol: string): boolean {
  return CRYPTO.has(symbol);
}

async function request(path: string, init: RequestInit = {}): Promise<any> {
  if (!alpacaConfigured()) throw new Error("Alpaca credentials are not configured");
  const res = await fetch(`${alpacaBase()}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "APCA-API-KEY-ID": process.env.ALPACA_API_KEY || "",
      "APCA-API-SECRET-KEY": process.env.ALPACA_API_SECRET || "",
      ...(init.headers || {}),
    },
  });
  const text = await res.text();
  let body: any = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* handled below */ }
  if (!res.ok) {
    throw Object.assign(new Error(body?.message || `Alpaca HTTP ${res.status}`), { status: res.status });
  }
  return body;
}

export async function alpacaAccount(): Promise<any> {
  const a = await request("/v2/account");
  return {
    cash: Number(a.cash),
    equity: Number(a.equity),
    buying_power: Number(a.buying_power),
    currency: a.currency || "USD",
    account_number: a.account_number || "",
    status: a.status || "",
  };
}

export async function alpacaPositions(): Promise<any[]> {
  const rows = await request("/v2/positions");
  return (rows || []).map((p: any) => ({
    symbol: String(p.symbol || "").replace("/", ""),
    qty: Number(p.qty),
    avg_price: Number(p.avg_entry_price),
    last: Number(p.current_price),
    market_value: Number(p.market_value),
    unrealized_pnl: Number(p.unrealized_pl),
    realized_pnl: 0,
  }));
}

export async function alpacaSubmit(input: {
  symbol: string; side: string; type: string; qty: number; price?: number | null; client_order_id?: string;
}): Promise<any> {
  const symbol = String(input.symbol || "").toUpperCase();
  if (!alpacaCanTrade(symbol)) throw new Error(`Alpaca cannot trade ${symbol} (equities and crypto only)`);
  const body: Record<string, unknown> = {
    symbol: venueSymbol(symbol),
    qty: String(input.qty),
    side: input.side,
    type: input.type,
    time_in_force: isCrypto(symbol) ? "gtc" : "day",
    client_order_id: input.client_order_id || `mft-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  };
  if (input.type === "limit") body.limit_price = String(input.price);
  const o = await request("/v2/orders", { method: "POST", body: JSON.stringify(body) });
  const filled = Number(o.filled_qty || 0);
  return {
    venue: "ALPACA",
    venue_order_id: String(o.id || ""),
    status: String(o.status || "accepted"),
    symbol,
    side: input.side,
    qty: Number(o.qty || input.qty),
    filled_qty: filled,
    price: Number(o.filled_avg_price || input.price || 0),
    raw_status: o.status,
  };
}

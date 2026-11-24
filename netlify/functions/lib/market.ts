export interface Symbol {
  symbol: string;
  asset_type: string;
  name: string;
  enabled: number;
  tick_size: number;
  lot_size: number;
  exchange: string;
  currency: string;
  sector: string;
}

export type CandleRow = {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  source: string;
};

export async function listSymbols(db: any): Promise<Symbol[]> {
  return await db`SELECT * FROM symbols WHERE enabled = 1 ORDER BY symbol`;
}

export async function getQuotes(db: any): Promise<any[]> {
  return await db`
    SELECT q.symbol, q.ts, q.bid, q.ask, q.bid_size, q.ask_size, q.last, q.source,
           s.name, s.asset_type, s.exchange, s.currency
    FROM quotes_l1 q
    JOIN symbols s ON s.symbol = q.symbol
    WHERE q.ts = (SELECT MAX(ts) FROM quotes_l1 WHERE symbol = q.symbol)
    ORDER BY q.symbol
  `;
}

/**
 * Bars for a symbol, oldest first. `tf=1d` reads candles_1d; everything else
 * reads candles_1m. A missing higher-resolution series is reported as empty,
 * never back-filled from the daily table.
 */
export async function getCandles(db: any, symbol: string, limit = 200, tf = "1m"): Promise<CandleRow[]> {
  const rows: CandleRow[] = tf === "1d"
    ? await db`
        SELECT ts, open, high, low, close, volume, source
        FROM candles_1d
        WHERE symbol = ${symbol}
        ORDER BY ts DESC
        LIMIT ${limit}
      `
    : await db`
        SELECT ts, open, high, low, close, volume, source
        FROM candles_1m
        WHERE symbol = ${symbol}
        ORDER BY ts DESC
        LIMIT ${limit}
      `;
  return rows.slice().reverse();
}

export async function latestPrice(db: any, symbol: string): Promise<{ bid: number; ask: number; last: number } | null> {
  const rows: any = await db`
    SELECT bid, ask, last FROM quotes_l1
    WHERE symbol = ${symbol}
    ORDER BY ts DESC LIMIT 1
  `;
  if (!rows.length) return null;
  const r = rows[0];
  const last = Number(r.last ?? r.bid ?? r.ask);
  if (!isFinite(last) || last <= 0) return null;
  return { bid: Number(r.bid), ask: Number(r.ask), last };
}

/** Daily series for every symbol in one query, keyed by symbol, oldest first. */
export async function dailySeries(db: any, days = 120): Promise<Map<string, CandleRow[]>> {
  const rows: any = await db`
    SELECT symbol, ts, open, high, low, close, volume, source
    FROM candles_1d
    WHERE ts >= (SELECT MAX(ts) FROM candles_1d) - ${days * 86400}
    ORDER BY symbol, ts
  `;
  const out = new Map<string, CandleRow[]>();
  for (const r of rows) {
    const list = out.get(r.symbol) || [];
    list.push({
      ts: Number(r.ts), open: Number(r.open), high: Number(r.high), low: Number(r.low),
      close: Number(r.close), volume: Number(r.volume), source: r.source,
    });
    out.set(r.symbol, list);
  }
  return out;
}

/** Latest quote per symbol in one query. */
export async function quoteMap(db: any): Promise<Map<string, any>> {
  const rows: any = await db`
    SELECT q.symbol, q.bid, q.ask, q.last, q.ts
    FROM quotes_l1 q
    WHERE q.ts = (SELECT MAX(ts) FROM quotes_l1 WHERE symbol = q.symbol)
  `;
  const out = new Map<string, any>();
  for (const r of rows) out.set(r.symbol, r);
  return out;
}

export async function listExchanges(db: any): Promise<any[]> {
  return await db`SELECT * FROM exchanges ORDER BY code`;
}

export async function listMarkets(db: any): Promise<any[]> {
  return await db`SELECT code, name, type, asset_classes, region, currency, tz FROM exchanges ORDER BY code`;
}

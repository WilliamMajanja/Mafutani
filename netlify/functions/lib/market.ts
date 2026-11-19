export interface Symbol { symbol: string; asset_type: string; name: string; enabled: number; tick_size: number; lot_size: number; exchange: string; currency: string; sector: string; }

export async function listSymbols(db: any): Promise<Symbol[]> {
  return await db`SELECT * FROM symbols WHERE enabled = 1 ORDER BY symbol`;
}

export async function getQuotes(db: any): Promise<any[]> {
  return await db`
    SELECT q.symbol, q.ts, q.bid, q.ask, q.bid_size, q.ask_size, q.last, q.source,
           s.name, s.asset_type
    FROM quotes_l1 q
    JOIN symbols s ON s.symbol = q.symbol
    WHERE q.ts = (SELECT MAX(ts) FROM quotes_l1 WHERE symbol = q.symbol)
    ORDER BY q.symbol
  `;
}

export async function getCandles(db: any, symbol: string, limit = 200): Promise<any[]> {
  return await db`
    SELECT ts, open, high, low, close, volume, source
    FROM candles_1m
    WHERE symbol = ${symbol}
    ORDER BY ts DESC
    LIMIT ${limit}
  `;
}

export async function listExchanges(db: any): Promise<any[]> {
  return await db`SELECT * FROM exchanges ORDER BY code`;
}

export async function listMarkets(db: any): Promise<any[]> {
  return await db`SELECT code, name, type, asset_classes, region, currency, tz FROM exchanges ORDER BY code`;
}
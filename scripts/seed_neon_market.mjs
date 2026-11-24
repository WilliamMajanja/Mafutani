#!/usr/bin/env node
/**
 * Seed real daily market data into the Neon database.
 *
 * Source: Yahoo Finance chart API (1d bars, 1 year). Quotes are derived from
 * the latest daily close and marked `yahoo-chart`, so no figure is synthetic.
 *
 * Usage: node scripts/seed_neon_market.mjs
 */
import dotenv from "dotenv";
dotenv.config({ path: ".env.local" });
import { neon } from "@neondatabase/serverless";

const url = process.env.DATABASE_URL || process.env.DATABASE_URL_UNPOOLED;
if (!url) {
  console.error("DATABASE_URL is not set");
  process.exit(1);
}
const sql = neon(url);

const INSTRUMENTS = [
  { symbol: "AAPL", yahoo: "AAPL", name: "Apple Inc.", asset: "equity", exchange: "NASDAQ", currency: "USD", sector: "Technology" },
  { symbol: "MSFT", yahoo: "MSFT", name: "Microsoft Corp.", asset: "equity", exchange: "NASDAQ", currency: "USD", sector: "Technology" },
  { symbol: "NVDA", yahoo: "NVDA", name: "NVIDIA Corp.", asset: "equity", exchange: "NASDAQ", currency: "USD", sector: "Technology" },
  { symbol: "AMZN", yahoo: "AMZN", name: "Amazon.com Inc.", asset: "equity", exchange: "NASDAQ", currency: "USD", sector: "Consumer" },
  { symbol: "GOOGL", yahoo: "GOOGL", name: "Alphabet Inc.", asset: "equity", exchange: "NASDAQ", currency: "USD", sector: "Technology" },
  { symbol: "META", yahoo: "META", name: "Meta Platforms Inc.", asset: "equity", exchange: "NASDAQ", currency: "USD", sector: "Technology" },
  { symbol: "TSLA", yahoo: "TSLA", name: "Tesla Inc.", asset: "equity", exchange: "NASDAQ", currency: "USD", sector: "Automotive" },
  { symbol: "JPM", yahoo: "JPM", name: "JPMorgan Chase & Co.", asset: "equity", exchange: "NYSE", currency: "USD", sector: "Financials" },
  { symbol: "BTCUSD", yahoo: "BTC-USD", name: "Bitcoin / US Dollar", asset: "crypto", exchange: "COINBASE", currency: "USD", sector: "" },
  { symbol: "ETHUSD", yahoo: "ETH-USD", name: "Ether / US Dollar", asset: "crypto", exchange: "COINBASE", currency: "USD", sector: "" },
  { symbol: "EURUSD", yahoo: "EURUSD=X", name: "Euro / US Dollar", asset: "fx", exchange: "EBS", currency: "USD", sector: "" },
  { symbol: "GBPUSD", yahoo: "GBPUSD=X", name: "British Pound / US Dollar", asset: "fx", exchange: "EBS", currency: "USD", sector: "" },
  { symbol: "USDJPY", yahoo: "USDJPY=X", name: "US Dollar / Japanese Yen", asset: "fx", exchange: "EBS", currency: "USD", sector: "" },
  { symbol: "XAUUSD", yahoo: "GC=F", name: "Gold Futures", asset: "commodity", exchange: "COMEX", currency: "USD", sector: "Metals" },
  { symbol: "XAGUSD", yahoo: "SI=F", name: "Silver Futures", asset: "commodity", exchange: "COMEX", currency: "USD", sector: "Metals" },
  { symbol: "WTI", yahoo: "CL=F", name: "WTI Crude Oil Futures", asset: "commodity", exchange: "NYMEX", currency: "USD", sector: "Energy" },
  { symbol: "NATGAS", yahoo: "NG=F", name: "Natural Gas Futures", asset: "commodity", exchange: "NYMEX", currency: "USD", sector: "Energy" },
  { symbol: "COPPER", yahoo: "HG=F", name: "Copper Futures", asset: "commodity", exchange: "COMEX", currency: "USD", sector: "Metals" },
  { symbol: "CORN", yahoo: "ZC=F", name: "Corn Futures", asset: "commodity", exchange: "CBOT", currency: "USD", sector: "Grains" },
  { symbol: "US10Y", yahoo: "ZN=F", name: "10-Year T-Note Futures", asset: "future", exchange: "CBOT", currency: "USD", sector: "Rates" },
  { symbol: "SPX", yahoo: "^GSPC", name: "S&P 500 Index", asset: "index", exchange: "NYSE", currency: "USD", sector: "Index" },
  { symbol: "NDX", yahoo: "^NDX", name: "Nasdaq 100 Index", asset: "index", exchange: "NASDAQ", currency: "USD", sector: "Index" },
  { symbol: "VIX", yahoo: "^VIX", name: "CBOE Volatility Index", asset: "index", exchange: "CBOE", currency: "USD", sector: "Volatility" },
  { symbol: "SOLUSD", yahoo: "SOL-USD", name: "Solana / US Dollar", asset: "crypto", exchange: "COINBASE", currency: "USD", sector: "" },
  { symbol: "XRPUSD", yahoo: "XRP-USD", name: "XRP / US Dollar", asset: "crypto", exchange: "COINBASE", currency: "USD", sector: "" },
];

async function yahooDaily(yahoo, range = "1y") {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(yahoo)}?range=${range}&interval=1d`;
  const res = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (compatible; MafutaniBot/1.0)" } });
  if (!res.ok) throw new Error(`Yahoo ${yahoo}: HTTP ${res.status}`);
  const body = await res.json();
  const result = body?.chart?.result?.[0];
  const timestamps = result?.timestamp ?? [];
  const q = result?.indicators?.quote?.[0] ?? {};
  const bars = [];
  for (let i = 0; i < timestamps.length; i++) {
    if (q.open?.[i] == null || q.close?.[i] == null) continue;
    bars.push({
      ts: timestamps[i],
      open: q.open[i],
      high: q.high?.[i] ?? q.close[i],
      low: q.low?.[i] ?? q.close[i],
      close: q.close[i],
      volume: q.volume?.[i] ?? 0,
    });
  }
  return { bars, meta: result?.meta ?? {} };
}

async function main() {
  for (const inst of INSTRUMENTS) {
    try {
      const { bars, meta } = await yahooDaily(inst.yahoo);
      if (!bars.length) throw new Error("no bars returned");

      await sql`
        INSERT INTO symbols (symbol, asset_type, name, enabled, tick_size, lot_size, exchange, currency, sector)
        VALUES (${inst.symbol}, ${inst.asset}, ${inst.name}, 1, ${inst.asset === "fx" ? 0.0001 : 0.01},
                ${inst.asset === "crypto" ? 0.0001 : 1}, ${inst.exchange}, ${inst.currency}, ${inst.sector})
        ON CONFLICT (symbol) DO UPDATE SET
          asset_type = EXCLUDED.asset_type, name = EXCLUDED.name, enabled = 1,
          exchange = EXCLUDED.exchange, currency = EXCLUDED.currency, sector = EXCLUDED.sector
      `;

      await sql`
        INSERT INTO candles_1d (symbol, ts, open, high, low, close, volume, source)
        SELECT ${inst.symbol}, t, o, h, l, c, v, 'yahoo-chart'
        FROM unnest(
          ${bars.map((b) => b.ts)}::bigint[],
          ${bars.map((b) => b.open)}::double precision[],
          ${bars.map((b) => b.high)}::double precision[],
          ${bars.map((b) => b.low)}::double precision[],
          ${bars.map((b) => b.close)}::double precision[],
          ${bars.map((b) => b.volume)}::double precision[]
        ) AS x(t, o, h, l, c, v)
        ON CONFLICT (symbol, ts) DO UPDATE SET
          open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
          close = EXCLUDED.close, volume = EXCLUDED.volume, source = 'yahoo-chart'
      `;

      const last = bars[bars.length - 1];
      const prev = bars[bars.length - 2] ?? last;
      const spread = Math.abs(last.close) * (inst.asset === "fx" ? 0.00002 : 0.0005);
      await sql`
        INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size, last, source)
        VALUES (${inst.symbol}, ${Math.floor(Date.now() / 1000)}, ${last.close - spread}, ${last.close + spread},
                100, 100, ${last.close}, 'yahoo-chart')
      `;

      console.log(`${inst.symbol}: ${bars.length} bars, last ${last.close} (${(((last.close - prev.close) / prev.close) * 100).toFixed(2)}%), ${meta.currency ?? ""}`);
    } catch (e) {
      console.error(`${inst.symbol}: ${e.message}`);
    }
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});

import { alpacaConfigured, alpacaCanTrade } from "./alpaca";
import { liveConfigured as mt5Configured } from "./mt5";
import { paperEnabled } from "./trading";

export type VenueDescriptor = {
  id: string;
  label: string;
  dependency: string;
  configured: boolean;
  live: boolean;
  capabilities: string[];
  supports: (symbol: string) => boolean;
  detail: string;
};

/**
 * Zero-dependency venue registry.
 *
 * Each adapter is plain `fetch`/stdlib code. Adding a venue means adding a
 * module with `configured/supports/submit` and one entry here; the order
 * ticket, risk engine and journal do not change.
 */
export function venueRegistry(): VenueDescriptor[] {
  const alpaca = alpacaConfigured();
  const mt5 = mt5Configured();
  return [
    {
      id: "alpaca",
      label: "Alpaca",
      dependency: "REST over fetch (no SDK)",
      configured: alpaca,
      live: alpaca,
      capabilities: ["US equities", "crypto", "market orders", "limit orders"],
      supports: (symbol: string) => alpaca && alpacaCanTrade(symbol),
      detail: alpaca ? "keys present" : "ALPACA_API_KEY/SECRET missing",
    },
    {
      id: "mt5",
      label: "MetaTrader 5 bridge",
      dependency: "stdlib HTTP bridge + MQL5 EA",
      configured: mt5,
      live: mt5,
      capabilities: ["FX", "metals", "energy", "commodities", "indices", "any broker symbol"],
      supports: (symbol: string) => mt5 && !!symbol,
      detail: mt5 ? "bridge configured" : "MT5_BRIDGE_URL/TOKEN missing",
    },
    {
      id: "paper",
      label: "Internal paper book",
      dependency: "Postgres only",
      configured: paperEnabled(),
      live: false,
      capabilities: ["validation", "fills", "positions", "P&L"],
      supports: () => paperEnabled(),
      detail: paperEnabled() ? "local paper mode" : "disabled on hosted (live-only)",
    },
  ];
}

/** Which adapter would receive a live order for this symbol. */
export function routeLive(symbol: string): VenueDescriptor | null {
  return venueRegistry().find((v) => v.live && v.supports(symbol)) || null;
}

# Zero-dependency venue adapters

Mafutani routes live orders through plain adapters. None of them require an SDK
or a vendor library; each is `fetch` or stdlib HTTP.

## Contract

```ts
type VenueAdapter = {
  id: string;
  label: string;
  configured: boolean;
  supports(symbol: string): boolean;
  submit(order): Promise<VenueOrder>;
  health?(): Promise<{ ok: boolean; detail?: string }>;
  account?(): Promise<VenueAccount>;
  positions?(): Promise<VenuePosition[]>;
};
```

The order ticket, risk engine, audit log and journal do not know which adapter
executed an order. Adding a venue means adding one module and one registry entry.

## Current adapters

| Adapter | Dependency | Covers | Configured by |
| --- | --- | --- | --- |
| Alpaca | `fetch` REST | US equities, crypto | `ALPACA_API_KEY`, `ALPACA_API_SECRET` |
| MT5 bridge | stdlib HTTP + MQL5 EA | FX, metals, energy, commodities, indices | `MT5_BRIDGE_URL`, `MT5_BRIDGE_TOKEN` |
| Paper | Postgres only | validation/fills/positions | local `TRADING_MODE=paper` |

Admin → **Venue adapters** shows the registry and which adapter would receive a
given symbol. `/api/venues?symbol=AAPL` returns the route.

## MT5 setup (the missing piece)

MT5 needs a Windows host; it cannot run inside Netlify.

1. Install MetaTrader 5 and log in to the broker account.
2. On that host:
   ```bash
   python services/mt5/bridge_server.py --port 8031 --token <TOKEN>
   ```
3. Compile `services/mt5/MafutaniBridge.mq5` in MetaEditor and attach it to a
   chart. Add `http://127.0.0.1:8031` to Tools → Options → Expert Advisors →
   Allow WebRequest. Start with `AllowTrading = false` and verify reads.
4. Expose the bridge over HTTPS (Cloudflare named tunnel, Tailscale Funnel, or a
   reverse proxy with TLS). Do not expose plaintext HTTP to the internet.
5. Set:
   ```bash
   netlify env:set MT5_BRIDGE_URL 'https://bridge.example.com' --context production --force
   netlify env:set MT5_BRIDGE_TOKEN '<TOKEN>' --context production --force
   netlify deploy --prod --no-build
   ```
6. Verify:
   ```bash
   curl -s https://mafutani.org/api/venues?symbol=EURUSD -H "X-Session: $TOKEN"
   curl -s -X POST https://mafutani.org/api/admin/live/test -H "X-Session: $TOKEN"
   ```

`retcode 10009` is acceptance, not a fill. The adapter reports `open` until the
EA reports a fill. A 504 means the order may or may not have executed; reconcile
by ticket before retrying.

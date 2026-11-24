# Production configuration

This deployment (Netlify + Neon Postgres) is complete for paper trading and
market research. Two capabilities depend on operator-run services that cannot
run inside a Netlify Function. This file is the checklist for turning them on.

## 1. Required environment (Netlify, production/functions scope)

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Neon pooled Postgres URL |
| `DATABASE_URL_UNPOOLED` | Direct URL for migrations |
| `ADMIN_SETUP_TOKEN` | Header `x-admin-token` for `/api/admin/init` and `/api/admin/seed` |
| `GLOBAL_ADMIN_EMAILS` | Comma-separated bootstrap admin emails |
| `GLOBAL_ADMIN_PASSWORD` | Bootstrap password used by the seed endpoint |
| `MT5_BRIDGE_URL` | HTTPS URL of the MT5 bridge (live orders) |
| `MT5_BRIDGE_TOKEN` | Shared bridge token |
| `CHAIN_API_URL` | HTTPS URL of the Python CommCoin service |
| `CHAIN_API_TOKEN` | Shared token for the chain service, if configured |

Set them with:

```bash
netlify env:set NAME 'value' --context production --scope functions --force
```

## 2. Live MetaTrader 5

MT5 requires a Windows host running the terminal; it cannot be embedded in
Netlify.

1. On the Windows host, start the bridge:
   ```bash
   python services/mt5/bridge_server.py --port 8031 --token <TOKEN>
   ```
2. Attach `services/mt5/MafutaniBridge.mq5` to a chart in the terminal.
3. Expose the bridge over HTTPS (Cloudflare Tunnel, Tailscale Funnel, or a
   reverse proxy with TLS). Do not expose plain HTTP to the internet.
4. Set `MT5_BRIDGE_URL` and `MT5_BRIDGE_TOKEN` in Netlify and redeploy.
5. Verify: `GET /api/live/config` should return `available: true` for Pro,
   Institutional and Admin tiers.

Live orders require `confirm:true` and are audited. MT5 `retcode 10009` is
acceptance, not a fill; the UI reports `open` until the EA reports a fill.

## 3. CommCoin / MFT and M-Pesa

The chain lives in the Python service (`app.py`, `chain/`, `minting/`), not in
the TypeScript Netlify port. The Netlify app proxies every `/api/chain/*` and
`/api/offramp/mpesa*` request to `CHAIN_API_URL`.

1. Deploy the Python service to a host that supports long-lived processes
   (Render, Railway, Fly.io, or a VPS) using the repository `Dockerfile`.
2. Persist `chain/mft.db` and wallet/seed state on a volume. Never commit them.
3. Set `CHAIN_API_URL` to the public HTTPS origin and `CHAIN_API_TOKEN` to a
   shared secret.
4. Redeploy Netlify. `/api/status` will continue to report the chain as
   unconfigured until the variable is present.

Until this is done the CommCoin page shows an explicit 503 with the missing
configuration. It never fabricates chain data.

## 4. Market data refresh

Daily history is fetched from Yahoo Finance and written to `candles_1d`:

```bash
node scripts/seed_neon_market.mjs
```

Run it on a schedule (daily after the US close) from CI or a cron host. The
script is idempotent: it upserts bars and the latest quote per symbol.

## 5. Verification checklist

```bash
curl -s https://<site>/api/status
curl -s -X POST https://<site>/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"<admin>","password":"<password>"}'
```

Then, with the returned session token:

```bash
curl -s https://<site>/api/portfolio -H "X-Session: <token>"
curl -s https://<site>/api/screener -H "X-Session: <token>"
curl -s https://<site>/api/live/config -H "X-Session: <token>"
```

Expected: `status.ok = true`; portfolio returns cash/positions/summary;
screener returns stored instruments; live config reports `available` only when
the MT5 bridge is configured.

## 6. Trading-mode policy

- Local development (`app.py`, Raspberry Pi node) is **paper**. `.env.local` sets
  `TRADING_MODE=paper`.
- Hosted Netlify is **live-only**. Paper order entry returns HTTP 403. Set
  `TRADING_MODE=live` in Netlify (default when unset).
- Live orders additionally require `MT5_BRIDGE_URL` + `MT5_BRIDGE_TOKEN`, a tier
  with `live_enabled`, and `confirm:true`.

## 7. Operator textbook

`docs/Mafutani_Textbook.pdf` (also served at `/Mafutani_Textbook.pdf`) covers
architecture, page-by-page operations, risk, chain operations, security,
investor reporting, the CBK DhowCSD/bond blueprint, and the zero-dependency
broker adapter blueprint.

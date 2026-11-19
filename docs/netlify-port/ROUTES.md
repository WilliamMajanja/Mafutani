# Netlify port: route inventory

Source: `api/routes.py` (Python). Status: `pending` = not yet ported, `done` = ported to `netlify/functions/api.ts`, `deferred` = stays on a separate host (live money / chain / MT5).

| Path | Status | Notes |
|---|---|---|
| `/api/admin/audit` | pending |  |
| `/api/admin/audit.csv` | pending |  |
| `/api/admin/daraja/status` | deferred | Money/chain/broker runtime; decide separately |
| `/api/admin/daraja/withdraw` | deferred | Money/chain/broker runtime; decide separately |
| `/api/admin/external_refs` | pending |  |
| `/api/admin/external_refs/check` | pending |  |
| `/api/admin/kyc` | pending |  |
| `/api/admin/kyc/` | pending |  |
| `/api/admin/offramp/mpesa` | deferred | Money/chain/broker runtime; decide separately |
| `/api/admin/offramp/mpesa/` | deferred | Money/chain/broker runtime; decide separately |
| `/api/admin/offramp/mpesa/callbacks` | deferred | Money/chain/broker runtime; decide separately |
| `/api/admin/overview` | pending |  |
| `/api/admin/readiness` | pending |  |
| `/api/admin/symbols/` | pending |  |
| `/api/admin/users` | pending |  |
| `/api/admin/users/` | pending |  |
| `/api/agents` | pending |  |
| `/api/agents/` | pending |  |
| `/api/agents/tick` | pending |  |
| `/api/allocation` | pending |  |
| `/api/analysis` | pending |  |
| `/api/anomalies` | pending |  |
| `/api/arbitrage` | pending |  |
| `/api/arbitrage/venues` | pending |  |
| `/api/auth/2fa` | pending |  |
| `/api/auth/2fa/disable` | pending |  |
| `/api/auth/2fa/enable` | pending |  |
| `/api/auth/2fa/setup` | pending |  |
| `/api/auth/login` | pending |  |
| `/api/auth/logout` | pending |  |
| `/api/auth/signup` | pending |  |
| `/api/backtests` | pending |  |
| `/api/backtests/` | pending |  |
| `/api/backtests/validate` | pending |  |
| `/api/brokers` | deferred | Money/chain/broker runtime; decide separately |
| `/api/brokers/command` | deferred | Money/chain/broker runtime; decide separately |
| `/api/candles` | pending |  |
| `/api/chain/balance` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/blocks` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/commitment` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/export` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/headers` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/import` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/info` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/mine` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/proof/tx` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/proof/verify` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chains` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/send` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chain/wallet` | deferred | Money/chain/broker runtime; decide separately |
| `/api/chart_context` | pending |  |
| `/api/chart_position` | pending |  |
| `/api/compositor` | deferred | Money/chain/broker runtime; decide separately |
| `/api/compositor/action` | deferred | Money/chain/broker runtime; decide separately |
| `/api/cot` | pending |  |
| `/api/cot/contract` | pending |  |
| `/api/cot/history` | pending |  |
| `/api/cot/sync` | pending |  |
| `/api/exchanges` | pending |  |
| `/api/i18n` | pending |  |
| `/api/i18n/messages` | pending |  |
| `/api/journal` | pending |  |
| `/api/kyc/me` | pending |  |
| `/api/macro` | pending |  |
| `/api/macro/fetch` | pending |  |
| `/api/macro/fetch to refresh` | pending |  |
| `/api/macro/series` | pending |  |
| `/api/markets` | pending |  |
| `/api/me/entitlements` | pending |  |
| `/api/news` | pending |  |
| `/api/node/mempool` | deferred | Money/chain/broker runtime; decide separately |
| `/api/node/pinet` | deferred | Money/chain/broker runtime; decide separately |
| `/api/node/pinet/capture` | deferred | Money/chain/broker runtime; decide separately |
| `/api/node/status` | deferred | Money/chain/broker runtime; decide separately |
| `/api/node/sync/pull` | deferred | Money/chain/broker runtime; decide separately |
| `/api/node/tx/submit` | deferred | Money/chain/broker runtime; decide separately |
| `/api/offramp/mpesa` | deferred | Money/chain/broker runtime; decide separately |
| `/api/offramp/mpesa/callback` | deferred | Money/chain/broker runtime; decide separately |
| `/api/offramp/mpesa/info` | deferred | Money/chain/broker runtime; decide separately |
| `/api/orders` | deferred | Money/chain/broker runtime; decide separately |
| `/api/orders/` | deferred | Money/chain/broker runtime; decide separately |
| `/api/policies` | pending |  |
| `/api/portfolio` | pending |  |
| `/api/positions/flatten` | deferred | Money/chain/broker runtime; decide separately |
| `/api/quote` | pending |  |
| `/api/quotes` | pending |  |
| `/api/risk` | pending |  |
| `/api/screener` | pending |  |
| `/api/signals/preview` | pending |  |
| `/api/status` | done | Phase 1 foundation |
| `/api/strategies` | pending |  |
| `/api/symbols` | pending |  |
| `/api/terminal/command` | deferred | Money/chain/broker runtime; decide separately |
| `/api/watchlists` | pending |  |
| `/api/watchlists/` | pending |  |

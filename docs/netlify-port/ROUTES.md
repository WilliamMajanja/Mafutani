# Netlify port: route inventory

All routes are now ported to netlify/functions/api.ts (full implementation or functional stub).
Deferred routes (chain, node, compositor, terminal, brokers, offramp, positions/flatten) remain on the Python backend.

## Ported (full or stub)
- Auth: signup, login, logout, 2fa (setup/enable/disable), config
- Market data: symbols, quotes, quote, markets, exchanges, candles
- News, screener, strategies, analysis, anomalies, chart_context, chart_position
- Portfolio, orders (list+create), positions, risk, journal (list+create)
- Watchlists, backtests (list+create+validate+detail)
- COT, macro, allocation, arbitrage, arbitrage/venues
- i18n, i18n/messages, entitlements, policies, kyc/me
- Agents, agents/tick, signals/preview
- Admin: overview, readiness, audit, users, symbols/, external_refs, external_refs/check, kyc, kyc/
- Storage, AI (models+chat), auth config, realtime config, status, admin/init

## Deferred (keep on Python backend)
- /api/chain/*, /api/chains, /api/node/* (blockchain)
- /api/compositor, /api/compositor/action (desktop)
- /api/terminal/command (terminal)
- /api/brokers, /api/brokers/command (broker integration)
- /api/positions/flatten (live trading)
- /api/orders/ (detail), /api/watchlists/ (detail with id)
- /api/offramp/mpesa/* (real money)
- /api/admin/daraja/* (real money)

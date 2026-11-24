/**
 * Operator wiki served by /api/policies. Plain text so it renders anywhere.
 * Troubleshooting articles are first-class; the site changes often.
 */

export type WikiArticle = { slug: string; title: string; category: string; body: string };

const A = (slug: string, title: string, category: string, body: string): WikiArticle => ({ slug, title, category, body });

export function wikiArticles(): WikiArticle[] {
  return [
    A("start", "Start here: environments and modes", "Start here",
`Two environments share one codebase:

LOCAL / RASPBERRY PI
- Python app: python3 app.py
- URL: http://localhost:8000
- Trading mode: PAPER (TRADING_MODE=paper in .env.local)
- Data: local SQLite + local chain/mft.db
- Service: systemctl --user status mafutani

HOSTED
- Netlify SPA + functions, Neon Postgres
- URL: https://mafutani.org
- Trading mode: LIVE-ONLY (paper order entry returns 403)
- Live venues: Alpaca (US equities/crypto), MT5 bridge (FX/metals/energy/indices)
- CommCoin: proxied to CHAIN_API_URL; unavailable until configured

Never assume a feature works in both. Check the footer mode and /api/status first.`),

    A("login", "Troubleshooting: cannot log in", "Troubleshooting",
`Symptoms: "invalid credentials", page returns to login, button does nothing.

1. Check the email exactly. A typo such as williamamajanja@ is a different
   address and has no account.
2. Confirm the account exists: admin -> Users, or query the users table.
3. Clear the remembered email: DevTools -> Application -> Local Storage ->
   remove mafutani_email, or use a private window.
4. Check response codes:
   401 invalid credentials
   403 account disabled
   429 too many attempts (wait, or check edge rate limit)
   500 database or server error
5. If the page is blank, open the console. A JS boot error prevents form
   binding. Check that index.html loads app.js after the terminal/palette
   markup.
6. If login succeeds but the dashboard is empty, check /api/me/entitlements
   with the session token.`),

    A("dashboard", "Dashboard, equity and P&L", "Trading",
`P&L is derived, not accumulated:

equity = cash + securities market value + CommCoin value
total P&L = equity - starting_cash
unrealized = sum((last - avg_price) * qty) for open positions
realized = (cash + securities MV - starting_cash) - unrealized
return % = total P&L / starting_cash

If the dashboard says "updated ..." the data polled successfully. It refreshes
every 15 seconds. The quote tape polls every 10 seconds.

If cash is 100,000,000 and positions are empty, the account is flat. Three
admins at 100M each show 300,000,000 aggregate starting cash in Admin.

Common causes of a P&L mismatch:
- stale quote for an open position
- a live position exists at the venue but not in the local book
- CommCoin is connected and included in equity
- starting_cash was changed by an operator`),

    A("orders", "Troubleshooting: orders disabled or rejected", "Troubleshooting",
`"LIVE ONLY - the MT5 venue is not connected"
- Hosted is live-only. Configure ALPACA_API_KEY/SECRET (equities/crypto) or
  MT5_BRIDGE_URL/TOKEN (FX, metals, energy, indices).

403 paper trading is disabled
- Expected on hosted. Local .env.local must set TRADING_MODE=paper.

403 kill switch is on
- Admin/risk_limits kill_switch = 1. Clear it before ordering.

403 insufficient cash / max_notional / max_position_qty
- The order exceeds the risk limits. Check /api/risk.

404 unknown or disabled symbol
- The symbol is disabled in Admin -> Instruments.

409 no quote available
- The quotes_l1 table has no row for the symbol. Refresh market data.

428 live orders require confirm:true
- The ticket sends it after the confirmation dialog. API callers must send it.

502/503 venue errors
- The bridge or Alpaca is unreachable or credentials are wrong. Check
  Admin -> System readiness -> Test MT5 bridge and function logs.

If an order is accepted but no position appears:
- Hosted live positions live at the venue; the local paper book is separate.
- Check the order status and venue_order_id in Portfolio -> Recent orders.`),

    A("mt5", "MT5 bridge: setup and troubleshooting", "Live venues",
`MT5 cannot run inside Netlify. Run the bridge on a Windows host beside the
terminal.

  python services/mt5/bridge_server.py --port 8031 --token <TOKEN>
  # attach services/mt5/MafutaniBridge.mq5 to a chart
  # expose over HTTPS (Cloudflare Tunnel, Tailscale Funnel, reverse proxy)

Netlify:
  MT5_BRIDGE_URL=https://bridge.example.com
  MT5_BRIDGE_TOKEN=<TOKEN>

Checks:
  GET /api/live/config          -> mt5 true, available true
  Admin -> Test MT5 bridge      -> HTTP 200 from /health
  GET /api/status               -> db up

Common errors:
- "MT5_BRIDGE_URL is not configured": env missing or not redeployed.
- "bridge must use HTTPS": plaintext non-loopback URL refused.
- "bridge refused the token": token mismatch.
- "EA did not respond in time": terminal closed, EA not attached, or symbol
  not in Market Watch. The order may or may not have executed; reconcile by
  ticket before retrying.
- retcode 10009 is acceptance, not a fill. The UI reports open until a fill
  is reported.
- retcode 10025 trade context busy: another command is executing; wait.`),

    A("alpaca", "Alpaca live venue: setup and troubleshooting", "Live venues",
`Alpaca is the Option B venue for US equities and crypto.

  ALPACA_API_KEY=<key>
  ALPACA_API_SECRET=<secret>
  ALPACA_BASE_URL=https://api.alpaca.markets   # live; optional

Routing:
  AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, JPM -> Alpaca
  BTCUSD, ETHUSD, SOLUSD, XRPUSD -> BTC/USD, ETH/USD, SOL/USD, XRP/USD
  FX, metals, energy, indices -> MT5 (Alpaca does not cover them)
  SPX, NDX, VIX, N225, HSI, ASX200, SENSEX, US10Y -> no live adapter

Common errors:
- 401/403 from Alpaca: wrong key/secret or account not enabled for the asset.
- "insufficient buying power": not a Mafutani risk limit; the venue refused.
- "Alpaca cannot trade X": instrument has no adapter; do not fake it.
- Orders accepted but not filled: market may be closed; crypto trades 24/7.
- Use the live host. The paper host is paper-api.alpaca.markets and is not
  used by this deployment.`),

    A("commcoin", "CommCoin: local node, hosted proxy and troubleshooting", "CommCoin",
`The chain is a separate Python service. The hosted app proxies /api/chain/*
to CHAIN_API_URL.

LOCAL PI
  systemctl --user status mafutani
  curl -s http://127.0.0.1:8000/api/chain/info
  POST /api/chain/wallet {confirm:true}
  POST /api/chain/mine {rounds:500000}

HOSTED
  CHAIN_API_URL=https://<public-chain-service>
  CHAIN_API_TOKEN=<shared-token>

If CHAIN_API_URL is absent, the page shows a setup panel. That is expected,
not a stub. Hosted users cannot use the Pi's localhost directly.

Common errors:
- "internal: name 'ROOT' is not defined": fixed in api/routes.py.
- "create a chain wallet first": POST /api/chain/wallet before mining.
- "no solution in round": normal; increase rounds or call again with next_nonce.
- Balance 0 after mining: blocks after the epoch seed carry no block reward.
  The 100B seed goes to the address that mines the seed block. Distribute by
  transfer from the seeded wallet.
- "csrf mismatch": send X-CSRF from login alongside X-Session.
- Wallet state is PQC/stateful. Never restore an old backup or reuse indexes.

The local operator wallet holds the epoch seed. A verified transfer of
1,000,000 MFT funded the Pi operator account.`),

    A("pi", "Raspberry Pi operations", "Operations",
`Device: Raspberry Pi 5, 16 GB, aarch64, Debian, Python 3.13.

Start/restart:
  systemctl --user restart mafutani
  systemctl --user status mafutani
  journalctl --user -u mafutani -f

Health:
  curl -s http://127.0.0.1:8000/api/status
  curl -s http://127.0.0.1:8000/api/chain/info

If the service is "activating (auto-restart)" with exit code 1:
- check the log for a port conflict (another python3 app.py is running)
- kill the stray process and restart the service
- verify the working directory and Python path in the unit file

Wallet files live in data/wallets with mode 600. Back up the current state
only. Chain state is chain/mft.db.`),

    A("deploy", "Netlify and Neon deployment", "Operations",
`Build:
  npm run typecheck
  netlify deploy --prod --no-build

If the build fails in @netlify/plugin-emails, retry; it is a Netlify app
plugin/API issue, not the repository. You can also deploy with --no-build
after running the typecheck locally.

Environment changes require a redeploy.
Secrets live in Netlify env, never in git. .env and .env.local are ignored.

Neon:
- DATABASE_URL (pooled) and DATABASE_URL_UNPOOLED (direct)
- migrations are additive; run ALTER TABLE IF NOT EXISTS style changes
- check /api/status for db up
- scale-to-zero cold starts add latency to the first query

Cache:
- index.html, JS and CSS are no-cache; API responses are no-store
- a stale browser tab should be reloaded after a deploy`),

    A("marketdata", "Market data, zones and consolidation bases", "Reference",
`Prices are delayed daily history from Yahoo Finance. They are research data,
not an execution feed.

Tables:
  candles_1d   daily OHLCV, source yahoo-chart
  candles_1m   minute bars when a real feed exists
  quotes_l1    latest top-of-book per symbol
  consolidation_bases  ports, airports, docks, SEZs, ICDs, rail hubs

Zones: AMER, EUROPE, MEA, APAC, GLOBAL. Sessions are computed in UTC.

Refresh:
  node scripts/seed_neon_market.mjs
  node scripts/seed_bases.mjs

If a chart is empty, the symbol may have no rows at the requested resolution.
The API falls back from 1m to 1d and reports the served tf.`),

    A("research", "Screener, analysis, anomalies and backtests", "Research",
`Screener: session change, RSI, trend, score, volume, spread, anomaly score.
Analysis: adds an AI brief and a universe overview (breadth, regime, classes,
movers, dispersion, RSI, EMA21 breadth).
Anomalies: price z-score, volume z-score, gaps. All instruments are shown by
default; "flagged only" hides clean rows.
Backtests: long/flat signal simulation on stored bars with 5 bps cost, Sharpe,
drawdown, win rate, profit factor, CAGR, coverage and a significance verdict.

A short sample is reported as unproven. Book imbalance and news momentum
cannot be backtested from candles alone and return an explicit error.`),

    A("journal", "Journal and reconciliation", "Trading",
`Every order path writes a journal row: ticket, terminal, flatten. Backtests,
AI analyses, strategy reviews and desk briefs are journaled too.

Admin -> Trade ledger shows all users and reconciliation:
  orders: total orders
  missing_journal: orders without a matching journal ref_id

If missing_journal is not zero, an order path bypassed the logger. Check
recent deployments and add logging at that path. Export the ledger before
changing schema.

To filter: GET /api/admin/journal?user_id=&kind=&symbol=&limit=`),

    A("security", "Security, access and incident response", "Security",
`Controls:
- HTTPS, HSTS, CSP, frame-deny, nosniff, strict referrer policy
- HttpOnly SameSite session cookie plus bearer token fallback
- Admin tier on /api/admin/*; self-disable and self-demote blocked
- ADMIN_SETUP_TOKEN on setup endpoints
- per-IP edge rate limit; CSRF token on mutating calls
- audit_log for logins, orders, admin actions, bridge tests

Incident:
1. Disable the account.
2. Revoke its sessions (Admin -> Revoke sessions).
3. Rotate MT5 bridge token, chain token, Alpaca keys, database password.
4. Preserve audit_log and function logs before changes.
5. Notify per legal/regulatory obligations.

Not yet done: external penetration test, SSO/MFA, central SIEM, formal SOC 2
or ISO 27001 certification. Do not claim them.`),

    A("approvals", "Trade approvals and 3/3 admin consensus", "Trading",
`Every hosted live order is a request, not an execution.

1. POST /api/orders creates status='pending_approval'.
2. The request is journaled as kind='trade_request'.
3. Admin approves or rejects in Admin -> Trade approvals.
4. One approval for a normal order.
5. A large move requires every active admin. The threshold is
   ORDER_CONSENSUS_THRESHOLD (default 100000 notional). With three admins that
   is 3/3.
6. When the threshold is met, the order is sent to the live venue.
7. A venue failure sets status='execution_failed'; Retry re-attempts once the
   threshold is still met.
8. A rejection closes the request. No paper fallback.

Endpoints:
  GET  /api/admin/orders/pending
  POST /api/admin/orders/:id/approve
  POST /api/admin/orders/:id/reject
  POST /api/admin/orders/:id/retry

If no venue is configured, approval reaches the venue step and fails with the
venue error. The request remains visible and auditable.`),

    A("cash", "Cash, M-Pesa C2C and reconciliation", "Trading",
`Cash is a reconciled view, not an editable number.

Deposit:
1. Portfolio -> Cash & M-Pesa -> submit amount and the M-Pesa transaction code.
2. The app creates a cash_movements row with status='pending'.
3. Send M-Pesa C2C to the configured number (default +254713270444).
4. Admin -> Cash approvals -> Confirm after checking the statement.
5. Confirmation credits accounts.cash exactly once. A replayed confirmation
   returns 409 and cannot double-credit.
6. Rejection closes the request with a note.

Endpoints:
  GET  /api/cash/config
  POST /api/cash/deposit
  GET  /api/cash/movements
  GET  /api/admin/cash?status=pending
  POST /api/admin/cash/:id/confirm
  POST /api/admin/cash/:id/reject
  GET  /api/admin/cash/reconcile

Rules:
- M-Pesa C2C is the funding rail, not a trading venue.
- Live venue cash is separate; move funds to the venue and record it.
- The M-Pesa bank statement remains the source of truth.
- KYC/AML, sanctions, tax and licensing are operator responsibilities.`),

    A("hardening", "Hardening baseline (CPIP-informed)", "Security",
`CPIP- is a report-only external reference. Mafutani does not vendor, depend on,
or auto-apply it. Its defensive concepts are adopted manually.

Hosted hardening baseline (Admin -> Hardening baseline; /api/admin/hardening):
H-01 live-only hosted; paper rejected
H-02 ADMIN_SETUP_TOKEN on setup endpoints
H-03 env bootstrap admins; demo accounts removed
H-04 Admin tier on every admin route
H-05 per-user session revocation
H-06 TOTP/SSO — FAIL, not implemented in the Netlify port
H-07 audit_log for auth/orders/admin/bridge tests
H-08 journal for trades, backtests, AI reviews, UX errors
H-09 HTTPS, HSTS, CSP, frame-deny, nosniff, referrer policy
H-10 live bridge tokens only over HTTPS
H-11 edge rate limit — PARTIAL, per isolate
H-12 request body limit — 1 MB at the function boundary
H-13 live venue — FAIL until Alpaca/MT5 credentials
H-14 chain service — FAIL until CHAIN_API_URL
H-15 external penetration test — MANUAL
H-16 KYC/AML/sanctions — MANUAL
H-17 backup/restore test — MANUAL
H-18 data provenance and partial-bar flag — PASS

Next actions: TOTP/SSO, shared rate-limit store, live venue, chain service,
penetration test, KYC/AML program, restore drill.

Verification:
  curl -sI https://mafutani.org/ | grep -Ei 'content-security|x-frame|strict-transport'
  curl -s https://mafutani.org/api/admin/hardening -H "X-Session: $TOKEN"

This is a control checklist, not a certification or guarantee.`),

    A("errors", "Troubleshooting: HTTP status codes", "Troubleshooting",
`400 bad input: read the message; symbol/side/qty/price validation.
401 unauthorized: missing or expired session. Log in again.
403 forbidden: tier, kill switch, risk limit, disabled account, paper on
hosted, or admin-only route.
404 not found: wrong path or unknown symbol.
409 conflict: no quote, no candles, duplicate email.
428 confirmation required: live order without confirm:true.
429 rate limited: wait for Retry-After.
500 server error: database or code bug; check function logs.
501 not implemented: capability not configured (COT, macro, KYC review, 2FA).
502 upstream error: AI gateway, Alpaca, MT5 bridge or chain service.
503 unavailable: missing env/config or upstream down.
504 timeout: MT5 EA did not answer; the order may or may not have executed.`),

    A("stale", "Troubleshooting: stale data or blank widgets", "Troubleshooting",
`Dashboard timestamp not moving: the 15s poll is failing. Check /api/quotes and
the browser Network tab.
Tape says "feed reconnecting": the quote poll failed. Check /api/status.
Chart empty: no candles at that resolution. Try tf=1d; check /api/candles.
Heatmap blank: /api/screener returned no rows or the canvas has zero height.
Anomalies empty: no instrument met the thresholds; uncheck "flagged only".
News empty: Google News RSS failed or no symbol match; the response carries
stale:true and an error string.
Markets empty: exchanges table missing rows or region values changed.
Admin 403: the session tier is not Admin.
AI 502: gateway key/credits or model ID. Test /api/ai/models.`),

    A("api", "API reference", "Reference",
`POST /api/auth/login            session + CSRF + cookie
POST /api/auth/signup           bootstrap admin or Free
GET  /api/me/entitlements       tier, email, display name
GET  /api/portfolio             cash, equity, positions, P&L, CommCoin
GET/POST /api/orders            list / place
POST /api/positions/flatten     close all
GET  /api/screener              universe analytics
GET  /api/universe              breadth, regime, classes, movers
GET  /api/analysis              per-symbol intelligence
GET  /api/anomalies             scanner
GET  /api/candles               bars by tf
GET  /api/quote(s)              latest quotes
GET  /api/news                  RSS ingestion/cache
POST /api/signals/preview       strategy signal
POST /api/backtests             run/store
POST /api/ai/analysis           AI brief
POST /api/ai/strategy           AI strategy review
POST /api/ai/brief              desk brief
GET  /api/policies              policy + wiki
GET  /api/admin/*               Admin tier
GET/POST /api/chain/*           proxied chain service`),

    A("faq", "FAQ", "Start here",
`Is this live? Hosted is live-only; local is paper. Footer shows the mode.
Can I trade without a venue? No. Orders are disabled until Alpaca or MT5 is
configured. A button that fills without a venue would be a simulation.
Why three admins at 100M show 300M? Each account has its own 100M starting
cash; Admin aggregates them.
Why is my P&L negative with no closed trades? Open positions are marked to
the latest stored quote.
Why is CommCoin 503? CHAIN_API_URL is not configured; the Pi chain is local.
Why can't I mine from hosted? Hosted sessions are Neon; the chain has its own
users. Identity bridging is not implemented.
Is the data real-time? No, daily Yahoo history, delayed.
Is MFT a security? No. Experimental token, no guaranteed value.`),
  ];
}

# Mafutani

**Zero-dependency, stdlib-only Python platform for experimental trading, CommCoin/MFT (PiNet), and operator-guarded off-ramps.**

![stdlib-only](https://img.shields.io/badge/runtime-stdlib--only-1f6feb)
![python](https://img.shields.io/badge/python-3.x-3776AB)
![chain](https://img.shields.io/badge/chain-CommCoin--MFT_experimental-f59e0b)
![license](https://img.shields.io/badge/license-see_LICENSE-lightgrey)

> **Status:** experimental / unaudited. Use in controlled environments. Read [SECURITY.md](./SECURITY.md) and [docs/POLICIES.md](./docs/POLICIES.md) before deployment.

## What Mafutani Includes

| Area | What it does |
|---|---|
| **Trading (paper-first)** | Quotes/screener/signals/backtests/orders/positions/journal/risk. Live trading is gated (`LIVE_TRADING_ENABLED`, tier/risk/confirm, venue credentials) and never silently simulated. |
| **CommCoin / MFT** | Independent stdlib-only chain: SBC-mineable SHAKE256 PoW, hash-based PQC signatures (WOTS+/Merkle/SHAKE256), 182-day epochs, **100,000,000,000 MFT** deterministic epoch seed, deterministic token expiry/replacement, Merkle proofs/commitments. |
| **PiNet (SBC capture)** | Raspberry Pi/SBC-friendly block capture: bounded mining rounds, mempool staging, tx submit, chain export/import/headers, peer pull over stdlib HTTP JSON, proof verification. |
| **Node Compositor Desktop** | Wayland-inspired **in-process** model (display/registry/surface/output/seat/xdg/toplevel/scene/damage/shell) + web renderer at `#/desktop`. Not libwayland/DRM/KMS. |
| **Off-ramp (Daraja/M-Pesa)** | User-facing CommCoin → M-Pesa request flow + admin actions. Requires operator config, CSRF/auth/audit, explicit `confirm:true`. **Acceptance is not settlement.** |
| **Zero-dep KYC hooks** | Local KYC workflow (status, hashed document references, admin review/audit, optional off-ramp gate). **Technical plumbing only — not legal/regulatory compliance.** |
| **Web UI + Terminal** | Vanilla JS SPA, SSE quotes, Bloomberg-style terminal (`\`` / `Ctrl+K`), i18n hooks, policies page. No npm build. |

## Consensus / Proofing Hardening (experimental, not certification)
- Added additive checks (tests passing): ledger tx/block bounds, duplicate input/txid detection, memo/output/input limits, block hash consistency, **difficulty bits enforcement for tip-extension**, sync response size/structure limits, stricter Merkle proof verification (`verify_detailed`, schema/reasons, domain allowlist, path limits). **Not a formal proof of safety/liveness/consensus correctness.**
- Sync remains **tip-append** with untrusted peers; PoW/PQC/chain **unaudited**. No guarantees for any industry/discipline.

## Honest Risk Summary

- **Not hardened or certified for all industries and disciplines.** No code/test/doc/control certifies suitability for finance, payments, land/title/registry, healthcare, government, safety-critical, or regulated use.
- **Not financial, investment, legal, tax, or real-estate/title advice.** Paper default; live/crypto can cause real loss.
- **CommCoin/MFT is experimental and unaudited.** Keys/transactions may be irreversible; bugs/consensus/PQC/fork-stall issues/key loss can cause total loss. **No guaranteed security, consensus correctness, value, liquidity, convertibility, compliance, or settlement.**
- **`$100,000` per CommCoin/MFT is reference/config metadata only** (`MAFUTANI_COIN_USD_VALUE`) — **not** a market price, quote, guarantee, offer, liquidity promise, or stability guarantee.
- **Design goals ≠ guarantees:** deterministic supply/epoch rules, SBC/PiNet capture, validation controls, and “no unnecessary token loss” design intent do **not** guarantee value, stability, security, adoption, consensus correctness, or absence of bugs/hacks.
- **M-Pesa/Daraja:** third-party/operator-configured. **Request acceptance ≠ settlement.** Operators own credentials, callbacks/reconciliation, KYC/AML/sanctions/tax/compliance, and lawful deployment.
- **PQC wallets are stateful:** **never reuse signature indexes or restore stale wallet backups/state.** One authoritative wallet state only.
- **CPIP/external references are report-only.** No safe auto-update or auto-apply; auto-apply is intentionally excluded. Never fetch/apply external code/config into the runtime automatically. Manual security review required.
- **Land/asset transactions:** the chain may carry bounded metadata/hash anchors, but this is **not** a land registry, not legal title/ownership, and not proof of enforceability. “Any size” is technically bounded (UTXOs, tx/block/memo/record limits, wallet signature capacity, token expiry rules). Current token expiry semantics may be unsuitable for durable land/title. Requires authoritative registries, jurisdictional legal process, KYC/authority controls, and audits.
- **No replacement for audits.** Production reliance requires independent crypto/consensus/security audits, formal/threat modeling, adversarial/fuzz/fork-stall testing, vetted standardized PQC migration (e.g., SLH-DSA/ML-DSA or audited implementations) with versioned test vectors, and operator compliance/custody controls.

### Technical remediations (reduce preventable risk; residual risk remains)

- **Irreversible/high-impact actions gated:** explicit `confirm:true` for CommCoin sends, chain import, PiNet sync pull, and off-ramp/admin submission paths (where implemented); auth/CSRF/admin controls and audit logging on sensitive flows.
- **Stateful PQC wallet safety:** capacity checks before signing, atomic state save (`tmp`+`fsync`+`replace`), and fail-safe if state cannot be persisted (do not use/broadcast tx). **Cannot prevent** stale backup restore or external key misuse.
- **Reference-only labeling:** chain/node/off-ramp/KYC/market-reference APIs and docs label `MAFUTANI_COIN_USD_VALUE` and MFT quotes as reference/config metadata only (no price/guarantee). Off-ramp responses include settlement disclaimers.
- **Validation/sync controls:** PiNet import/pull is validated and currently tip-append oriented; peers remain untrusted. Harden further for adversarial/public P2P.
- **KYC/off-ramp workflow controls:** zero-dep KYC hooks (optional gate) + admin review/audit; **not** legal/compliance verification. Daraja flows require operator config and reconciliation; acceptance ≠ settlement.
- **CPIP/external refs controls:** admin report-only check/list endpoints (`/api/admin/external_refs*`); **never auto-update/apply external code**.
- **Not remediable by code alone:** unaudited/experimental status, industry/discipline fitness, consensus/security correctness, value/stability/liquidity outcomes, legal/tax/regulatory/title compliance, third-party settlement/reconciliation, CPIP/external change review, user/operator custody mistakes, and absence of bugs/hacks.

### What cannot be guaranteed or created by this code
- **Safety/security/consensus is not guaranteed.** Experimental/unaudited software; bugs, consensus/PQC issues, forks/stalls, misconfiguration, and attacks can still cause loss. Controls reduce preventable risk but do not make the system "safe."
- **No guarantees of value, price, liquidity, convertibility, or stability.** `MAFUTANI_COIN_USD_VALUE` is reference/config metadata only. Real value/liquidity require external markets, participants, adoption, listings, and economics — not code assertions.
- **Not compliance-certified or industry-hardened.** KYC hooks are workflow plumbing only. Land/asset anchors are not legal title/registry records. Legal/regulatory/title/compliance fitness depends on jurisdiction, qualified advice, authoritative registries/processes, operator controls, audits, and third-party requirements. Request acceptance is **not** settlement.

- **Irreversible/high-impact actions gated:** explicit `confirm:true` for CommCoin sends, chain import, PiNet sync pull, and off-ramp/admin submission paths (where implemented); auth/CSRF/admin controls and audit logging on sensitive flows.
- **Stateful PQC wallet safety:** capacity checks before signing, atomic state save (`tmp`+`fsync`+`replace`), and fail-safe if state cannot be persisted (do not use/broadcast tx). **Cannot prevent** stale backup restore or external key misuse.
- **Reference-only labeling:** chain/node/off-ramp/KYC/market-reference APIs and docs label `MAFUTANI_COIN_USD_VALUE` and MFT quotes as reference/config metadata only (no price/guarantee). Off-ramp responses include settlement disclaimers.
- **Validation/sync controls:** PiNet import/pull is validated and currently tip-append oriented; peers remain untrusted. Harden further for adversarial/public P2P.
- **KYC/off-ramp workflow controls:** zero-dep KYC hooks (optional gate) + admin review/audit; **not** legal/compliance verification. Daraja flows require operator config and reconciliation; acceptance ≠ settlement.
- **Not remediable by code alone:** unaudited/experimental status, value/stability/liquidity outcomes, legal/tax/regulatory compliance, third-party settlement/reconciliation, user/operator custody mistakes, and absence of bugs/hacks.

## Quick Start

```bash
git clone https://github.com/WilliamMajanja/Mafutani.git
cd Mafutani
python3 scripts/seed.py      # init DB + demo data (optional but useful)
python3 app.py               # http://localhost:8000 (PORT overrides)
```

Demo (if seeded): `demo@local` / `demo1234` (Pro).

Useful local mode:
```bash
PORT=8000 MAFUTANI_NO_FEED=1 python3 app.py
```

Docker:
```bash
docker build -t mafutani .
docker run -p 8000:8000 mafutani
```

## PiNet on Raspberry Pi / SBC (CommCoin Capture)

CommCoin is designed to be SBC-friendly. PiNet provides bounded block capture and simple stdlib sync.

```bash
# On a Raspberry Pi / SBC
PORT=8000 MAFUTANI_NO_FEED=1 \
MAFUTANI_PINET_ROUNDS=100000 \
MAFUTANI_PINET_MAX_ROUNDS=500000 \
python3 app.py
```

- Use the UI (`#/chain`) or APIs to create/load a wallet and run **bounded** capture rounds.
- Sync/import is validated and currently **tip-append** oriented (`/api/chain/export`, `/api/chain/import`, `/api/node/sync/pull`); treat peers as untrusted and harden before adversarial/public deployment.
- **Do not** reset `chain/mft.db` or overwrite wallet seed/state files casually.

## Key Configuration

| Variable | Purpose |
|---|---|
| `PORT` | HTTP server port (default 8000) |
| `MAFUTANI_NO_FEED=1` | Disable external quote feed polling |
| `MAFUTANI_CHAIN_DB` | Chain SQLite path (default `chain/mft.db`) |
| `MAFUTANI_COIN_USD_VALUE` | **Reference metadata only** (default `100000`) |
| `MAFUTANI_COIN_SYMBOL` / `MAFUTANI_COIN_NAME` / `MAFUTANI_COIN_ALIAS` | Coin display metadata |
| `MAFUTANI_PINET_ROUNDS` / `MAFUTANI_PINET_MAX_ROUNDS` | SBC mining/capture bounds |
| `MAFUTANI_MFT_KES_RATE` | Operator-configured KES/MFT reference rate for off-ramp (0 disables) |
| `MAFUTANI_MPESA_OFFRAMP_MIN_MFT` / `MAX_MFT` | Off-ramp amount limits |
| `MAFUTANI_OFFRAMP_MAX_PENDING_PER_USER` / `COOLDOWN_SECONDS` / `MAX_PER_DAY` | Preventive velocity/cooldown controls (not compliance) |
| `MAFUTANI_OFFRAMP_MAKER_CHECKER=1` | Require approve→submit by different admin (preventive) |
| `MAFUTANI_REQUIRE_2FA_FOR_SENSITIVE=1|strict` | Step-up TOTP for sensitive admin actions |
| `MAFUTANI_DARAJA_CALLBACK_SECRET` | Optional shared-secret gate for callback scaffold (not full Daraja verification) |
| `MAFUTANI_OFFRAMP_TREASURY_MFT_ADDRESS` | Optional treasury instruction for user off-ramp requests |
| `MAFUTANI_MPESA_OFFRAMP_AUTO_SUBMIT=1` | Explicit opt-in auto-submit to Daraja (if B2C-ready); **not settlement** |
| `MAFUTANI_KYC_REQUIRED_FOR_OFFRAMP=1` | Require approved KYC workflow status before off-ramp requests |
| `DARAJA_*` | Daraja credentials/config (see `minting/daraja.py`, `minting/mpesa_offramp.py`); keep in secret stores |
| `LIVE_TRADING_ENABLED=1` | Enables live trading gate path (still requires tier/risk/confirm/configured broker) |
| `MAFUTANI_PROXY` / `MAFUTANI_PROXY_NO_PROXY` | Optional stdlib SOCKS5/HTTP proxy for outbound venue calls |

> Never commit `.env`, Daraja credentials, or wallet seed/state files. `*.db` and `data/wallets/*seed*.json` patterns are ignored.

## API Overview (authoritative: `api/routes.py`)

All mutating endpoints typically require an authenticated session and CSRF; admin endpoints require admin tier; some require explicit `confirm:true`.

### Core / Trading
- Auth: `POST /api/auth/signup|login|logout`, `GET /api/me/entitlements`, 2FA endpoints (`/api/auth/2fa*`)
- Market data: `GET /api/symbols`, `GET /api/candles`, `GET /api/screener`, `GET /stream/quotes` (SSE)
- Watchlists/strategies/signals/backtests/orders/portfolio/risk/journal/news/COT endpoints (see `api/routes.py`)
- Brokers: `GET /api/brokers`, `POST /api/brokers/command` (paper/REST profiles; no Binance integration)
- Arbitrage (Pro+): `GET /api/arbitrage`, `GET /api/arbitrage/venues`
- Terminal: `POST /api/terminal/command`

### CommCoin / MFT / PiNet
- Chain: `GET /api/chain/info`, `GET /api/chain/blocks`, `GET /api/chain/balance?address=...`, `GET /api/chain/wallets`, `POST /api/chain/wallet/create`, `POST /api/chain/tx/send`, `POST /api/chain/mine` (admin), `POST /api/chain/epoch/seed` (admin)
- Proofs/commitments: `GET /api/chain/proof/tx`, `GET /api/chain/commitment`, `POST /api/chain/proof/verify`
- Sync/export: `GET /api/chain/export`, `GET /api/chain/headers`, `POST /api/chain/import` (admin+CSRF), `POST /api/node/sync/pull` (admin+CSRF)
- PiNet/node: `GET /api/node/status`, `GET /api/node/pinet`, `GET /api/node/mempool`, `POST /api/node/pinet/capture` (auth+CSRF), `POST /api/node/tx/submit` (auth+CSRF)

### Compositor / Locale / Policies
- `GET /api/compositor`, `POST /api/compositor/action` (auth/CSRF as implemented)
- `GET /api/i18n`, `GET /api/i18n/messages?lang=...`
- `GET /api/policies`

### Off-ramp / KYC / Admin
- User M-Pesa off-ramp: `GET /api/offramp/mpesa/info`, `GET /api/offramp/mpesa`, `POST /api/offramp/mpesa` (`confirm:true`)
- Admin off-ramp: `GET /api/admin/offramp/mpesa`, `POST /api/admin/offramp/mpesa/<id>/{approve|submit|reject|complete|pending|failed}` (admin+CSRF; `confirm:true`; optional maker-checker `MAFUTANI_OFFRAMP_MAKER_CHECKER=1` requires approve by different admin before submit; optional step-up 2FA via `MAFUTANI_REQUIRE_2FA_FOR_SENSITIVE`). Daraja callback scaffold: `POST /api/offramp/mpesa/callback` (reconciliation only; authenticity not fully verified; optional `MAFUTANI_DARAJA_CALLBACK_SECRET`). Admin callbacks: `GET /api/admin/offramp/mpesa/callbacks`.
- KYC workflow hooks: `GET/POST /api/kyc/me`, `GET /api/admin/kyc`, `POST /api/admin/kyc/<user_id>/review` (admin+CSRF)
- Admin Daraja (legacy/admin tool path, as implemented): `GET /api/admin/daraja/status`, `POST /api/admin/daraja/withdraw` (admin+CSRF+`confirm:true`)
- Admin: `GET /api/admin/overview`, `GET /api/admin/audit`, `GET /api/admin/readiness` (operator checklist; **not** safety/compliance certification), `GET /api/admin/external_refs`, `POST /api/admin/external_refs/check` (admin+CSRF+confirm; **report-only, NEVER auto-updates/applies external code**)

## OpenTerminal

Press `` ` `` or `Ctrl+K` (or the Terminal button) for the command drawer.

```text
help | quote AAPL | buy AAPL 1 | sell BTCUSD 0.5 limit 70000
flatten [SYM] | positions | orders [N] | signal ema_trend AAPL
backtest ema_trend AAPL 2000 | screener | risk | tier
arb [crypto|fx|commodity|all] | venues BTCUSD
```

Terminal trades are paper-only and risk-checked like REST orders.

## Project Layout

```text
api/          HTTP routes & handlers
chain/        CommCoin/MFT consensus, ledger, PQC, wallet, miner, Merkle
node/         PiNet, mempool, proofs, sync/export/import
compositor/   Wayland-like in-process desktop model
minting/      Daraja + M-Pesa off-ramp + zero-dep KYC hooks
core/         trading/risk/i18n/terminal logic
data/         schema, feeds, markets, locale assets
engines/      backtest/signals/arbitrage/analysis
brokers/      paper/REST/MT5 broker adapters (no Binance)
web/          vanilla JS/CSS SPA
docs/         policies, chain docs, RFC/RFF mirrors
tests/        stdlib/pytest-compatible tests
```

## Zero-Dependency (stdlib-only)

The app/runtime tree is intentionally stdlib-only. No `requirements.txt`, `pip install`, or npm build is required for the core app.

```bash
python3 -m pytest tests/test_zero_dependency.py -q
```

## Security & Responsible Disclosure

See [SECURITY.md](./SECURITY.md). **Do not** open public issues for vulnerabilities. High-impact areas: keys/PQC state, admin/off-ramp/KYC controls, chain/consensus/proofs, PiNet import/sync, and secrets/custody.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md). Keep changes stdlib-only, tested, documented, and honest about risk/compliance.

## Documentation

- [Docs index](./docs/index.md)
- [Policies & security stance](./docs/POLICIES.md)
- [Mafutani Chain / CommCoin](./docs/MAFUTANI_CHAIN.md)
- [RFCs](./rfcs/) / [docs/rfcs](./docs/rfcs/)
- [RFFs](./rffs/) / [docs/rffs](./docs/rffs/)
- [Changelog](./CHANGELOG.md)

## License

See [LICENSE](./LICENSE). Repository licensing/rights are defined by the owner; do not assume an open-source grant until an explicit license is provided.

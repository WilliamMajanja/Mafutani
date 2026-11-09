# Mafutani Policies & Security Stance

**Scope:** Mafutani platform, APIs, web UI, terminal, CommCoin/MFT chain, PiNet/node sync, compositor desktop, wallet/mining, M-Pesa/Daraja off-ramp, and zero-dependency KYC workflow hooks.

> Operational safeguards and disclosures — **not legal advice**. Operators must obtain qualified legal, tax, and compliance advice for their jurisdictions.

## 1. Risk & No-Advice Disclaimer

> **No system safety, value, liquidity, or compliance is guaranteed by this software.** Technical controls reduce preventable risk; they do not create audits, legal compliance, market value, or liquidity.

- **Not financial, investment, trading, legal, or tax advice.** Outputs are informational/experimental.
- **Paper trading is default** for trading features. Live trading is gated/audited; real loss is possible.
- **Digital assets / CommCoin (MFT) carry extreme risk.** Total loss possible. Transfers may be irreversible. Bugs, consensus/PQC failures, key loss, stale PQC state reuse, network/payment failures, fraud, and regulatory action can cause loss.
- **No guarantee of value, liquidity, convertibility, or stability.** The configured CommCoin/MFT reference value (default **$100,000 USD**, `MAFUTANI_COIN_USD_VALUE`) is **reference/config metadata only** — not a market price, quote, guarantee, offer, or payout promise.
- **Design goals are not guarantees.** Deterministic epoch seeding/expiry replacement, SBC/PiNet capture, validation controls, and “no unnecessary token loss / no nonsense hacks” are design intents and technical controls — **not** proof of security, stability, or economic outcomes.

## 2. CommCoin / MFT / PiNet Policy

- Chain/node/compositor/wallet/miner/PiNet code is **experimental and not externally audited**.
- Consensus is deterministic by code (`chain/consensus.py`, `chain/ledger.py`): 182-day epochs, **100,000,000,000 MFT** epoch seed, token expiry/replacement as implemented. Changes require explicit, versioned migration/review.
- **PQC (hash-based):** `MFT-PQC-WOTS-MERKLE-SHAKE256-v1` — **not externally audited**, **stateful**. Never reuse signature indexes; never restore stale wallet backups/state.
- **PiNet sync/import:** block export/import/pull use stdlib JSON/HTTP and validated tip-append import in the current model. Treat peer data as untrusted; harden for adversarial/public P2P use.

## 3. M-Pesa / Daraja, Off-ramps, and Zero-dep KYC

- **M-Pesa/Daraja** is operator-configured (third-party). User/admin off-ramp flows require auth/CSRF/audit and explicit `confirm:true` as implemented. **Acceptance is not settlement.** Callbacks/reconciliation, idempotency, float/limits, recipient validity, and compliance are required.
- **Secrets:** store Daraja credentials in secret stores/env; never in source control, logs, or client responses. `SecurityCredential` handling is operator-controlled.
- **Zero-dependency KYC hooks** (`minting/kyc.py`, `/api/kyc/*`) provide workflow status, hashed document references, admin review, and optional off-ramp gating. **Not** a substitute for legal/regulatory compliance, qualified identity verification, sanctions/AML screening, or professional advice.

## 4. Remediations, Controls & Residual Risk (honest stance)

These are **technical mitigations**, not guarantees. They reduce preventable errors but **cannot eliminate** unaudited/experimental risk, value/stability outcomes, legal/compliance obligations, third-party settlement, or user/operator custody mistakes. **No immunity claimed; no fabricated incident details.**

### Technical remediations implemented (verify in code/config)
- **Explicit confirmation (`confirm:true`)** for high-impact/irreversible actions where implemented: CommCoin send (`/api/chain/send`), chain import (`/api/chain/import`, admin), PiNet sync pull (`/api/node/sync/pull`, admin), M-Pesa off-ramp create, admin off-ramp submit, and other gated flows as documented.
- **Stateful PQC wallet safety (`chain/wallet.py`):** checks signature index capacity before signing, signs inputs then performs atomic state save (`tmp`+`fsync`+`replace`, restrictive mode), and **fails safe** if state cannot be persisted (raises a critical error instructing not to broadcast). **Cannot prevent** stale backup restore, copied wallet files, or external misuse.
- **Reference-only / no-guarantee labeling:** `MAFUTANI_COIN_USD_VALUE` (default $100,000/coin) and MFT market/reference quotes are labeled metadata/reference only across chain/node/off-ramp APIs and docs — not market price, quote, guarantee, offer, liquidity, or stability.
- **Settlement honesty:** off-ramp responses/docs state **Daraja/request acceptance is not settlement**; reconciliation, callbacks/idempotency, valid credentials/limits, and operator compliance are required.
- **Access controls:** session auth, CSRF on mutating flows, admin gates for sensitive operations, `HttpOnly`/`SameSite` cookies (and `Secure` where applicable), 2FA capability (`/api/auth/2fa*`).
- **Audit logging:** key transitions/actions recorded where implemented.
- **PiNet/sync validation:** import/pull validates blocks and uses a pragmatic tip-append model; peers treated as untrusted (further adversarial hardening required for public deployment).
- **Zero-dep KYC workflow hooks:** status/hashed references/admin review/audit and optional off-ramp gate (`MAFUTANI_KYC_REQUIRED_FOR_OFFRAMP`) — **technical workflow only, not legal/regulatory compliance**.
- **Stdlib-only runtime:** reduces third-party dependency surface (not a substitute for review/hardening). Enforced by `tests/test_zero_dependency.py`.

### Residual risks (cannot be fully remediated by code; operator/user responsibility)
- **Experimental/unaudited:** chain, PQC, Merkle, PiNet, compositor, trading live paths, off-ramp/KYC integrations may contain defects causing loss or incorrect behavior.
- **Irreversibility / total loss:** blockchain transfers may be irreversible; key loss, stale PQC state reuse, consensus bugs, or operational errors can cause total loss.
- **No value/stability guarantees:** reference metadata and deterministic protocol rules do not guarantee market value, liquidity, convertibility, stability, adoption, or economic outcomes.
- **Custody/state misuse:** users/operators can restore stale PQC backups, copy state, leak secrets, or misconfigure hosts. Use secure custody practices; never commit seeds/credentials.
- **Third-party settlement & compliance:** Daraja/M-Pesa and other services are operator-configured third parties. **Acceptance ≠ settlement.** Operators own callbacks/reconciliation, credentials, fraud controls, KYC/AML/sanctions/tax/compliance, and lawful deployment.
- **Insufficient dual control:** admin+CSRF+confirm ≠ enforced maker-checker. Add separation of duties, step-up 2FA, allowlists, velocity limits, delays/cooling-off, out-of-band confirmation, and reconciliation.
- **Deployment hardening:** TLS, reverse proxy/WAF, OS/runtime hardening, secret stores, logging/observability (no secrets), backups, and incident playbooks are required.

## 5. Data, Locale, User Protection

- i18n is supported (`core/i18n.py`, locale files, `/api/i18n*`); legal/compliance notices must be localized and reviewed per jurisdiction.
- Collect minimum necessary data. Protect logs from secrets and sensitive personal data. Follow applicable privacy/data-protection laws.

## 6. Change Control

- Consensus, monetary policy, PQC/key custody, payout/off-ramp, KYC/admin controls, and live-trading gates require review, tests, migration notes, and updated docs.
- Core app remains **stdlib-only**; dependency additions require explicit reviewed decisions and updated enforcement.
